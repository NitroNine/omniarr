"""Uploading an .nzb by hand: it's matched to a library title and goes through NZBGet + import like a grab."""
import asyncio
import base64

import pytest
from fastapi.testclient import TestClient

from app import db, library, manual_nzb
from app.services import metadata, nzbget

NZB = (b'<?xml version="1.0"?><nzb xmlns="http://www.newzbin.com/DTD/2003/nzb"><head>'
       b'<meta type="name">Heat.1995.1080p.BluRay.x264-AMIABLE</meta></head>'
       b'<file><segments><segment bytes="700000000" number="1">a@b</segment>'
       b'<segment bytes="300000000" number="2">c@d</segment></segments></file></nzb>')


@pytest.fixture()
def sent(monkeypatch):
    out = []

    async def call(method, *params):
        if method == "append":
            out.append({"name": params[0], "cat": params[2], "nzb": base64.b64decode(params[1])})
            return 100 + len(out)
        return [] if method in ("listgroups", "history") else None
    monkeypatch.setattr(nzbget, "call", call)
    return out


def _movies():
    db.ex("INSERT INTO movies (tmdb_id, title, year, tier) VALUES (949, 'Heat', 1995, 'hd')")
    db.ex("INSERT INTO movies (tmdb_id, title, year, tier) VALUES (949, 'Heat', 1995, 'uhd')")
    db.ex("INSERT INTO movies (tmdb_id, title, year, tier) VALUES (1, 'Heat', 1972, 'hd')")
    return {(r["year"], r["tier"]): r["id"] for r in db.q("SELECT * FROM movies")}


def test_guess_picks_title_year_and_version(env):
    ids = _movies()
    assert manual_nzb.guess("movie", "Heat.1995.1080p.BluRay.x264-AMIABLE")["id"] == ids[(1995, "hd")]
    assert manual_nzb.guess("movie", "Heat.1995.2160p.UHD.BluRay.x265-W4NK3R")["id"] == ids[(1995, "uhd")]
    assert manual_nzb.guess("movie", "Heat.1972.1080p.WEB-DL-GRP")["id"] == ids[(1972, "hd")]
    assert manual_nzb.guess("movie", "Ronin.1998.1080p.BluRay-GRP") is None


def test_movie_upload(env, sent):
    ids = _movies()
    r = asyncio.run(manual_nzb.add("whatever.nzb", NZB, "movie"))
    assert r["title"] == "Heat" and r["tier"] == "hd" and r["quality"] == "Bluray-1080p"
    assert sent[0]["cat"] == "movies" and sent[0]["nzb"] == NZB
    dl = db.one("SELECT * FROM downloads")
    assert dl["movie_id"] == ids[(1995, "hd")] and dl["indexer"] == "Manual upload" and dl["size"] == 10 ** 9
    assert dl["release_title"] == "Heat.1995.1080p.BluRay.x264-AMIABLE" and dl["status"] == "queued"
    # an explicit choice wins over the name
    asyncio.run(manual_nzb.add("x.nzb", NZB, "movie", ids[(1972, "hd")]))
    assert db.q("SELECT movie_id FROM downloads ORDER BY id")[1]["movie_id"] == ids[(1972, "hd")]


def test_rejects_unknown_and_non_nzb(env, sent):
    _movies()
    with pytest.raises(ValueError, match="Pick it from the list"):
        asyncio.run(manual_nzb.add("Ronin.1998.1080p.BluRay-GRP.nzb", b"<nzb></nzb>", "movie"))
    with pytest.raises(ValueError, match="isn't an .nzb"):
        asyncio.run(manual_nzb.add("a.nzb", b"PK\x03\x04zip", "movie"))
    assert not sent


def test_tv_upload_maps_episodes(env, sent, monkeypatch):
    async def details(tvdb_id=None, tmdb_id=None):
        return {"tvdb_id": 403245, "title": "The Bear", "year": 2022, "overview": "", "poster": None,
                "imdb_id": None, "tmdb_id": 136315, "runtime": 30, "status": "Continuing", "series_type": "standard"}

    async def episodes(_id):
        return [{"tvdb_id": i, "season": 1, "episode": i, "absolute": i, "title": f"Ep {i}",
                 "air_date": f"2022-06-{i:02d}", "overview": ""} for i in range(1, 4)]
    monkeypatch.setattr(metadata, "series_details", details)
    monkeypatch.setattr(metadata, "series_episodes", episodes)
    s = asyncio.run(library.add_series(403245, "hd"))
    r = asyncio.run(manual_nzb.add("The.Bear.S01E02.1080p.WEB.h264-ETHEL.nzb", b"<nzb></nzb>", "tv"))
    assert r["title"] == "The Bear" and r["episodes"] == 1 and sent[0]["cat"] == "tv"
    r = asyncio.run(manual_nzb.add("The.Bear.S01.1080p.HULU.WEB-DL.DDP5.1.H.264-NTb.nzb", b"<nzb></nzb>", "tv"))
    assert r["episodes"] == 3
    assert {d["series_id"] for d in db.q("SELECT series_id FROM downloads")} == {s["id"]}
    with pytest.raises(ValueError, match="which episodes"):
        asyncio.run(manual_nzb.add("The.Bear.1080p.WEB-GRP.nzb", b"<nzb></nzb>", "tv"))


def test_api(env, sent):
    from app import auth
    from app.main import app
    _movies()
    auth._failures.clear()
    auth.create_user("boss", "bosspassword", role="admin")
    auth.create_user("kid", "kidpassword")
    kid = TestClient(app)
    kid.post("/api/auth/login", json={"username": "kid", "password": "kidpassword"})
    assert kid.post("/api/nzb", json={"name": "a.nzb", "data": "", "kind": "movie"}).status_code == 403  # admins only
    c = TestClient(app)
    c.post("/api/auth/login", json={"username": "boss", "password": "bosspassword"})
    r = c.post("/api/nzb", json={"name": "a.nzb", "data": base64.b64encode(NZB).decode(), "kind": "movie"})
    assert r.status_code == 200 and r.json()["title"] == "Heat"
    r = c.post("/api/nzb", json={"name": "Ronin.1998.nzb", "data": base64.b64encode(b"<nzb/>").decode(), "kind": "movie"})
    assert r.status_code == 400 and "Pick it" in r.json()["detail"]
    assert c.get("/api/nzb/guess", params={"kind": "movie", "name": "Heat.1995.2160p.WEB.nzb"}).json()["item_id"]
