#!/usr/bin/env python3
"""CrewHex Screen Client v2.

Runs on a Raspberry Pi (or any Linux box) behind a Chromium kiosk. Stdlib only,
Python 3.9+ (Bullseye) and 3.11 (Bookworm).

What it does
  * Pairs with CrewHex using the v2 protocol: the SERVER issues the code shown on
    the TV plus a device secret only this box holds. The device token is
    released once, to the secret holder, and stored 0600.
  * Keeps a local copy of the screen's manifest and every image/video it needs,
    each verified by SHA-256, so the TV keeps playing through network outages.
  * Serves the kiosk page and that local copy on 127.0.0.1 only. The kiosk page
    never sees the device token.
  * Heartbeats telemetry and proof-of-play; runs remote commands (identify,
    reload, clear cache); installs signed OTA updates into /var/lib (writable
    under ProtectSystem=strict) and rolls back if a new build won't start.
  * Falls back to the v1 endpoints if the server hasn't been upgraded yet, and
    reuses a v1 device token, so a Pi upgraded from 1.1.x stays paired.

Protocol: docs/PROTOCOL.md.  Server: teamHub_resell api/routers/signage.py.
"""
import hashlib
import http.server
import json
import os
import re
import secrets
import shutil
import socketserver
import subprocess
import tarfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("SCREEN_CONFIG", "/etc/crewhex-screen/config.json"))
STATE_PATH = Path(os.environ.get("SCREEN_STATE", "/var/lib/crewhex-screen/device.json"))
STATE_DIR = STATE_PATH.parent
MEDIA_DIR = STATE_DIR / "media"
MANIFEST_PATH = STATE_DIR / "manifest.json"
APP_DIR = STATE_DIR / "app"                       # OTA installs land here (launch.py picks them)
STATIC_DIR = HERE / "kiosk"
PUBKEY_PATH = HERE / "ota_pubkey.pem"             # present => OTA signatures are REQUIRED
CLIENT_VERSION = ((HERE / "VERSION").read_text().strip() if (HERE / "VERSION").exists() else "dev")

POLL_MANIFEST = int(os.environ.get("SCREEN_POLL_MANIFEST", "20"))
POLL_PAIRING = float(os.environ.get("SCREEN_POLL_PAIRING", "3"))
HEARTBEAT = int(os.environ.get("SCREEN_HEARTBEAT", "60"))
LIVE = 60
ROTATE_TOKEN_AFTER = 7 * 86400
VERSION_RE = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}$")
LEGACY_PIN_DIGITS = "23456789"
MAX_MEDIA_BYTES = 300 * 1024 * 1024
PAGE_KEY_RE = re.compile(r"^hub-page/[0-9a-fA-F-]{36}/[0-9a-fA-F-]{36}\.(jpg|png|webp)$")


def log(*a):
    print("[screen]", *a, flush=True)


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return default


