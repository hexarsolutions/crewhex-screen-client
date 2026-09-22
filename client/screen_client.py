#!/usr/bin/env python3
"""CrewHex Screen Client.

Runs on a Raspberry Pi (or any Linux box). Two modes:

  pairing  - shows a pairing code; polls the CrewHex API until a tenant
             links this screen from the web app.
  content  - polls published Tenant Hub pages and serves them to a local
             kiosk page rendered fullscreen by Chromium.

Zero third-party dependencies (stdlib only) so the install is trivial on
a fresh Raspberry Pi OS image.
"""
import http.server
import json
import os
import secrets
import socketserver
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

CONFIG_PATH = os.environ.get("SCREEN_CONFIG", "/etc/crewhex-screen/config.json")
STATE_PATH = os.environ.get("SCREEN_STATE", "/var/lib/crewhex-screen/device.json")
STATIC_DIR = Path(__file__).resolve().parent / "kiosk"
VERSION_PATH = Path(__file__).resolve().parent / "VERSION"
CLIENT_VERSION = (VERSION_PATH.read_text().strip() if VERSION_PATH.exists() else "dev")
POLL_CONTENT = 20          # seconds between content polls
POLL_PAIRING = 3           # seconds between pairing-status polls
HEARTBEAT = 60             # seconds between heartbeats
# Pairing PIN: 6 digits, no 0/1. Tenant admin enters their business login
# name (e.g. hexarsolutions) + this PIN in CrewHex > Screens.
PIN_LENGTH = 6
PIN_DIGITS = "23456789"


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return default


def save_json(path, data):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, indent=2))


class State:
    """Shared mutable state guarded by a lock; read by the HTTP thread."""

    def __init__(self):
        self.lock = threading.Lock()
        cfg = load_json(CONFIG_PATH, {})
        self.api_base = (cfg.get("api_base") or "https://api.crewhex.com").rstrip("/")
        self.display_name = cfg.get("display_name") or "Screen"
        self.port = int(cfg.get("port", 8080))
        st = load_json(STATE_PATH, {})
        self.device_token = st.get("device_token")
        self.device_id = st.get("device_id")
        self.pairing_code = None      # 6-digit pairing PIN (str when pairing)
        self.content = None          # last successful content payload
        self.content_version = None
        self.last_ok = None          # epoch of last successful API contact
        self.last_error = None
        self.paired_at = st.get("paired_at")


STATE = State()


def api_request(method, path, body=None, token=None, timeout=8):
    url = STATE.api_base + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def register_pin():
    """Register this boot's PIN so a tenant can claim it."""
    try:
        api_request("POST", "/api/v1/hub-device/register", {"pin": STATE.pairing_code})
        STATE.last_ok = time.time()
        STATE.last_error = None
        return True
    except Exception as e:
        STATE.last_error = str(e)[:120]
        return False


def try_pair():
    """Poll the pairing endpoint until the tenant links this screen."""
    code = STATE.pairing_code
    if not getattr(STATE, "pin_registered", False):
        if not register_pin():
            return
        STATE.pin_registered = True
    
    try:
        r = api_request("GET", "/api/v1/hub-device/pair/" + code)
        STATE.last_ok = time.time()
        STATE.last_error = None
    except urllib.error.HTTPError as e:
        if e.code == 410:          # code expired -> mint a fresh one
            with STATE.lock:
                STATE.pairing_code = new_code()
        else:
            STATE.last_error = "pair http %s" % e.code
        return
    except Exception as e:
        STATE.last_error = str(e)[:120]
        return
    if r.get("status") == "paired" and r.get("device_token"):
        with STATE.lock:
            STATE.device_token = r["device_token"]
            STATE.device_id = r.get("device_id")
            STATE.paired_at = datetime.now(timezone.utc).isoformat()
            STATE.pairing_code = None
        save_json(STATE_PATH, {
            "device_token": STATE.device_token,
            "device_id": STATE.device_id,
            "paired_at": STATE.paired_at,
            "display_name": r.get("display_name") or STATE.display_name,
        })
        print("[screen] paired as device", STATE.device_id, flush=True)


