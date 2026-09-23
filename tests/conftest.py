import importlib, json, os, sys, threading, http.client
from pathlib import Path
import pytest
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "client")); sys.path.insert(0, str(Path(__file__).parent))
from fake_server import Fake


@pytest.fixture()
def env(tmp_path):
    fake = Fake(public_dir=tmp_path / "public").start()
    (tmp_path / "public").mkdir()
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"api_base": "http://127.0.0.1:%d" % fake.port, "port": 0}))
    os.environ["SCREEN_CONFIG"] = str(cfg)
    os.environ["SCREEN_STATE"] = str(tmp_path / "state" / "device.json")
    sys.modules.pop("screen_client", None)
    sc = importlib.import_module("screen_client")
    yield sc, fake, tmp_path
    fake.stop()


@pytest.fixture()
def local(env):
    """The client's own 127.0.0.1 server, for guard tests."""
    sc, fake, tmp = env
    srv = sc.Server(("127.0.0.1", 0), sc.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]

    def req(method, path, headers=None, body=None, host=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        h = {"Host": host or "127.0.0.1:%d" % port}
        h.update(headers or {})
        c.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
        r = c.getresponse(); data = r.read()
        return r.status, data, dict(r.getheaders())
    yield sc, fake, tmp, req, port
    srv.shutdown(); srv.server_close()