def write_private(path, data):
    """Atomic write, 0600 from the first byte. (v1 recreated device.json with the
    default umask after a re-pair, leaving the token world-readable.)"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(data if isinstance(data, str) else json.dumps(data, indent=2))
    os.chmod(str(tmp), 0o600)
    os.replace(str(tmp), str(path))


def unlink(p):
    try:
        Path(p).unlink()
    except FileNotFoundError:
        pass


# ============================================================ state
class State:
    def __init__(self):
        self.lock = threading.Lock()
        cfg = load_json(CONFIG_PATH, {})
        self.api_base = (cfg.get("api_base") or "https://api.crewhex.com").rstrip("/")
        self.display_name = cfg.get("display_name") or "Screen"
        self.port = int(cfg.get("port", 8080))
        st = load_json(STATE_PATH, {})
        self.device_token = st.get("device_token")
        self.device_id = st.get("device_id")
        self.token_at = st.get("token_at") or time.time()
        self.paired_at = st.get("paired_at")
        self.reg = None               # {id, code, secret, expires, expires_at}
        self.legacy_pin = None
        self.legacy_registered = False
        self.v1 = False               # server has no v2 endpoints yet
        self.manifest = load_json(MANIFEST_PATH, None) if self.device_token else None
        self.live = {}
        self.last_ok = None
        self.last_error = None
        self.kiosk_commands = []
        self.report = {"plays": {}, "telemetry": {}}
        self.force_sync = False
        self.boot = time.time()

    def save_device(self):
        st = load_json(STATE_PATH, {})
        st.update({"device_token": self.device_token, "device_id": self.device_id, "paired_at": self.paired_at,
                   "token_at": self.token_at, "display_name": self.display_name})
        write_private(STATE_PATH, st)

    def ok(self):
        self.last_ok = time.time()
        self.last_error = None

    def link(self):
        age = (time.time() - self.last_ok) if self.last_ok else 1e9
        return "green" if age < 90 else ("amber" if age < 300 else "red")


STATE = State()


# ============================================================ http to CrewHex
class HTTPStatus(Exception):
    def __init__(self, code, body=None):
        super().__init__("HTTP %s" % code)
        self.code = code
        self.body = body


def api(method, path, body=None, token=None, timeout=10, headers=None):
    """Returns (status, json_or_None). Raises HTTPStatus for >= 400."""
    req = urllib.request.Request(STATE.api_base + path,
                                 data=json.dumps(body).encode() if body is not None else None, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "crewhex-screen/%s" % CLIENT_VERSION)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw.decode()) if raw else None)
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return 304, None
        try:
            payload = json.loads(e.read().decode())
        except Exception:
            payload = None
        raise HTTPStatus(e.code, payload)


def download(path_or_url, dest, token=None, expect_sha=None, max_bytes=MAX_MEDIA_BYTES, timeout=60):
    """Stream to dest.part, verify SHA-256, then rename. Never leaves a partial file behind."""
    url = path_or_url if re.match(r"^https?://", path_or_url) else STATE.api_base + path_or_url
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "crewhex-screen/%s" % CLIENT_VERSION)
    if token:
        req.add_header("Authorization", "Bearer " + token)
    dest = Path(dest)
    part = dest.with_name(dest.name + ".part")
    h = hashlib.sha256()
    n = 0
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r, open(str(part), "wb") as f:
            while True:
                chunk = r.read(1 << 16)
                if not chunk:
                    break
                n += len(chunk)
                if n > max_bytes:
                    raise ValueError("download larger than %d bytes" % max_bytes)
                h.update(chunk)
                f.write(chunk)
    except urllib.error.HTTPError as e:
        unlink(part)
        raise HTTPStatus(e.code)
    except Exception:
        unlink(part)
        raise
    digest = h.hexdigest()
    if expect_sha and digest != expect_sha:
        unlink(part)
        raise ValueError("checksum mismatch for %s" % dest.name)
    os.replace(str(part), str(dest))
    return digest


# ============================================================ pairing
def reset_to_pairing(reason):
    log("reverting to pairing:", reason)
    with STATE.lock:
        STATE.device_token = None
        STATE.device_id = None
        STATE.reg = None
        STATE.legacy_pin = None
        STATE.manifest = None
    unlink(STATE_PATH)
    unlink(MANIFEST_PATH)
    shutil.rmtree(str(MEDIA_DIR), ignore_errors=True)


def adopt_token(token, device_id, name=None):
    with STATE.lock:
        STATE.device_token = token
        STATE.device_id = device_id
        STATE.paired_at = datetime.now(timezone.utc).isoformat()
        STATE.token_at = time.time()
        STATE.reg = None
        STATE.legacy_pin = None
        if name:
            STATE.display_name = name
    STATE.save_device()
    STATE.force_sync = True
    log("paired as device", device_id)


def pair_step():
    if STATE.v1:
        return legacy_pair_step()
    reg = STATE.reg
    if not reg or time.time() > reg["expires"]:
        try:
            _, r = api("POST", "/api/v1/hub-device/v2/register", {"player_kind": "pi", "client_version": CLIENT_VERSION})
        except HTTPStatus as e:
            if e.code in (404, 405):
                log("server has no v2 pairing - using v1")
                STATE.v1 = True
            else:
                STATE.last_error = "register http %s" % e.code
            return
        except Exception as e:
            STATE.last_error = str(e)[:120]
            return
        STATE.ok()
        exp = datetime.fromisoformat(r["expires_at"].replace("Z", "+00:00")).timestamp()
        with STATE.lock:
            STATE.reg = {"id": r["registration_id"], "code": r["pairing_code"], "secret": r["device_secret"],
                         "expires": exp, "expires_at": r["expires_at"]}
        return
    try:
        _, r = api("POST", "/api/v1/hub-device/v2/claim-status", {"registration_id": reg["id"], "device_secret": reg["secret"]})
        STATE.ok()
    except HTTPStatus as e:
        if e.code in (404, 409, 410):
            STATE.reg = None               # fresh code next cycle
        else:
            STATE.last_error = "claim-status http %s" % e.code
        return
    except Exception as e:
        STATE.last_error = str(e)[:120]
        return
    if r.get("status") == "paired" and r.get("device_token"):
        adopt_token(r["device_token"], r.get("display_id"), r.get("display_name"))


def legacy_pair_step():
    """v1 protocol, only so a v2 client still pairs against a not-yet-upgraded server."""
    if not STATE.legacy_pin:
        STATE.legacy_pin = "".join(secrets.choice(LEGACY_PIN_DIGITS) for _ in range(6))
        STATE.legacy_registered = False
    if not STATE.legacy_registered:
        try:
            api("POST", "/api/v1/hub-device/register", {"pin": STATE.legacy_pin})
            STATE.legacy_registered = True
            STATE.ok()
        except Exception as e:
            STATE.last_error = str(e)[:120]
            return
    try:
        _, r = api("GET", "/api/v1/hub-device/pair/" + STATE.legacy_pin)
        STATE.ok()
    except HTTPStatus as e:
        if e.code == 410:
            STATE.legacy_pin = None
        return
    except Exception as e:
        STATE.last_error = str(e)[:120]
        return
    if r and r.get("status") == "paired" and r.get("device_token"):
        adopt_token(r["device_token"], r.get("device_id"), r.get("display_name"))


# ============================================================ manifest + media
def page_image_file(key):
    return MEDIA_DIR / ("page-" + hashlib.sha256(key.encode()).hexdigest()[:40])


def media_file(sha):
    return MEDIA_DIR / sha


def manifest_page_keys(man):
    out = []
    for p in (man.get("pages") or {}).values():
        for b in p.get("blocks") or []:
            u = str(b.get("image_url") or "")
            if b.get("type") == "image" and u.startswith("/hub-device/v2/page-image?key="):
                k = urllib.parse.unquote(u.split("key=", 1)[1])
                if PAGE_KEY_RE.match(k):
                    out.append(k)
    return out


def fetch_assets(man):
    """Download everything the manifest references BEFORE the kiosk sees it."""
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    keep = set()
    for mid, m in (man.get("media") or {}).items():
        sha = str(m.get("sha256") or "")
        if not re.fullmatch(r"[0-9a-f]{64}", sha):
            continue
        keep.add(sha)
        f = media_file(sha)
        if f.exists():
            continue
        try:
            download("/api/v1" + m["url"], f, token=STATE.device_token, expect_sha=sha)
        except HTTPStatus as e:
            if e.code == 401:
                raise
            STATE.last_error = "media %s http %s" % (mid, e.code)
        except Exception as e:
            STATE.last_error = ("media %s: %s" % (mid, e))[:160]
    for key in manifest_page_keys(man):
        f = page_image_file(key)
        keep.add(f.name)
        if f.exists():
            continue
        try:
            download("/api/v1/hub-device/v2/page-image?key=" + urllib.parse.quote(key, safe="/"), f,
                     token=STATE.device_token, max_bytes=20 * 1024 * 1024)
        except HTTPStatus as e:
            if e.code == 401:
                raise
        except Exception as e:
            STATE.last_error = ("page image: %s" % e)[:160]
    for f in MEDIA_DIR.iterdir():                 # garbage-collect what's no longer referenced
        if f.name not in keep and not f.name.endswith(".part"):
            unlink(f)


def legacy_to_manifest(c):
    """v1 /hub-device/content -> v2 manifest shape (Hub pages only)."""
    pages = {}
    for p in c.get("pages") or []:
        if p.get("enabled") is False:
            continue
        cfg = p.get("published_config") or {}
        pages[p["page_key"]] = {"title": p.get("title", ""), "enabled": True, "sort_order": p.get("sort_order", 0),
                                "rotation_seconds": p.get("rotation_seconds") or 30, "blocks": cfg.get("blocks") or [],
                                "message": cfg.get("message") or cfg.get("body") or "", "items": cfg.get("items") or []}
    d = dict(c.get("display") or {})
    if d.get("after_hours_mode") == "content":
        d["after_hours_mode"] = "keep_playing"
    if c.get("screen_off"):
        d["status"] = "paused"
    man = {"schema": 2, "display": d, "branding": c.get("branding") or {}, "schedules": [], "overrides": [],
           "playlists": {}, "media": {}, "pages": pages}
    man["etag"] = "v1-" + hashlib.sha256(json.dumps(man, sort_keys=True, default=str).encode()).hexdigest()[:24]
    return man, (c.get("live") or {})


def sync_manifest():
    if STATE.v1:
        _, c = api("GET", "/api/v1/hub-device/content", token=STATE.device_token)
        STATE.ok()
        man, STATE.live = legacy_to_manifest(c or {})
    else:
        hdrs = {}
        if STATE.manifest and STATE.manifest.get("etag") and not STATE.force_sync:
            hdrs["If-None-Match"] = '"%s"' % STATE.manifest["etag"]
        try:
            code, man = api("GET", "/api/v1/hub-device/v2/manifest", token=STATE.device_token, headers=hdrs)
        except HTTPStatus as e:
            if e.code == 404:
                log("server has no v2 manifest - using v1 content")
                STATE.v1 = True
                return sync_manifest()
            raise
        STATE.ok()
        STATE.force_sync = False
        if code == 304:
            return False
        fetch_assets(man)
    if STATE.manifest and STATE.manifest.get("etag") == man.get("etag"):
        return False
    write_private(MANIFEST_PATH, man)
    with STATE.lock:
        STATE.manifest = man
    return True


def sync_live():
    if STATE.v1:
        return
    _, r = api("GET", "/api/v1/hub-device/v2/live", token=STATE.device_token)
    STATE.ok()
    STATE.live = (r or {}).get("live") or {}


def heartbeat():
    with STATE.lock:
        rep, STATE.report = STATE.report, {"plays": {}, "telemetry": {}}
    body = dict(rep["telemetry"])
    body.update({"client_version": CLIENT_VERSION, "uptime_s": int(time.time() - STATE.boot),
                 "manifest_etag": (STATE.manifest or {}).get("etag"), "plays": list(rep["plays"].values()),
                 "errors": ([STATE.last_error] if STATE.last_error else []) + list(body.get("errors") or [])})
    try:
        body["cache_bytes"] = sum(f.stat().st_size for f in MEDIA_DIR.iterdir()) if MEDIA_DIR.exists() else 0
    except Exception:
        pass
    if STATE.v1:
        api("POST", "/api/v1/hub-device/heartbeat", {"version_seen": body["manifest_etag"]}, token=STATE.device_token, timeout=8)
        STATE.ok()
        return legacy_update_check()
    try:
        _, r = api("POST", "/api/v1/hub-device/v2/heartbeat", body, token=STATE.device_token, timeout=8)
    except Exception:
        with STATE.lock:            # keep unsent proof-of-play for the next heartbeat
            for k, v in rep["plays"].items():
                cur = STATE.report["plays"].setdefault(k, dict(v, plays=0, seconds=0))
                cur["plays"] += v["plays"]
                cur["seconds"] += v["seconds"]
        raise
    STATE.ok()
    mark_update_good()
    for c in r.get("commands") or []:
        cmd = c.get("command")
        if cmd == "clear_cache":
            shutil.rmtree(str(MEDIA_DIR), ignore_errors=True)
            unlink(MANIFEST_PATH)
            STATE.manifest = None
            STATE.force_sync = True
            cmd = "reload"
        with STATE.lock:
            STATE.kiosk_commands.append({"id": c.get("id"), "command": cmd})
    if STATE.manifest and r.get("manifest_etag") and r["manifest_etag"] != STATE.manifest.get("etag"):
        STATE.force_sync = True
    upd = r.get("update") or {}
    if upd.get("version"):
        apply_update(upd["version"], "%s/public/screen-client-%s.tar.gz" % (STATE.api_base, upd["version"]))


def rotate_token():
    if STATE.v1 or time.time() - (STATE.token_at or 0) < ROTATE_TOKEN_AFTER:
        return
    _, r = api("POST", "/api/v1/hub-device/v2/rotate-token", token=STATE.device_token)
    with STATE.lock:
        STATE.device_token = r["device_token"]
        STATE.token_at = time.time()
    STATE.save_device()
    log("device token rotated")


# ============================================================ OTA
def legacy_update_check():
    try:
        _, r = api("GET", "/api/v1/hub-device/update-check?current=" + urllib.parse.quote(CLIENT_VERSION),
                   token=STATE.device_token, timeout=8)
    except Exception:
        return
    if r.get("update") and r.get("url"):
        apply_update(r["version"], r["url"])
    elif r.get("confirmed_current"):
        confirm_update()


def safe_members(t, root="client"):
    """Reject anything that could escape the staging dir. Bookworm ships Python
    3.11.2, which predates tarfile's extraction filters, so check by hand."""
    ok = []
    for m in t.getmembers():
        name = m.name[2:] if m.name.startswith("./") else m.name
        parts = Path(name).parts
        if name.startswith("/") or ".." in parts or not parts or parts[0] != root:
            raise ValueError("unsafe path in update: %r" % m.name)
        if not (m.isfile() or m.isdir()):
            raise ValueError("links/devices not allowed in update: %r" % m.name)
        if m.size > 50 * 1024 * 1024:
            raise ValueError("oversized file in update: %r" % m.name)
        m.mode = 0o755 if m.isdir() else 0o644
        ok.append(m)
    return ok


