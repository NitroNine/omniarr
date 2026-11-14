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


def test_new_movies_streaming_and_cinema(client, monkeypatch):
    seen = []
    _fake(monkeypatch, seen)
    db.ex("INSERT INTO movies(tmdb_id, title, tier, monitored) VALUES(7, 'New Film', 'hd', 1)")
    d = client.get("/api/new/movie").json()
    path, p = seen[-1]
    assert path == "/discover/movie" and p["with_release_type"] == "4" and p["sort_by"] == "popularity.desc"
    assert p["with_watch_monetization_types"] == "flatrate" and "with_watch_providers" not in p
    assert p["with_original_language"] == "en" and p["release_date.gte"] < p["release_date.lte"]
    assert [(r["title"], r["library_tiers"]) for r in d["results"]] == [("New Film", ["hd"]), ("Other", [])]
    assert d["results"][0]["poster"].endswith("/p.jpg") and d["total_pages"] == 3
    client.get("/api/new/movie?where=cinema&provider=8")
    p = seen[-1][1]
    assert p["with_release_type"] == "2|3" and "with_watch_providers" not in p and "with_watch_monetization_types" not in p
    client.get("/api/new/movie?where=streaming&provider=8")
    assert seen[-1][1]["with_watch_providers"] == 8


def test_new_tv(client, monkeypatch):
    seen = []
    _fake(monkeypatch, seen)
    d = client.get("/api/new/tv?page=2").json()
    path, p = seen[-1]
    assert path == "/discover/tv" and p["page"] == 2 and "10767" in p["without_genres"] and "first_air_date.gte" in p
    assert d["results"][0]["title"] == "New Show"
    assert client.get("/api/new/book").status_code == 400


def test_new_tv_by_service(client, monkeypatch):
    seen = []
    _fake(monkeypatch, seen)
    client.get("/api/new/tv?provider=8")
    p = seen[-1][1]
    assert p["with_watch_providers"] == 8 and p["watch_region"] and p["with_watch_monetization_types"] == "flatrate"
    client.get("/api/new/tv")
    assert "with_watch_providers" not in seen[-1][1]  # a separate cache entry, not the service's list


def test_new_4k_discs(client, monkeypatch):
    import time
    today = time.strftime("%Y-%m-%d")
    old = time.strftime("%Y-%m-%d", time.localtime(time.time() - 200 * 86400))
    recent = time.strftime("%Y-%m-%d", time.localtime(time.time() - 10 * 86400))
    notes = {1: [("US", 5, recent, "4K Ultra HD / Blu-ray / DVD")], 2: [("US", 5, recent, "Blu-ray / DVD")],
             3: [("GB", 5, today, "4k Ultra HD SteelBook"), ("US", 4, today, "4K")], 4: [("US", 5, old, "4K Ultra HD")]}
    seen = []

    async def tmdb(path, **params):
        seen.append((path, params))
        if path.endswith("/release_dates"):
            mid = int(path.split("/")[2])
            return {"results": [{"iso_3166_1": c, "release_dates": [{"type": t, "release_date": d + "T00:00:00.000Z", "note": n}]}
                                for c, t, d, n in notes[mid]]}
        return {"page": params["page"], "total_pages": 1, "total_results": 4,
                "results": [{"id": i, "title": f"Film {i}", "release_date": recent} for i in notes]}
    monkeypatch.setattr(metadata, "_tmdb", tmdb)
    metadata._info_cache.clear()
    d = client.get("/api/new/movie?where=disc4k").json()
    assert seen[0][1]["with_release_type"] == "5"
    assert [(r["title"], r["date"]) for r in d["results"]] == [("Film 3", today), ("Film 1", recent)]  # newest first
    assert d["total_pages"] == 1 and client.get("/api/new/movie?where=bogus").status_code == 400
