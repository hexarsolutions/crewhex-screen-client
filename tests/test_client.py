import hashlib, io, json, os, stat, subprocess, tarfile, time, shutil
from pathlib import Path
import pytest

K = {"X-CrewHex-Kiosk": "1"}


def pair(sc, fake):
    sc.pair_step()                      # register
    assert sc.STATE.reg["code"] == "K7M 4PX"
    sc.pair_step()                      # pending
    assert sc.STATE.device_token is None
    fake.claimed = True
    sc.pair_step()                      # paired
    assert sc.STATE.device_token == "chx_dev_T1"


def test_pairing_v2_and_token_file_is_private(env):
    sc, fake, tmp = env
    pair(sc, fake)
    st = sc.STATE_PATH.stat()
    assert stat.S_IMODE(st.st_mode) == 0o600
    assert json.loads(sc.STATE_PATH.read_text())["device_token"] == "chx_dev_T1"
    sc.reset_to_pairing("test"); pair_again = sc.STATE.device_token
    assert pair_again is None and not sc.STATE_PATH.exists()


def test_wrong_secret_never_gets_token(env):
    sc, fake, tmp = env
    sc.pair_step(); sc.STATE.reg["secret"] = "guess"; fake.claimed = True
    sc.pair_step()
    assert sc.STATE.device_token is None and sc.STATE.reg is None     # 404 -> fresh registration


def test_manifest_downloads_verified_media_then_304(env):
    sc, fake, tmp = env
    pair(sc, fake)
    assert sc.sync_manifest() is True
    f = sc.media_file(fake.manifest["media"]["m1"]["sha256"])
    assert f.exists() and f.read_bytes() == fake.media["m1"]
    assert stat.S_IMODE(sc.MANIFEST_PATH.stat().st_mode) == 0o600
    assert sc.sync_manifest() is False                 # If-None-Match -> 304


def test_tampered_media_is_rejected_and_not_cached(env):
    sc, fake, tmp = env
    pair(sc, fake); fake.tamper = True
    sc.sync_manifest()
    assert not sc.media_file(fake.manifest["media"]["m1"]["sha256"]).exists()
    assert "checksum" in (sc.STATE.last_error or "")
    assert not list(sc.MEDIA_DIR.glob("*.part"))


def test_unreferenced_media_is_garbage_collected(env):
    sc, fake, tmp = env
    pair(sc, fake); sc.sync_manifest()
    (sc.MEDIA_DIR / ("0" * 64)).write_bytes(b"old")
    fake.manifest["etag"] = "e2"; sc.sync_manifest()
    assert not (sc.MEDIA_DIR / ("0" * 64)).exists()


def test_401_drops_back_to_pairing_and_wipes_cache(env):
    sc, fake, tmp = env
    pair(sc, fake); sc.sync_manifest()
    fake.tokens.clear()
    with pytest.raises(sc.HTTPStatus):
        sc.sync_manifest()
    sc.reset_to_pairing("401")
    assert not sc.MEDIA_DIR.exists() and not sc.MANIFEST_PATH.exists() and sc.STATE.manifest is None


def test_heartbeat_delivers_commands_and_plays(env):
    sc, fake, tmp = env
    pair(sc, fake); sc.sync_manifest()
    sc.STATE.report["plays"]["2026-09-23|x"] = {"day": "2026-09-23", "ref": "x", "plays": 2, "seconds": 20}
    fake.commands = [{"id": "c1", "command": "identify"}, {"id": "c2", "command": "clear_cache"}]
    sc.heartbeat()
    assert fake.heartbeats[-1]["plays"][0]["plays"] == 2 and fake.heartbeats[-1]["client_version"] == sc.CLIENT_VERSION
    assert [c["command"] for c in sc.STATE.kiosk_commands] == ["identify", "reload"]
    assert sc.STATE.force_sync and not sc.MEDIA_DIR.exists()


