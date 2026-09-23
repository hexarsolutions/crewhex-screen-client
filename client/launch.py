#!/usr/bin/env python3
"""systemd starts this, not screen_client.py.

/opt/crewhex-screen is read-only under ProtectSystem=strict, so OTA builds are
installed to /var/lib/crewhex-screen/app/<version>/ and recorded in
app/current.json. This launcher runs that build, and falls back to the build
bundled in /opt if the OTA build fails to come up three times in a row
(screen_client.py resets the counter after its first good heartbeat).
"""
import json, os, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE_DIR = Path(os.environ.get("SCREEN_STATE", "/var/lib/crewhex-screen/device.json")).parent
CURRENT = STATE_DIR / "app" / "current.json"
MAX_BOOTS = 3


def pick():
    try:
        info = json.loads(CURRENT.read_text())
        target = Path(info["path"]) / "screen_client.py"
        app_root = (STATE_DIR / "app").resolve()
        if not str(target.resolve()).startswith(str(app_root) + os.sep) or not target.exists():
            raise ValueError("bad path")
        if int(info.get("boots", 0)) >= MAX_BOOTS:
            print("[launch] OTA build %s failed to start %d times - rolling back to bundled build"
                  % (info.get("version"), MAX_BOOTS), flush=True)
            CURRENT.rename(CURRENT.with_suffix(".failed.json"))
            raise ValueError("rolled back")
        info["boots"] = int(info.get("boots", 0)) + 1
        tmp = CURRENT.with_suffix(".tmp")
        tmp.write_text(json.dumps(info))
        os.chmod(str(tmp), 0o600)
        os.replace(str(tmp), str(CURRENT))
        return target
    except Exception:
        return HERE / "screen_client.py"


if __name__ == "__main__":
    target = pick()
    os.environ["CHX_LAUNCHED"] = "1"
    print("[launch] starting", target, flush=True)
    os.execv(sys.executable, [sys.executable, str(target)] + sys.argv[1:])
