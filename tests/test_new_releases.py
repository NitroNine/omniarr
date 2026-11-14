from app import db
from app.services import metadata


def _fake(monkeypatch, seen):
    async def tmdb(path, **params):
        seen.append((path, params))
        date = "release_date" if path.endswith("movie") else "first_air_date"
        return {"page": 1, "total_pages": 3, "total_results": 50,
                "results": [{"id": 7, "title": "New Film", "name": "New Show", date: "2026-11-01", "poster_path": "/p.jpg"},
                            {"id": 8, "title": "Other", "name": "Other Show", date: "2026-10-20"}]}
    monkeypatch.setattr(metadata, "_tmdb", tmdb)
    metadata._info_cache.clear()


def test_new_movies_home_and_cinema(client, monkeypatch):
    seen = []
    _fake(monkeypatch, seen)
    db.ex("INSERT INTO movies(tmdb_id, title, tier, monitored) VALUES(7, 'New Film', 'hd', 1)")
    d = client.get("/api/new/movie").json()
    path, p = seen[-1]
    assert path == "/discover/movie" and p["with_release_type"] == "4|5" and p["sort_by"] == "popularity.desc"
    assert p["with_original_language"] == "en" and p["release_date.gte"] < p["release_date.lte"]
    assert [(r["title"], r["library_tiers"]) for r in d["results"]] == [("New Film", ["hd"]), ("Other", [])]
    assert d["results"][0]["poster"].endswith("/p.jpg") and d["total_pages"] == 3
    client.get("/api/new/movie?where=cinema")
    assert seen[-1][1]["with_release_type"] == "2|3"


def test_new_tv(client, monkeypatch):
    seen = []
    _fake(monkeypatch, seen)
    d = client.get("/api/new/tv?page=2").json()
    path, p = seen[-1]
    assert path == "/discover/tv" and p["page"] == 2 and "10767" in p["without_genres"] and "first_air_date.gte" in p
    assert d["results"][0]["title"] == "New Show"
    assert client.get("/api/new/book").status_code == 400