def test_v1_server_fallback_keeps_screen_working(env):
    sc, fake, tmp = env
    fake.v1 = True
    sc.pair_step()                                       # v2 404 -> v1
    assert sc.STATE.v1
    sc.pair_step(); fake.claimed = True; sc.pair_step()
    assert sc.STATE.device_token == "chx_dev_T1"
    assert sc.sync_manifest() is True
    m = sc.STATE.manifest
    assert m["pages"]["announcement"]["message"] == "hello" and m["display"]["after_hours_mode"] == "keep_playing"
    sc.heartbeat()


def test_v1_token_survives_upgrade(env):
    """A 1.1.x device.json has only device_token/device_id; 2.0 must reuse it."""
    sc, fake, tmp = env
    sc.STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    sc.STATE_PATH.write_text(json.dumps({"device_token": "chx_dev_T1", "device_id": "d1"}))
    import importlib, sys
    sys.modules.pop("screen_client"); sc2 = importlib.import_module("screen_client")
    assert sc2.STATE.device_token == "chx_dev_T1"
    assert sc2.sync_manifest() is True


# ---------------------------------------------------------------- local server guards
def test_local_api_requires_kiosk_header_and_local_host(local):
    sc, fake, tmp, req, port = local
    assert req("GET", "/api/status")[0] == 403                        # no header
    assert req("GET", "/api/status", K)[0] == 200
    assert req("GET", "/api/status", K, host="evil.example")[0] == 403  # DNS rebinding
    assert req("GET", "/api/status", dict(K, Origin="https://evil.example"))[0] == 403
    assert req("OPTIONS", "/api/wifi/connect")[0] == 403              # preflight never granted
    assert req("GET", "/../config.json", K)[0] == 404
    assert req("GET", "/kiosk/index.html")[0] == 404


def test_status_never_exposes_token(local):
    sc, fake, tmp, req, port = local
    pair(sc, fake)
    code, body, _ = req("GET", "/api/status", K)
    assert b"chx_dev" not in body and b"S3CRET" not in body
    assert json.loads(body)["mode"] == "content"
    assert b"chx_dev" not in req("GET", "/api/manifest", K)[1]


def test_wifi_connect_blocked_when_paired_and_online(local, monkeypatch):
    sc, fake, tmp, req, port = local
    monkeypatch.setattr(sc.shutil, "which", lambda n: "/usr/bin/nmcli")
    calls = []
    monkeypatch.setattr(sc.subprocess, "run", lambda *a, **k: calls.append(a) or type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})())
    pair(sc, fake); sc.STATE.ok()
    assert req("POST", "/api/wifi/connect", K, {"ssid": "evil"})[0] == 403
    assert req("POST", "/api/wifi/connect", {"Content-Type": "text/plain"}, {"ssid": "evil"})[0] == 403
    assert not calls


def test_media_served_with_ranges(local):
    sc, fake, tmp, req, port = local
    pair(sc, fake); sc.sync_manifest()
    code, body, h = req("GET", "/media/m1", {"Range": "bytes=0-7"})
    assert code == 206 and body == fake.media["m1"][:8] and h["Content-Range"].startswith("bytes 0-7/")
    assert req("GET", "/media/nope")[0] == 404


# ---------------------------------------------------------------- OTA
def make_bundle(tmp, version, extra=None, bad_version=None):
    src = tmp / ("b-" + version) / "client"; (src / "kiosk").mkdir(parents=True)
    (src / "screen_client.py").write_text("print('new')\n")
    (src / "kiosk" / "index.html").write_text("<html></html>")
    (src / "VERSION").write_text((bad_version or version) + "\n")
    out = tmp / "public" / ("screen-client-%s.tar.gz" % version)
    with tarfile.open(out, "w:gz") as t:
        t.add(src, arcname="client")
        for name, data in (extra or {}).items():
            ti = tarfile.TarInfo(name); ti.size = len(data); t.addfile(ti, io.BytesIO(data))
    (tmp / "public" / (out.name + ".sha256")).write_text(hashlib.sha256(out.read_bytes()).hexdigest() + "  x\n")
    return out


