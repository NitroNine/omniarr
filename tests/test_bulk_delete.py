import asyncio
import os

from app import db, library
from app.services import metadata, nzbget

FILMS = {1: "Alpha", 2: "Beta", 3: "Gamma"}


def _fake(monkeypatch):
    async def details(tmdb_id):
        return {"tmdb_id": tmdb_id, "imdb_id": None, "title": FILMS[tmdb_id], "original_title": FILMS[tmdb_id],
                "year": 2020, "overview": "", "poster": None, "runtime": 90, "release_date": "2020-01-01"}
    monkeypatch.setattr(metadata, "movie_details", details)


def _with_file(m):
    d = library.movie_dir(m)
    os.makedirs(d, exist_ok=True)
    f = os.path.join(d, f"{m['title']} {m['tier']}.mkv")
    open(f, "w").close()
    db.update("movies", m["id"], {"file_path": f})
    return d


def test_bulk_delete_keeps_files_by_default(client, monkeypatch):
    _fake(monkeypatch)
    a = asyncio.run(library.add_movie(1, "hd"))
    asyncio.run(library.add_movie(1, "uhd"))
    b = asyncio.run(library.add_movie(2, "hd"))
    asyncio.run(library.add_movie(3, "hd"))
    da, db_ = _with_file(a), _with_file(b)
    r = client.post("/api/movies/delete", json={"ids": [a["id"], b["id"]]}).json()
    assert r == {"removed": 2, "versions": 3}
    assert [m["title"] for m in db.q("SELECT title FROM movies")] == ["Gamma"]
    assert os.path.isdir(da) and os.path.isdir(db_)


def test_bulk_delete_files_and_cancels_downloads(client, monkeypatch):
    _fake(monkeypatch)
    a = asyncio.run(library.add_movie(1, "hd"))
    u = asyncio.run(library.add_movie(1, "uhd"))
    d = _with_file(a)
    _with_file({**u, "folder": a["folder"]})
    dl = db.insert("downloads", {"kind": "movie", "movie_id": u["id"], "release_title": "x", "nzbget_id": 42,
                                 "status": "downloading"})
    gone = []

    async def delete(nid, from_history=False):
        gone.append(nid)
    monkeypatch.setattr(nzbget, "delete", delete)
    r = client.post("/api/movies/delete", json={"ids": [a["id"]], "delete_files": True}).json()
    assert r["removed"] == 1 and not os.path.exists(d) and gone == [42]
    assert db.one("SELECT status FROM downloads WHERE id=?", (dl,))["status"] == "failed"
    assert not db.q("SELECT 1 FROM movies")


def test_series_bulk_delete(client):
    sid = db.insert("series", {"tvdb_id": 1, "tmdb_id": 9, "title": "Show", "tier": "hd", "folder": "Show (2020)"})
    db.insert("episodes", {"series_id": sid, "season": 1, "episode": 1, "title": "Pilot"})
    assert client.post("/api/series/delete", json={"ids": [sid]}).json()["removed"] == 1
    assert not db.q("SELECT 1 FROM series") and not db.q("SELECT 1 FROM episodes")
