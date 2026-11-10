import asyncio

from fastapi.testclient import TestClient

from app import db
from app.services import metadata

CALLS = []


def _fake_tmdb(monkeypatch):
    async def tmdb(path, **params):
        CALLS.append((path, params))
        if path == "/watch/providers/movie":
            return {"results": [
                {"provider_id": 8, "provider_name": "Netflix", "logo_path": "/n.png", "display_priorities": {"US": 1}},
                {"provider_id": 350, "provider_name": "Apple TV+", "logo_path": "/a.png", "display_priorities": {"US": 4}},
                {"provider_id": 2, "provider_name": "Apple TV Store", "logo_path": "/s.png", "display_priorities": {"US": 30}}]}
        if path == "/watch/providers/tv":
            return {"results": [
                {"provider_id": 8, "provider_name": "Netflix", "logo_path": "/n.png", "display_priorities": {"US": 0}},
                {"provider_id": 531, "provider_name": "Paramount Plus", "logo_path": "/p.png", "display_priorities": {"US": 6}}]}
        if path == "/discover/movie":
            return {"page": 1, "total_pages": 3, "total_results": 55, "results": [
                {"id": 100, "title": "Glass Onion", "release_date": "2022-11-23", "poster_path": "/g.jpg", "vote_average": 7.1},
                {"id": 101, "title": "Extraction", "release_date": "2020-04-24", "poster_path": None, "vote_average": 6.7}]}
        if path == "/discover/tv":
            return {"page": 1, "total_pages": 1, "total_results": 1, "results": [
                {"id": 66732, "name": "Stranger Things", "first_air_date": "2016-07-15", "poster_path": "/s.jpg"}]}
        if path == "/tv/66732/external_ids":
            return {"tvdb_id": 305288, "imdb_id": "tt4574334"}
        raise AssertionError(path)
    monkeypatch.setattr(metadata, "_tmdb", tmdb)
    metadata._providers_cache.clear()
    CALLS.clear()


def test_providers_merge_and_flags(env, monkeypatch):
    _fake_tmdb(monkeypatch)
    ps = asyncio.run(metadata.watch_providers())
    assert [p["name"] for p in ps] == ["Netflix", "Apple TV+", "Paramount Plus", "Apple TV Store"]
    nf = ps[0]
    assert nf["kinds"] == ["movie", "tv"] and nf["logo"].endswith("/n.png") and nf["originals"]
    assert next(p for p in ps if p["name"] == "Apple TV+")["originals"]
    assert not next(p for p in ps if p["name"] == "Apple TV Store")["originals"]
    asyncio.run(metadata.watch_providers())
    assert len(CALLS) == 2  # cached


def test_discover_params(env, monkeypatch):
    _fake_tmdb(monkeypatch)
    db.save_settings({"streaming_region": "gb"})
    asyncio.run(metadata.discover("movie", 8, page=2, sort="newest"))
    path, p = CALLS[-1]
    assert path == "/discover/movie" and p["with_watch_providers"] == 8 and p["watch_region"] == "GB"
    assert p["with_watch_monetization_types"] == "flatrate" and p["sort_by"] == "primary_release_date.desc" and p["page"] == 2
    asyncio.run(metadata.discover("tv", 350, originals_of="Apple TV+"))
    path, p = CALLS[-1]
    assert path == "/discover/tv" and p["with_networks"] == 2552 and "with_watch_providers" not in p


def test_api_marks_library_and_adds_tv(env, monkeypatch):
    _fake_tmdb(monkeypatch)
    db.insert("movies", {"tmdb_id": 100, "title": "Glass Onion", "tier": "hd", "folder": "x"})
    client = TestClient(__import__("app.main", fromlist=["app"]).app)
    d = client.get("/api/providers/8/movie").json()
    assert d["provider"]["name"] == "Netflix" and d["total_results"] == 55
    assert d["results"][0]["library_id"] and d["results"][1]["library_id"] is None

    async def details(tvdb_id):
        assert tvdb_id == 305288
        return {"tvdb_id": 305288, "title": "Stranger Things", "year": 2016, "overview": "", "poster": None,
                "imdb_id": "tt4574334", "tmdb_id": None, "runtime": 50, "status": "Ended", "series_type": "standard"}

    async def episodes(_):
        return []
    monkeypatch.setattr(metadata, "series_details", details)
    monkeypatch.setattr(metadata, "series_episodes", episodes)
    r = client.post("/api/series/from-tmdb", json={"tmdb_id": 66732, "tier": "uhd", "search": False})
    assert r.status_code == 200, r.text
    s = db.one("SELECT * FROM series")
    assert (s["tvdb_id"], s["tmdb_id"], s["tier"]) == (305288, 66732, "uhd")
    tv = client.get("/api/providers/8/tv").json()
    assert tv["results"][0]["library_id"] == s["id"]