def url(sc, v): return "%s/public/screen-client-%s.tar.gz" % (sc.STATE.api_base, v)


def test_ota_installs_to_state_dir_not_opt(env):
    sc, fake, tmp = env
    make_bundle(tmp, "9.9.9")
    assert sc.apply_update("9.9.9", url(sc, "9.9.9"), restart=False)
    info = json.loads((sc.APP_DIR / "current.json").read_text())
    assert info["version"] == "9.9.9" and (Path(info["path"]) / "screen_client.py").exists()


def test_ota_rejects_path_traversal(env):
    sc, fake, tmp = env
    make_bundle(tmp, "9.9.8", extra={"../../evil.sh": b"rm -rf /"})
    assert not sc.apply_update("9.9.8", url(sc, "9.9.8"), restart=False)
    assert not (tmp / "evil.sh").exists() and not (sc.APP_DIR / "current.json").exists()


def test_ota_rejects_checksum_mismatch_bad_version_and_foreign_host(env):
    sc, fake, tmp = env
    b = make_bundle(tmp, "9.9.7"); b.write_bytes(b.read_bytes() + b"x")
    assert not sc.apply_update("9.9.7", url(sc, "9.9.7"), restart=False)
    make_bundle(tmp, "9.9.6", bad_version="1.0.0")
    assert not sc.apply_update("9.9.6", url(sc, "9.9.6"), restart=False)
    assert not sc.apply_update("9.9.5", "https://evil.example/screen-client-9.9.5.tar.gz", restart=False)
    assert not sc.apply_update("../../x", url(sc, "x"), restart=False)


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl CLI needed")
def test_ota_signature_required_when_key_bundled(env, monkeypatch):
    sc, fake, tmp = env
    key, pub = tmp / "k.pem", tmp / "pub.pem"
    subprocess.run(["openssl", "genpkey", "-algorithm", "ed25519", "-out", str(key)], check=True)
    subprocess.run(["openssl", "pkey", "-in", str(key), "-pubout", "-out", str(pub)], check=True)
    monkeypatch.setattr(sc, "PUBKEY_PATH", pub)
    b = make_bundle(tmp, "9.9.4")
    assert not sc.apply_update("9.9.4", url(sc, "9.9.4"), restart=False)           # unsigned -> refused
    subprocess.run(["openssl", "pkeyutl", "-sign", "-inkey", str(key), "-rawin", "-in", str(b),
                    "-out", str(b) + ".sig"], check=True)
    assert sc.apply_update("9.9.4", url(sc, "9.9.4"), restart=False)               # signed -> installed
    b2 = make_bundle(tmp, "9.9.3"); shutil.copy(str(b) + ".sig", str(b2) + ".sig")  # wrong sig
    assert not sc.apply_update("9.9.3", url(sc, "9.9.3"), restart=False)


def test_launcher_rolls_back_after_three_failed_boots(env, monkeypatch):
    sc, fake, tmp = env
    make_bundle(tmp, "9.9.2"); sc.apply_update("9.9.2", url(sc, "9.9.2"), restart=False)
    import importlib, sys
    sys.modules.pop("launch", None); launch = importlib.import_module("launch")
    for _ in range(3):
        assert "9.9.2" in str(launch.pick())
    assert launch.pick().parent == Path(launch.__file__).parent      # 4th boot -> bundled build
    assert (sc.APP_DIR / "current.failed.json").exists()


def test_failed_build_is_not_reinstalled_in_a_loop(env):
    sc, fake, tmp = env
    make_bundle(tmp, "9.9.1")
    sc.APP_DIR.mkdir(parents=True, exist_ok=True)
    (sc.APP_DIR / "current.failed.json").write_text(json.dumps({"version": "9.9.1"}))
    assert not sc.apply_update("9.9.1", url(sc, "9.9.1"), restart=False)
    assert "previously failed" in sc.STATE.last_error