def new_code():
    return "".join(secrets.choice(PIN_DIGITS) for _ in range(PIN_LENGTH))


def poll_content():
    try:
        r = api_request("GET", "/api/v1/hub-device/content", token=STATE.device_token)
        STATE.last_ok = time.time()
        STATE.last_error = None
        with STATE.lock:
            STATE.content = r
            STATE.content_version = r.get("version")
    except urllib.error.HTTPError as e:
        if e.code == 401:
            # token revoked/unknown -> drop back to pairing
            with STATE.lock:
                STATE.device_token = None
                STATE.device_id = None
                STATE.pairing_code = new_code()
            Path(STATE_PATH).unlink(missing_ok=True)
            print("[screen] token rejected - reverting to pairing", flush=True)
        else:
            STATE.last_error = "content http %s" % e.code
    except Exception as e:
        STATE.last_error = str(e)[:120]


def heartbeat():
    try:
        api_request("POST", "/api/v1/hub-device/heartbeat",
                    body={"version_seen": STATE.content_version},
                    token=STATE.device_token, timeout=5)
    except Exception:
        pass  # heartbeat failures are non-fatal; content poll reports errors


# ---- OTA: tenant pushes a new client version from Tenant Hub ----
def check_update():
    try:
        from urllib.parse import quote
        r = api_request("GET", "/api/v1/hub-device/update-check?current="
                        + quote(CLIENT_VERSION), token=STATE.device_token, timeout=8)
    except Exception:
        return
    if r.get("update") and r.get("url"):
        apply_update(r["url"], r["version"])
    elif r.get("confirmed_current"):
        confirm_update()   # already running the pushed version — clear the flag


def apply_update(url, version):
    """Download the pushed client bundle, swap it in, restart. The screen is
    offline for a few seconds while systemd relaunches the service."""
    import shutil, subprocess, tarfile, urllib.request as _req
    tmp = Path(STATE_PATH).parent / "crewhex-update"
    shutil.rmtree(tmp, ignore_errors=True); tmp.mkdir(parents=True, exist_ok=True)
    tarball = tmp / "client.tar.gz"
    try:
        import socket
        socket.setdefaulttimeout(20)
        _req.urlretrieve(url, tarball)
        socket.setdefaulttimeout(None)
        with tarfile.open(tarball) as t:
            t.extractall(tmp)
    except Exception as e:
        socket.setdefaulttimeout(None)
        print("[screen] update download failed:", str(e)[:160], flush=True)
        return
    dest = Path(__file__).resolve().parent
    src = tmp / "client"
    swapped = False
    for rel in ("screen_client.py", "kiosk/index.html"):
        s = src / rel
        if s.exists():
            shutil.copy2(s, dest / rel); swapped = True
    if not swapped:
        print("[screen] update bundle missing expected files", flush=True)
        return
    (dest / "VERSION").write_text(version + "\n")
    st = load_json(STATE_PATH, {})
    st["updated_to"] = version
    save_json(STATE_PATH, st)
    print("[screen] updated client to", version, "- restarting service", flush=True)
    subprocess.Popen(["systemctl", "restart", "crewhex-screen"])
    os._exit(0)  # systemd brings the new version straight back up


def confirm_update():
    """Tell the server the pushed version is running, clearing the flag."""
    try:
        api_request("POST", "/api/v1/hub-device/update-applied", {"version": CLIENT_VERSION},
                    token=STATE.device_token, timeout=5)
    except Exception:
        pass
    st = load_json(STATE_PATH, {})
    st.pop("updated_to", None)
    save_json(STATE_PATH, st)


