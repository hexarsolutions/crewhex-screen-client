"""Minimal stand-in for the CrewHex device API (v2 + the v1 endpoints the client
falls back to). Stdlib only so this public repo's CI needs nothing private."""
import hashlib, json, threading, http.server, socketserver, urllib.parse
from pathlib import Path

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 120


class Fake:
    def __init__(self, public_dir=None):
        self.v1 = False
        self.claimed = False
        self.issued = False
        self.tokens = {"chx_dev_T1"}
        self.media = {"m1": PNG}
        self.tamper = False
        self.commands = []
        self.update = None
        self.heartbeats = []
        self.public_dir = Path(public_dir) if public_dir else None
        self.manifest = {
            "schema": 2, "display": {"name": "Lobby", "timezone": "Australia/Brisbane", "always_on": True, "status": "online"},
            "branding": {"name": "Acme"}, "schedules": [], "overrides": [],
            "playlists": {}, "media": {"m1": {"kind": "image", "content_type": "image/png", "bytes": len(PNG),
                                              "sha256": hashlib.sha256(PNG).hexdigest(), "url": "/hub-device/v2/media/m1"}},
            "pages": {}, "etag": "e1"}

    def start(self):
        fake = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a): pass

            def _json(self, obj, code=200, headers=None):
                b = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                for k, v in (headers or {}).items(): self.send_header(k, v)
                self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

            def _auth(self):
                tok = (self.headers.get("Authorization") or "")[7:]
                if tok not in fake.tokens:
                    self._json({"detail": "no"}, 401); return False
                return True

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n) or b"{}")

            def do_GET(self):
                p = urllib.parse.urlparse(self.path)
                if p.path == "/health": return self._json({"ok": True})
                if p.path.startswith("/public/") and fake.public_dir:
                    f = fake.public_dir / p.path[len("/public/"):]
                    if f.exists():
                        b = f.read_bytes(); self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b); return
                    return self._json({}, 404)
                if fake.v1 and "/v2/" in p.path: return self._json({}, 404)
                if p.path == "/api/v1/hub-device/v2/manifest":
                    if not self._auth(): return
                    et = '"%s"' % fake.manifest["etag"]
                    if self.headers.get("If-None-Match") == et:
                        self.send_response(304); self.send_header("ETag", et); self.end_headers(); return
                    return self._json(fake.manifest, headers={"ETag": et})
                if p.path.startswith("/api/v1/hub-device/v2/media/"):
                    if not self._auth(): return
                    b = fake.media.get(p.path.rsplit("/", 1)[1])
                    if b is None: return self._json({}, 404)
                    if fake.tamper: b = b + b"x"
                    self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b); return
                if p.path == "/api/v1/hub-device/v2/live":
                    if not self._auth(): return
                    return self._json({"live": {"fuel_dollars": 7}})
                if p.path == "/api/v1/hub-device/content":
                    if not self._auth(): return
                    return self._json({"version": "x", "display": {"name": "Old", "after_hours_mode": "content"},
                                       "pages": [{"page_key": "announcement", "title": "A", "enabled": True, "rotation_seconds": 20,
                                                  "published_config": {"message": "hello"}}], "live": {"fuel_dollars": 3}})
                if p.path.startswith("/api/v1/hub-device/pair/"):
                    return self._json({"status": "paired", "device_token": "chx_dev_T1", "device_id": "d1"} if fake.claimed else {"status": "pending"})
                if p.path.startswith("/api/v1/hub-device/update-check"):
                    return self._json({"update": False})
                return self._json({}, 404)

            def do_POST(self):
                p = urllib.parse.urlparse(self.path).path
                body = self._body()
                if fake.v1 and "/v2/" in p: return self._json({}, 404)
                if p == "/api/v1/hub-device/v2/register":
                    return self._json({"registration_id": "r1", "pairing_code": "K7M 4PX", "device_secret": "S3CRET",
                                       "expires_at": "2099-01-01T00:00:00+00:00", "poll_seconds": 3})
                if p == "/api/v1/hub-device/v2/claim-status":
                    if body.get("device_secret") != "S3CRET": return self._json({}, 404)
                    if fake.issued: return self._json({}, 409)
                    if not fake.claimed: return self._json({"status": "pending"})
                    fake.issued = True
                    return self._json({"status": "paired", "device_token": "chx_dev_T1", "display_id": "d1", "display_name": "Lobby"})
                if p == "/api/v1/hub-device/v2/heartbeat":
                    if not self._auth(): return
                    fake.heartbeats.append(body)
                    cmds, fake.commands = fake.commands, []
                    return self._json({"ok": True, "commands": cmds, "manifest_etag": fake.manifest["etag"], "update": fake.update})
                if p == "/api/v1/hub-device/v2/rotate-token":
                    if not self._auth(): return
                    fake.tokens.add("chx_dev_T2"); return self._json({"device_token": "chx_dev_T2"})
                if p in ("/api/v1/hub-device/register", "/api/v1/hub-device/heartbeat", "/api/v1/hub-device/update-applied"):
                    return self._json({"ok": True})
                return self._json({}, 404)

        class S(socketserver.ThreadingTCPServer):
            allow_reuse_address = True; daemon_threads = True
        self.httpd = S(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def stop(self):
        self.httpd.shutdown(); self.httpd.server_close()
