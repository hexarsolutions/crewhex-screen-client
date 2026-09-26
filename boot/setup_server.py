#!/usr/bin/env python3
"""pi-screen setup server — first-boot chooser + WiFi join for the Display-Hub path.

Serves 127.0.0.1:8090. On first boot the kiosk opens this page; staff pick the
platform and (for Display-Hub) join site WiFi. The choice is written to
/etc/pi-screen/kiosk.conf and is sticky — a power cycle goes straight to content.

CrewHex screens delegate to CrewHex's own installer (this server just triggers it).
Loopback-only: reachable only from the Pi itself (the kiosk browser).
"""
import os, re, subprocess, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ETC = os.environ.get("PI_SCREEN_ETC", "/etc/pi-screen")
CFG = os.path.join(ETC, "kiosk.conf")
PLAYERS = {
    "crewhex": "https://app.crewhex.com/player/",
    "displayhub": "https://staff.hexar.co/display",
}
HTML = """<!doctype html><meta charset=utf-8><title>Screen setup</title>
<style>body{font-family:system-ui;background:#0b1020;color:#e8ecf3;display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
.box{text-align:center;max-width:560px}h1{font-size:44px;margin:.2em 0}.sub{color:#9aa7bd;margin-bottom:2em}
.btn{display:block;width:100%;box-sizing:border-box;margin:.6em 0;padding:1.1em;border-radius:12px;border:0;
font-size:20px;font-weight:700;cursor:pointer;color:#fff}a{text-decoration:none}
.crewhex{background:#0b7285}.displayhub{background:#dd6416}.note{color:#6c7a91;font-size:13px;margin-top:2em}
</style><div class=box><h1>Screen setup</h1><div class=sub>Choose the platform this display runs</div>
<a class="btn crewhex" href="/choose?p=crewhex">CrewHex screen</a>
<a class="btn displayhub" href="/choose?p=displayhub">Hexar Display-Hub screen</a>
<div class=note>Sticky after pairing — TV power cycles won't re-ask.</div></div>"""

class H(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _read(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n).decode() if n else ""

    def _conf(self):
        if os.path.exists(CFG):
            with open(CFG) as f:
                for line in f:
                    if line.startswith("platform="):
                        return line.strip().split("=", 1)[1]
        return "choose"

    def _set(self, val):
        with open(CFG, "w") as f:
            f.write("platform=%s\n" % val)

    def _render(self, body, code=200, ctype="text/html; charset=utf-8"):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            return self._render(HTML) if self._conf() == "choose" else self._redirect(PLAYERS.get(self._conf(), "/"))
        if path.startswith("/choose"):
            p = self.path.split("p=")[-1].strip()
            if p in PLAYERS:
                self._set(p)
                if p == "crewhex":
                    # trigger CrewHex installer in background, then send along
                    subprocess.Popen(["/etc/pi-screen/choose.sh", "crewhex"],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    return self._render("<html>Installing CrewHex client &hellip; refresh in ~30s.</html>")
                return self._redirect(PLAYERS[p])
        if path == "/wifi":
            return self._render(self._wifi_page(self._scan()))
        if path == "/health":
            return self._render("ok", ctype="text/plain")
        return self._render("not found", 404, "text/plain")

    def do_POST(self):
        body = self._read()
        if self.path.startswith("/wifi"):
            import json, urllib.parse
            d = json.loads(body) if body else {}
            ssid, psk = d.get("ssid"), d.get("pass")
            if not ssid:
                return self._render("ssid required", 400, "text/plain")
            ok = self._join(ssid, psk or "")
            return self._render("ok" if ok else "fail", 200 if ok else 500, "text/plain")
        return self._render("not found", 404, "text/plain")

    def _redirect(self, url):
        b = ('<html><meta http-equiv="refresh" content="0;url=%s"></html>' % url).encode()
        self.send_response(302)
        self.send_header("Location", url)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _join(self, ssid, psk):
        cmd = ["nmcli", "device", "wifi", "connect", ssid]
        if psk:
            cmd += ["password", psk]
        r = subprocess.run(cmd, capture_output=True, text=True)
        return r.returncode == 0

    def _scan(self):
        try:
            r = subprocess.run(["nmcli", "-t", "-f", "SSID,IN-USE", "device", "wifi", "list"],
                               capture_output=True, text=True)
            nets = [l.split(":")[0] for l in r.stdout.splitlines() if l.strip() and l.split(":")[0]]
            return sorted(set(nets))
        except Exception:
            return []

    def _wifi_page(self, nets):
        opts = "\n".join("<option>%s</option>" % n for n in nets if n)
        return """<!doctype html><meta charset=utf-8><title>WiFi</title>
<style>body{font-family:system-ui;background:#0b1020;color:#e8ecf3;display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
.box{width:420px;text-align:center}h2{font-size:34px;margin:.2em 0}.note{color:#9aa7bd;font-size:13px}
input,select{display:block;width:100%;box-sizing:border-box;margin:.6em 0;padding:.9em;border-radius:10px;border:1px solid #2b3550;background:#111830;color:#e8ecf3;font-size:16px}
button{width:100%;padding:.95em;border:0;border-radius:10px;background:#dd6416;color:#fff;font-size:18px;font-weight:700;cursor:pointer}
</style><div class=box><h2>Join site WiFi</h2><div class=note>Needed once — the display remembers it.</div>
<select id=s>__OPTS__</select><input id=p type=password placeholder="Password (if any)">
<button onclick="go()">Connect</button><div id=m></div>
<script>
async function go(){const r=await fetch('/wifi',{method:'POST',headers:{'Content-Type':'application/json'},
body:JSON.stringify({ssid:document.getElementById('s').value,pass:document.getElementById('p').value})});
document.getElementById('m').textContent = r.ok?'Connected ✓ returning to setup...':'Could not connect — try again.';
if(r.ok) setTimeout(()=>location='/',1200);}
</script></div>""".replace("__OPTS__", opts)

def main():
    try:
        ThreadingHTTPServer(("127.0.0.1", int(os.environ.get("PI_SCREEN_PORT", "8090"))), H).serve_forever()
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()