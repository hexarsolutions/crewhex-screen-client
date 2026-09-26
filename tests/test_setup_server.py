"""Tests for the first-boot setup server (platform chooser + wifi).

The server is pure stdlib and loopback-only, so it tests without a Pi.
"""
import http.client, importlib.util, os, socket, threading
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parent.parent
SETUP = ROOT / "boot" / "setup_server.py"


def _load():
    spec = importlib.util.spec_from_file_location("setup_server_under_test", SETUP)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def srv(tmp_path):
    """Boot the setup server on a random port against a temp config dir."""
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    os.environ["PI_SCREEN_ETC"] = str(tmp_path)
    os.environ["PI_SCREEN_PORT"] = str(port)
    mod = _load()
    server = mod.ThreadingHTTPServer(("127.0.0.1", port), mod.H)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def get(path):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("GET", path)
        r = c.getresponse()
        return r.status, r.getheader("Location"), r.read().decode(), dict(r.getheaders())

    yield get, tmp_path
    server.shutdown(); server.server_close()
    os.environ.pop("PI_SCREEN_ETC", None); os.environ.pop("PI_SCREEN_PORT", None)


def test_chooser_shows_both_platforms(srv):
    get, _ = srv
    status, _, body, _ = get("/")
    assert status == 200
    assert "CrewHex screen" in body and "Display-Hub screen" in body


def test_choose_displayhub_is_sticky_and_redirects_to_player(srv):
    get, tmp = srv
    status, loc, _, _ = get("/choose?p=displayhub")
    assert status == 302 and loc == "https://staff.hexar.co/display"
    assert (tmp / "kiosk.conf").read_text().strip() == "platform=displayhub"
    # a later " / " now goes straight to the player, no chooser
    status, loc, _, _ = get("/")
    assert status == 302 and loc == "https://staff.hexar.co/display"


def test_health_and_wifi_page(srv):
    get, _ = srv
    assert get("/health")[2] == "ok"
    status, _, body, _ = get("/wifi")
    assert status == 200 and "Join site WiFi" in body