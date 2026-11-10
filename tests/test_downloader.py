import asyncio
import os
import stat

from fastapi.testclient import TestClient

from app import auth, db
from app.main import app
from app.services import downloader, nzbget

TEMPLATE = """# NZBGet configuration
MainDir=~/downloads
DestDir=${MainDir}/dst
ControlIP=0.0.0.0
ControlPassword=tegbzn6789
Server1.Active=yes
Server1.Host=my.newsserver.com
Server1.Password=pass
Category1.Name=Movies
Category2.Name=Series
Category3.Name=Music
DupeCheck=yes
SomethingElse=keep
"""


def _fake_install(tmp_path, monkeypatch, script="sleep 60"):
    d = tmp_path / "opt-nzbget"
    d.mkdir()
    (d / "nzbget.conf").write_text(TEMPLATE)
    b = d / "nzbget"
    b.write_text(f"#!/bin/sh\n{script}\n")
    b.chmod(b.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(downloader, "APP_DIR", str(d))
    return d


def test_config_is_rendered_from_template(env, tmp_path, monkeypatch):
    _fake_install(tmp_path, monkeypatch)
    db.save_settings({"download_dir": str(tmp_path / "usenet"), "download_speed_limit": 5000})
    db.insert("usenet_servers", {"host": "news.example.com", "port": 563, "ssl": 1, "username": "me",
                                 "password": "s3cret", "connections": 30})
    db.insert("usenet_servers", {"host": "block.example.net", "port": 119, "ssl": 0, "level": 1, "active": 0})
    text = downloader.write_config()
    assert "my.newsserver.com" not in text and "Category3.Name=Music" not in text  # examples gone
    for line in (f"MainDir={tmp_path / 'usenet'}", "ControlIP=127.0.0.1", "DupeCheck=no", "DownloadRate=5000",
                 "SomethingElse=keep", "Category1.Name=movies", "Category2.Name=tv",
                 "Server1.Host=news.example.com", "Server1.Password=s3cret", "Server1.Connections=30",
                 "Server1.Encryption=yes", "Server1.CertVerification=strict",
                 "Server2.Host=block.example.net", "Server2.Level=1", "Server2.Optional=yes", "Server2.Active=no"):
        assert line in text.splitlines(), line
    assert "ControlPassword=tegbzn6789" not in text
    assert stat.S_IMODE(os.stat(downloader.conf_path()).st_mode) == 0o600  # holds passwords
    assert os.path.isdir(tmp_path / "usenet")
    # the control password is generated once and kept
    assert downloader.control_password() == downloader.control_password()


def test_builtin_is_supervised(env, tmp_path, monkeypatch):
    _fake_install(tmp_path, monkeypatch)
    calls = []

    async def fake_call(method, *a, **k):
        calls.append(method)
    monkeypatch.setattr(nzbget, "call", fake_call)
    try:
        asyncio.run(downloader.ensure())
        assert downloader.running()
        pid = downloader._proc.pid
        downloader._proc.kill()
        downloader._proc.wait()
        asyncio.run(downloader.ensure())  # crashed -> restarted
        assert downloader.running() and downloader._proc.pid != pid
        db.save_settings({"download_speed_limit": 100})
        asyncio.run(downloader.ensure())  # settings changed -> config rewritten and reloaded
        assert calls == ["reload"] and "DownloadRate=100" in open(downloader.conf_path()).read()
        db.save_settings({"download_client": "external"})
        asyncio.run(downloader.ensure())  # switched off -> stopped
        assert not downloader.running()
    finally:
        downloader.stop()


def test_client_target(env):
    url, auth_, _ = nzbget._target()
    assert url == "http://127.0.0.1:16789" and auth_[0] == "omniarr"
    db.save_settings({"download_client": "external", "nzbget_url": "http://10.0.0.5:6789/",
                      "nzbget_username": "u", "nzbget_password": "p"})
    assert nzbget._target()[:2] == ("http://10.0.0.5:6789", ("u", "p"))


def test_old_installs_keep_their_nzbget(tmp_path):
    db.reset_for_tests(str(tmp_path / "a" / "omniarr.db"))
    assert db.setting("download_client") == "builtin"  # new install
    db.save_settings({"nzbget_password": "x"})
    db.ex("DELETE FROM settings WHERE key='download_client'")
    db.reset_for_tests(str(tmp_path / "a" / "omniarr.db"))  # reopen = migrate
    assert db.setting("download_client") == "external"


def test_parse_servers_from_other_nzbget():
    cfg = [{"Name": "Server1.Host", "Value": "news.a.com"}, {"Name": "Server1.Encryption", "Value": "yes"},
           {"Name": "Server1.Port", "Value": "563"}, {"Name": "Server1.Username", "Value": "me"},
           {"Name": "Server1.Password", "Value": "pw"}, {"Name": "Server1.Connections", "Value": "40"},
           {"Name": "Server2.Host", "Value": ""}, {"Name": "Server3.Host", "Value": "block.b.com"},
           {"Name": "Server3.Level", "Value": "1"}, {"Name": "Server3.Active", "Value": "no"}]
    s = downloader.parse_servers(cfg)
    assert [x["host"] for x in s] == ["news.a.com", "block.b.com"]
    assert s[0]["ssl"] == 1 and s[0]["connections"] == 40 and s[0]["password"] == "pw"
    assert s[1]["level"] == 1 and s[1]["active"] == 0 and s[1]["port"] == 119


def test_server_api(client, monkeypatch):
    async def noop():
        return None
    monkeypatch.setattr(downloader, "ensure", noop)
    sid = client.post("/api/usenet-servers", json={"host": " news.a.com ", "username": "me", "password": "pw"}).json()["id"]
    rows = client.get("/api/usenet-servers").json()
    assert rows[0]["host"] == "news.a.com" and rows[0]["password"] == "********"  # never sent back
    client.put(f"/api/usenet-servers/{sid}", json={"connections": 20, "password": "********"})
    row = db.one("SELECT * FROM usenet_servers WHERE id=?", (sid,))
    assert row["connections"] == 20 and row["password"] == "pw"  # mask doesn't overwrite it

    tested = {}

    async def fake_call(method, *a, external=False):
        if method == "testserver":
            tested["args"] = a
            return "" if a[3] == "pw" else "Authentication failed"
        if method == "config":
            assert external
            return [{"Name": "Server1.Host", "Value": "news.a.com"}, {"Name": "Server1.Username", "Value": "me"},
                    {"Name": "Server2.Host", "Value": "news.c.com"}, {"Name": "Server2.Password", "Value": "z"}]
        raise AssertionError(method)
    monkeypatch.setattr(nzbget, "call", fake_call)
    assert client.post("/api/usenet-servers/test", json={"id": sid}).json()["ok"]  # stored password used
    assert tested["args"][3] == "pw"
    bad = client.post("/api/usenet-servers/test", json={"host": "x", "password": "nope", "ssl": True}).json()
    assert not bad["ok"] and "Authentication" in bad["message"]
    r = client.post("/api/usenet-servers/copy").json()
    assert r == {"found": 2, "added": 1}  # news.a.com/me was already there
    assert client.delete(f"/api/usenet-servers/{sid}").status_code == 200

    # admin only
    auth._failures.clear()
    auth.create_user("kid", "kidpassword")
    kid = TestClient(app)
    kid.post("/api/auth/login", json={"username": "kid", "password": "kidpassword"})
    for m, path in [("GET", "/api/usenet-servers"), ("POST", "/api/usenet-servers/copy"),
                    ("GET", "/api/downloader/status"), ("POST", "/api/downloader/pause")]:
        assert kid.request(m, path, json={}).status_code == 403, path


def test_settings_cannot_set_internal_keys(client, monkeypatch):
    async def noop():
        return None
    monkeypatch.setattr(downloader, "ensure", noop)
    pw = downloader.control_password()
    client.put("/api/settings", json={"_downloader_password": "hacked", "download_speed_limit": 10})
    assert downloader.control_password() == pw and db.setting("download_speed_limit") == 10
    assert "_downloader_password" not in client.get("/api/settings").json()


def test_download_folder_follows_container_setup(env, tmp_path, monkeypatch):
    mount = tmp_path / "downloads"
    monkeypatch.setattr(downloader, "DOWNLOADS_MOUNT", str(mount))
    assert downloader.download_dir() == "/data/usenet"  # no Downloads path in the container setup
    mount.mkdir()
    assert downloader.download_dir() == str(mount)  # mapped in the Unraid template -> used
    assert downloader.options()["MainDir"] == str(mount)
    db.save_settings({"download_dir": "/data/elsewhere/"})
    assert downloader.download_dir() == "/data/elsewhere"  # chosen in Settings wins