def verify_signature(tarball, sig):
    """ed25519 via the openssl CLI (the stdlib has no ed25519). Required whenever
    a public key ships with the client, which it does from 2.0.0."""
    if not PUBKEY_PATH.exists():
        log("WARNING: no OTA public key bundled - accepting update on SHA-256 only")
        return
    if not sig or not Path(sig).exists():
        raise ValueError("update is not signed")
    r = subprocess.run(["openssl", "pkeyutl", "-verify", "-pubin", "-inkey", str(PUBKEY_PATH), "-rawin",
                        "-in", str(tarball), "-sigfile", str(sig)], capture_output=True, text=True, timeout=30)
    if r.returncode != 0 or "Signature Verified Successfully" not in (r.stdout + r.stderr):
        raise ValueError("update signature check failed")


def apply_update(version, url, restart=True):
    if not VERSION_RE.match(str(version)) or version == CLIENT_VERSION:
        return False
    failed = load_json(APP_DIR / "current.failed.json", {})
    if failed.get("version") == version:          # it already crashed 3 times here - don't loop
        STATE.last_error = "update %s previously failed to start on this screen; push a newer build" % version
        return False
    api_host = urllib.parse.urlparse(STATE.api_base).netloc
    u = urllib.parse.urlparse(url)
    if u.netloc != api_host or (u.scheme != "https" and not api_host.startswith("127.0.0.1")):
        log("refusing update from", url)
        return False
    stage = APP_DIR / (version + ".staging")
    final = APP_DIR / version
    shutil.rmtree(str(stage), ignore_errors=True)
    stage.mkdir(parents=True)
    tarball = stage / "bundle.tar.gz"
    try:
        download(url + ".sha256", stage / "bundle.sha256", max_bytes=4096, timeout=20)
        expected = (stage / "bundle.sha256").read_text().split()[0].strip().lower()
        download(url, tarball, expect_sha=expected, max_bytes=100 * 1024 * 1024, timeout=120)
        sig = stage / "bundle.sig"
        try:
            download(url + ".sig", sig, max_bytes=4096, timeout=20)
        except Exception:
            sig = None
        verify_signature(tarball, sig)
        with tarfile.open(str(tarball)) as t:
            t.extractall(str(stage / "x"), members=safe_members(t))
        new = stage / "x" / "client"
        for need in ("screen_client.py", "kiosk/index.html", "VERSION"):
            if not (new / need).exists():
                raise ValueError("update bundle missing " + need)
        if (new / "VERSION").read_text().strip() != version:
            raise ValueError("bundle VERSION does not match the pushed version")
        shutil.rmtree(str(final), ignore_errors=True)
        os.replace(str(new), str(final))
        write_private(APP_DIR / "current.json", {"version": version, "path": str(final), "boots": 0,
                                                 "previous": CLIENT_VERSION, "installed_at": time.time()})
    except Exception as e:
        log("update to", version, "failed:", str(e)[:200])
        STATE.last_error = ("update %s failed: %s" % (version, e))[:160]
        return False
    finally:
        shutil.rmtree(str(stage), ignore_errors=True)
    st = load_json(STATE_PATH, {})
    st["updated_to"] = version
    write_private(STATE_PATH, st)
    log("installed", version, "- restarting; launch.py starts the new build")
    if restart:
        os._exit(0)                               # systemd Restart=always -> launch.py
    return True


