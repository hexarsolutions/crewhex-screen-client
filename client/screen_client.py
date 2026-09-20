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
POLL_CONTENT = 20          # seconds between content polls
POLL_PAIRING = 3           # seconds between pairing-status polls
HEARTBEAT = 60             # seconds between heartbeats
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I


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
        self.pairing_code = None
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


def try_pair():
    """Poll the pairing endpoint until the tenant links this screen."""
    code = STATE.pairing_code
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
    return "-".join(
        "".join(secrets.choice(CODE_ALPHABET) for _ in range(3))
        for _ in range(2)
    )


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


def worker():
    # Restore or mint a pairing code on boot.
    with STATE.lock:
        STATE.pairing_code = new_code() if not STATE.device_token else None
    next_heartbeat = 0
    while True:
        if not STATE.device_token:
            try_pair()
            time.sleep(POLL_PAIRING)
            continue
        poll_content()
        if time.time() >= next_heartbeat:
            heartbeat()
            next_heartbeat = time.time() + HEARTBEAT
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

    def do_GET(self):
        with STATE.lock:
            snap = {
                "mode": "content" if STATE.device_token else "pairing",
                "api_base": STATE.api_base,
                "pairing_code": STATE.pairing_code,
                "display_name": STATE.display_name,
                "version": STATE.content_version,
                "content": STATE.content,
                "last_ok": STATE.last_ok,
                "last_error": STATE.last_error,
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
