"""Torrents added by hand: linked to a movie or show, imported (hardlinked, keeps seeding) when finished."""
import asyncio
import base64
import hashlib
import os

import pytest

from app import db, importer, torrents
from app.services import mediainfo
from app.services import qbittorrent as qbt

HASH = "a" * 40


@pytest.fixture()
def fake_qbt(monkeypatch):
    """A pretend qBittorrent: torrents keyed by hash."""
    state = {}

    async def call(method, path, data=None, files=None, params=None):
        if path == "torrents/info":
            want = (params or {}).get("hashes")
            return [t for h, t in state.items() if not want or h in want.split("|")]
        if path == "torrents/add":
            if data.get("urls"):
                h = qbt.magnet_hash(data["urls"])
                state[h] = {"hash": h, "name": qbt.magnet_name(data["urls"]), "progress": 0, "state": "metaDL"}
            return "Ok."
        if path == "torrents/delete":
            state.pop(data["hashes"], None)
        return "Ok."

    async def probe(_path):
        return None
    monkeypatch.setattr(qbt, "call", call)
    monkeypatch.setattr(mediainfo, "probe", probe)
    return state


def test_hashes_from_magnets_and_files():
    assert qbt.magnet_hash(f"magnet:?xt=urn:btih:{HASH.upper()}&dn=Heat") == HASH
    b32 = base64.b32encode(bytes.fromhex(HASH)).decode()
    assert qbt.magnet_hash(f"magnet:?xt=urn:btih:{b32}") == HASH
    assert qbt.magnet_hash("http://example.com") is None
    assert qbt.magnet_name("magnet:?xt=urn:btih:%s&dn=Heat.1995.1080p.BluRay.x264-GRP" % HASH) == "Heat.1995.1080p.BluRay.x264-GRP"
    info = b"d6:lengthi5e4:name9:Heat.1995e"
    data = b"d8:announce3:abc4:info" + info + b"e"
    assert qbt.torrent_hash(data) == hashlib.sha1(info).hexdigest()
    assert qbt.torrent_name(data) == "Heat.1995"
    with pytest.raises(ValueError):
        qbt.torrent_hash(b"<nzb>")


def _finish(state, h, path):
    state[h].update(progress=1, state="stalledUP", content_path=str(path), total_size=1000)


def test_movie_linked_then_imported_by_hardlink(env, fake_qbt):
    mid = db.insert("movies", {"tmdb_id": 949, "title": "Heat", "year": 1995, "tier": "hd", "folder": "Heat (1995)"})
    db.save_settings({"import_mode": "move"})  # torrents still hardlink
    name = "Heat.1995.1080p.BluRay.x264-GRP"
    r = asyncio.run(torrents.add(magnet=f"magnet:?xt=urn:btih:{HASH}&dn={name}", kind="movie"))
    assert r["linked"]["title"] == "Heat" and r["linked"]["quality"] == "Bluray-1080p"
    asyncio.run(importer.process_downloads())
    assert db.one("SELECT status FROM downloads")["status"] == "queued"
    src = env["data"] / "torrents" / name
    src.mkdir(parents=True)
    (src / f"{name}.mkv").write_bytes(b"x" * 1000)
    _finish(fake_qbt, HASH, src)
    asyncio.run(importer.process_downloads())
    dl = db.one("SELECT * FROM downloads")
    assert dl["status"] == "imported", dl["message"]
    movie = db.one("SELECT * FROM movies WHERE id=?", (mid,))
    assert movie["file_path"].startswith(str(env["data"] / "media" / "movies"))
    assert os.path.exists(src / f"{name}.mkv")  # still there for seeding
    assert os.stat(movie["file_path"]).st_ino == os.stat(src / f"{name}.mkv").st_ino  # hardlink, no extra space


def test_torrent_linked_later_to_a_show(env, fake_qbt, client):
    sid = db.insert("series", {"tvdb_id": 1, "tmdb_id": 9, "title": "Show", "tier": "hd", "folder": "Show (2020)"})
    for e in (1, 2):
        db.insert("episodes", {"series_id": sid, "season": 1, "episode": e})
    name = "Show.S01.1080p.WEB-DL.DDP5.1.H.264-GRP"
    r = client.post("/api/torrents", json={"magnet": f"magnet:?xt=urn:btih:{HASH}&dn={name}"}).json()
    assert r["hash"] == HASH and r["linked"] is None
    lst = client.get("/api/torrents").json()
    assert lst["torrents"][0]["name"] == name and lst["torrents"][0]["link"] is None
    src = env["data"] / "torrents" / name
    src.mkdir(parents=True)
    for e in (1, 2):
        (src / f"Show.S01E0{e}.1080p.WEB-DL.DDP5.1.H.264-GRP.mkv").write_bytes(b"x")
    _finish(fake_qbt, HASH, src)
    assert client.get("/api/torrents/guess", params={"kind": "tv", "name": name}).json()["item_id"] == sid
    res = client.post(f"/api/torrents/{HASH}/link", json={"kind": "tv", "item_id": sid})
    assert res.status_code == 200, res.text
    asyncio.run(importer.process_downloads())
    eps = db.q("SELECT * FROM episodes WHERE series_id=? ORDER BY episode", (sid,))
    assert all(e["file_path"] and "Season 01" in e["file_path"] for e in eps)
    assert client.get("/api/torrents").json()["torrents"][0]["link"]["status"] == "imported"


def test_unlink_and_delete(env, fake_qbt, client):
    db.insert("movies", {"tmdb_id": 949, "title": "Heat", "year": 1995, "tier": "hd"})
    asyncio.run(torrents.add(magnet=f"magnet:?xt=urn:btih:{HASH}&dn=Heat.1995.1080p.BluRay.x264-GRP", kind="movie"))
    dl = db.one("SELECT * FROM downloads")
    assert client.delete(f"/api/queue/{dl['id']}").status_code == 200  # Activity remove = unlink
    assert HASH in fake_qbt  # the torrent stays
    assert db.one("SELECT status FROM downloads")["status"] == "removed"
    assert client.delete(f"/api/torrents/{HASH}").status_code == 200
    assert HASH not in fake_qbt


def test_unknown_title_is_added_but_not_linked(env, fake_qbt):
    r = asyncio.run(torrents.add(magnet=f"magnet:?xt=urn:btih:{HASH}&dn=Ronin.1998.1080p.BluRay-GRP", kind="movie"))
    assert r["linked"] is None and "Couldn't tell" in r["link_error"]
    assert HASH in fake_qbt
