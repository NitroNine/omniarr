"""After an import, the media server is asked to scan that title's folder."""
import asyncio

from app import db, importer
from app.services import mediainfo, mediaserver

SILO_LIBS = {"items": [
    {"id": "1", "name": "Movies", "paths": ["/mnt/user/media/movies"], "enabled": True},
    {"id": "2", "name": "TV Shows", "paths": ["/mnt/user/media/TV"], "enabled": True},
]}


def _fake(monkeypatch, libs=SILO_LIBS):
    calls = []

    async def call(method, path, json_body=None):
        calls.append((method, path, json_body))
        if path == "/api/v2/libraries":
            return libs
        if path == "/Library/VirtualFolders":
            return [{"Name": "TV", "ItemId": "a", "Locations": ["/tv"]}]
        return {"status": "accepted"}
    monkeypatch.setattr(mediaserver, "_call", call)
    monkeypatch.setattr(mediaserver, "_libs", None)
    return calls


def test_paths_matched_by_folder_name_or_mapping(env, monkeypatch):
    libs = [{"id": "2", "name": "TV", "paths": ["/mnt/user/media/TV"]}, {"id": "1", "name": "Movies", "paths": ["/mnt/user/media/movies"]}]
    lib, p = mediaserver.server_path("/data/TV/DIG (2026)", libs)
    assert lib["id"] == "2" and p == "/mnt/user/media/TV/DIG (2026)"
    assert mediaserver.server_path("/data/movies/Heat (1995)", libs)[1] == "/mnt/user/media/movies/Heat (1995)"
    assert mediaserver.server_path("/data/other/x", libs) is None
    assert mediaserver.server_path("/data/TV", libs)[1] == "/mnt/user/media/TV"  # Scan everything now
    xmas = [{"id": "3", "name": "Christmas", "paths": ["/christmas movies"]}]
    assert mediaserver.server_path("/data/Christmas Movies/Elf (2003)", xmas)[1] == "/christmas movies/Elf (2003)"
    db.save_settings({"media_server_path_from": "/data", "media_server_path_to": "/mnt/user/media"})
    assert mediaserver.server_path("/data/TV/Show", libs)[1] == "/mnt/user/media/TV/Show"
    assert mediaserver.server_path("/elsewhere/TV/Show", libs)[1] == "/mnt/user/media/TV/Show"


def test_silo_scan_after_episode_import(env, monkeypatch):
    calls = _fake(monkeypatch)

    async def probe(_p):
        return None
    monkeypatch.setattr(mediainfo, "probe", probe)
    db.save_settings({"media_server_type": "silo", "media_server_url": "http://silo:8090", "media_server_api_key": "sa_x",
                      "root_tv": str(env["data"] / "media" / "TV")})
    sid = db.insert("series", {"tvdb_id": 1, "tmdb_id": 9, "title": "Show", "tier": "hd", "folder": "Show (2020)"})
    eid = db.insert("episodes", {"series_id": sid, "season": 1, "episode": 1})
    src = env["tmp"] / "dl" / "Show.S01E01.1080p.WEB-DL-GRP"
    src.mkdir(parents=True)
    (src / "Show.S01E01.1080p.WEB-DL-GRP.mkv").write_bytes(b"x")
    dl = {"series_id": sid, "episode_ids": f"[{eid}]", "release_title": "Show.S01E01.1080p.WEB-DL-GRP",
          "quality": "WEBDL-1080p", "score": 0, "custom_formats": "[]"}

    async def run():
        await importer.import_episodes(dl, str(src))
        await asyncio.sleep(0.05)  # the scan runs in the background
    asyncio.run(run())
    scans = [c for c in calls if c[1] == "/api/v2/scan"]
    assert scans == [("POST", "/api/v2/scan", {"library_id": "2", "path": "/mnt/user/media/TV/Show (2020)"})]


def test_off_by_default_and_jellyfin(env, monkeypatch):
    calls = _fake(monkeypatch)
    assert asyncio.run(mediaserver.scan(["/data/TV/Show"])) == []
    db.save_settings({"media_server_type": "jellyfin", "media_server_url": "http://jf:8096", "media_server_api_key": "k"})
    assert asyncio.run(mediaserver.scan(["/data/tv/Show"])) == ["/tv/Show"]
    assert calls[-1] == ("POST", "/Library/Media/Updated", {"Updates": [{"Path": "/tv/Show", "UpdateType": "Created"}]})


def test_settings_api(client, monkeypatch):
    _fake(monkeypatch)
    client.put("/api/settings", json={"media_server_type": "silo", "media_server_url": "http://silo:8090", "media_server_api_key": "sa_secret"})
    s = client.get("/api/settings").json()
    assert s["media_server_api_key"] == "********"
    r = client.post("/api/media-server/test").json()
    assert r["ok"] and r["libraries"] == ["Movies", "TV Shows"]