def mark_update_good():
    cur = APP_DIR / "current.json"
    info = load_json(cur, None)
    if info and info.get("version") == CLIENT_VERSION and info.get("boots"):
        info["boots"] = 0
        write_private(cur, info)
    if load_json(STATE_PATH, {}).get("updated_to") == CLIENT_VERSION:
        confirm_update()


def confirm_update():
    try:
        api("POST", "/api/v1/hub-device/update-applied", {"version": CLIENT_VERSION}, token=STATE.device_token, timeout=5)
    except Exception:
        return
    st = load_json(STATE_PATH, {})
    st.pop("updated_to", None)
    write_private(STATE_PATH, st)


# ============================================================ worker
def worker():
    nxt = {"manifest": 0, "hb": 0, "live": 0, "rotate": 0}
    while True:
        try:
            if not STATE.device_token:
                pair_step()
                time.sleep(POLL_PAIRING)
                continue
            now = time.time()
            if STATE.force_sync or now >= nxt["manifest"]:
                nxt["manifest"] = now + POLL_MANIFEST
                sync_manifest()
            if now >= nxt["live"]:
                nxt["live"] = now + LIVE
                sync_live()
            if now >= nxt["hb"]:
                nxt["hb"] = now + HEARTBEAT
                heartbeat()
            if now >= nxt["rotate"]:
                nxt["rotate"] = now + 3600
                rotate_token()
        except HTTPStatus as e:
            if e.code == 401:
                reset_to_pairing("token rejected")
            else:
                STATE.last_error = "http %s" % e.code
        except Exception as e:
            STATE.last_error = str(e)[:160]
        time.sleep(1)