def worker():
    # Restore or mint a pairing code on boot.
    with STATE.lock:
        STATE.pairing_code = new_code() if not STATE.device_token else None
    if STATE.device_token and load_json(STATE_PATH, {}).get("updated_to"):
        confirm_update()   # finishing an OTA that restarted us
    next_heartbeat = 0
    while True:
        try:
            if not STATE.device_token:
                try_pair()
                time.sleep(POLL_PAIRING)
                continue
            check_update()
            poll_content()
            if time.time() >= next_heartbeat:
                heartbeat()
                next_heartbeat = time.time() + HEARTBEAT
            time.sleep(POLL_CONTENT)
        except Exception as e:
            print("[screen] worker cycle error:", str(e)[:160], flush=True)
            time.sleep(POLL_CONTENT)


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(STATIC_DIR), **kw)

    def log_message(self, fmt, *args):  # quiet
        pass

    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ---- Wi-Fi setup (no Ethernet case) ----
    def _wifi_available(self):
        import shutil
        return shutil.which("nmcli") is not None

    def do_POST(self):
        if self.path == "/api/wifi/connect" and self._wifi_available():
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                return self.send_json({"error": "bad body"}, 400)
            ssid = str(body.get("ssid") or "").strip()
            pw = str(body.get("password") or "")
            if not ssid:
                return self.send_json({"error": "ssid required"}, 400)
            cmd = ["nmcli", "dev", "wifi", "connect", ssid]
            if pw:
                cmd += ["password", pw]
            import subprocess as sp
            r = sp.run(cmd, capture_output=True, text=True, timeout=45)
            ok = r.returncode == 0 and "successfully" in (r.stdout + r.stderr).lower()
            # let NM settle, then probe the API
            time.sleep(3)
            reachable = False
            try:
                api_request("GET", "/health", timeout=6)
                reachable = True
            except Exception:
                pass
            return self.send_json({"ok": ok, "reachable": reachable,
                                   "detail": (r.stdout + r.stderr).strip()[-200:]})
        return self.send_json({"error": "not found"}, 404)

    def do_GET(self):
        if self.path == "/api/wifi/scan":
            if not self._wifi_available():
                return self.send_json({"available": False, "networks": []})
            import subprocess as sp
            try:
                sp.run(["nmcli", "dev", "wifi", "rescan"], capture_output=True, timeout=10)
            except Exception:
                pass
            r = sp.run(["nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY", "dev", "wifi", "list"],
                       capture_output=True, text=True, timeout=15)
            nets = {}
            for line in r.stdout.splitlines():
                parts = line.split(":")
                if len(parts) < 3 or not parts[0]:
                    continue
                ssid, sig, sec = parts[0], parts[1], ":".join(parts[2:])
                if ssid not in nets or int(sig or 0) > int(nets[ssid]["signal"] or 0):
                    nets[ssid] = {"ssid": ssid, "signal": sig,
                                  "secure": bool(sec and sec != "--")}
            out = sorted(nets.values(), key=lambda x: -int(x["signal"] or 0))[:12]
            return self.send_json({"available": True, "networks": out})
        with STATE.lock:
            age = (time.time() - STATE.last_ok) if STATE.last_ok else 1e9
            snap = {
                "mode": "content" if STATE.device_token else "pairing",
                "api_base": STATE.api_base,
                "pairing_pin": STATE.pairing_code,
                "display_name": STATE.display_name,
                "version": STATE.content_version,
                "content": STATE.content,
                "last_ok": STATE.last_ok,
                "last_error": STATE.last_error,
                "link": "green" if age < 90 else ("amber" if age < 300 else "red"),
                "now": time.time(),
            }
        if self.path == "/api/status":
            return self.send_json({k: v for k, v in snap.items() if k != "content"})
        if self.path == "/api/content":
            return self.send_json(snap["content"] or {"pages": []})
        return super().do_GET()


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    import argparse
    ap = argparse.ArgumentParser(description="CrewHex Screen Client")
    ap.add_argument("--port", type=int, default=STATE.port)
    args = ap.parse_args()
    threading.Thread(target=worker, daemon=True).start()
    with Server(("127.0.0.1", args.port), Handler) as httpd:
        print("[screen] serving kiosk on http://127.0.0.1:%s (api: %s)"
              % (args.port, STATE.api_base), flush=True)
        httpd.serve_forever()


if __name__ == "__main__":
    main()