# ============================================================ local kiosk server
class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "crewhex-screen"
    sys_version = ""

    def log_message(self, fmt, *args):
        pass

    # ---- guards ----
    def _local_host(self):
        """DNS-rebinding guard: only answer requests addressed to loopback."""
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].lower()
        return host in ("127.0.0.1", "localhost")

    def _kiosk_call(self):
        """API calls must come from the kiosk page: a custom header (forces a CORS
        preflight we never grant) and, if the browser sends one, our own Origin."""
        if self.headers.get("X-CrewHex-Kiosk") != "1":
            return False
        origin = self.headers.get("Origin")
        port = self.server.server_address[1]
        return origin in (None, "http://127.0.0.1:%d" % port, "http://localhost:%d" % port)

    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(403)          # never grant a CORS preflight
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _wifi_allowed(self):
        return shutil.which("nmcli") is not None and (not STATE.device_token or STATE.link() == "red")

    # ---- routes ----
    def do_POST(self):
        if not self._local_host() or not self._kiosk_call():
            return self.send_json({"error": "forbidden"}, 403)
        n = min(int(self.headers.get("Content-Length") or 0), 256 * 1024)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self.send_json({"error": "bad body"}, 400)
        if self.path == "/api/report":
            with STATE.lock:
                tele = STATE.report["telemetry"]
                for k in ("mode", "current_item", "screen_w", "screen_h", "user_agent"):
                    if k in body:
                        tele[k] = body[k]
                if body.get("errors"):
                    tele["errors"] = [str(e)[:200] for e in body["errors"][-5:]]
                for p in (body.get("plays") or [])[:200]:
                    try:
                        key = "%s|%s" % (p["day"], p["ref"])
                        cur = STATE.report["plays"].setdefault(key, {"day": str(p["day"])[:10], "ref": str(p["ref"])[:80],
                                                                     "plays": 0, "seconds": 0})
                        cur["plays"] += int(p.get("plays", 0))
                        cur["seconds"] += int(p.get("seconds", 0))
                    except Exception:
                        continue
            return self.send_json({"ok": True, "commands": []})
        if self.path == "/api/wifi/connect":
            if not self._wifi_allowed():
                return self.send_json({"error": "Wi-Fi setup is only available while the screen is offline or unpaired"}, 403)
            ssid = str(body.get("ssid") or "").strip()[:64]
            pw = str(body.get("password") or "")[:128]
            if not ssid:
                return self.send_json({"error": "ssid required"}, 400)
            r = subprocess.run(["nmcli", "dev", "wifi", "connect", ssid] + (["password", pw] if pw else []),
                               capture_output=True, text=True, timeout=45)
            time.sleep(3)
            reachable = False
            try:
                api("GET", "/health", timeout=6)
                reachable = True
                STATE.ok()
            except Exception:
                pass
            return self.send_json({"ok": r.returncode == 0, "reachable": reachable})
        return self.send_json({"error": "not found"}, 404)

    def do_GET(self):
        if not self._local_host():
            return self.send_json({"error": "forbidden"}, 403)
        path = self.path.split("?", 1)[0]
        if path.startswith("/api/") and not self._kiosk_call():
            return self.send_json({"error": "forbidden"}, 403)
        if path == "/api/status":
            with STATE.lock:
                cmds, STATE.kiosk_commands = STATE.kiosk_commands, []
                reg = STATE.reg
                code = reg["code"] if reg else (STATE.legacy_pin and STATE.legacy_pin[:3] + " " + STATE.legacy_pin[3:])
                paired = bool(STATE.device_token)
                snap = {"mode": "content" if paired else "pairing",
                        "pairing_code": None if paired else code,
                        "pairing_expires_at": reg["expires_at"] if reg and not paired else None,
                        "api_base": STATE.api_base, "display_name": STATE.display_name,
                        "client_version": CLIENT_VERSION, "protocol": "v1" if STATE.v1 else "v2",
                        "last_ok": STATE.last_ok, "last_error": STATE.last_error, "link": STATE.link(),
                        "wifi_available": shutil.which("nmcli") is not None, "commands": cmds, "now": time.time()}
            return self.send_json(snap)
        if path == "/api/manifest":
            return self.send_json(STATE.manifest or {})
        if path == "/api/live":
            return self.send_json({"live": STATE.live})
        if path == "/api/wifi/scan":
            return self.wifi_scan()
        if path.startswith("/media/"):
            mid = urllib.parse.unquote(path[len("/media/"):])
            m = ((STATE.manifest or {}).get("media") or {}).get(mid)
            if not m or not re.fullmatch(r"[0-9a-f]{64}", str(m.get("sha256"))):
                return self.send_json({"error": "not found"}, 404)
            return self.send_file(media_file(m["sha256"]), m.get("content_type") or "application/octet-stream")
        if path == "/page-image":
            key = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("key", [""])[0]
            if not PAGE_KEY_RE.match(key):
                return self.send_json({"error": "not found"}, 404)
            ctype = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}[key.rsplit(".", 1)[1]]
            return self.send_file(page_image_file(key), ctype)
        if path in ("/", "/index.html"):
            return self.send_file(STATIC_DIR / "index.html", "text/html; charset=utf-8", cache=False)
        return self.send_json({"error": "not found"}, 404)       # nothing else is served

    def wifi_scan(self):
        if not self._wifi_allowed():
            return self.send_json({"available": False, "networks": []})
        try:
            subprocess.run(["nmcli", "dev", "wifi", "rescan"], capture_output=True, timeout=10)
        except Exception:
            pass
        r = subprocess.run(["nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY", "dev", "wifi", "list"],
                           capture_output=True, text=True, timeout=15)
        nets = {}
        for line in r.stdout.splitlines():
            parts = line.split(":")
            if len(parts) < 3 or not parts[0]:
                continue
            ssid, sig, sec = parts[0], parts[1], ":".join(parts[2:])
            if ssid not in nets or int(sig or 0) > int(nets[ssid]["signal"] or 0):
                nets[ssid] = {"ssid": ssid, "signal": sig, "secure": bool(sec and sec != "--")}
        return self.send_json({"available": True, "networks": sorted(nets.values(), key=lambda x: -int(x["signal"] or 0))[:12]})

    def send_file(self, f, ctype, cache=True):
        """Serve a local file with single-range support (Chromium seeks MP4s)."""
        f = Path(f)
        if not f.exists():
            return self.send_json({"error": "not cached yet"}, 404)
        size = f.stat().st_size
        start, end = 0, size - 1
        m = re.match(r"bytes=(\d*)-(\d*)$", self.headers.get("Range") or "")
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else size - 1
            else:
                start = max(0, size - int(m.group(2)))
            end = min(end, size - 1)
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", "bytes */%d" % size)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        else:
            self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Cache-Control", "private, max-age=31536000, immutable" if cache else "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        with open(str(f), "rb") as fh:
            fh.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = fh.read(min(1 << 16, left))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return
                left -= len(chunk)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def _handoff_to_installed_build():
    """A Pi whose 1.1.x updater copied this file straight into /opt runs it
    without launch.py. Behave like the launcher so later OTA builds (installed
    under /var/lib) actually start, with the same 3-strike rollback."""
    if os.environ.get("CHX_LAUNCHED") == "1":
        return
    os.environ["CHX_LAUNCHED"] = "1"
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("chx_launch", str(HERE / "launch.py"))
        if spec and (HERE / "launch.py").exists():
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            target = mod.pick()
        else:
            info = load_json(APP_DIR / "current.json", None)
            target = Path(info["path"]) / "screen_client.py" if info and int(info.get("boots", 0)) < 3 else Path(__file__)
            if info and target != Path(__file__):
                info["boots"] = int(info.get("boots", 0)) + 1
                write_private(APP_DIR / "current.json", info)
        if Path(target).resolve() != Path(__file__).resolve() and Path(target).exists():
            import sys
            log("handing off to installed build", target)
            os.execv(sys.executable, [sys.executable, str(target)] + sys.argv[1:])
    except Exception as e:
        log("launcher handoff skipped:", e)


def main():
    _handoff_to_installed_build()
    import argparse
    ap = argparse.ArgumentParser(description="CrewHex Screen Client")
    ap.add_argument("--port", type=int, default=STATE.port)
    args = ap.parse_args()
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=worker, daemon=True).start()
    with Server(("127.0.0.1", args.port), Handler) as httpd:
        log("v%s serving kiosk on http://127.0.0.1:%s (api: %s)" % (CLIENT_VERSION, httpd.server_address[1], STATE.api_base))
        httpd.serve_forever()


if __name__ == "__main__":
    main()
