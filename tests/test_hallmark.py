import asyncio

from app import db, hallmark
from app.services import metadata

FILMS = {1: ("Christmas on Cherry Lane", "2026-10-25"), 2: ("A Danish Christmas", "2026-11-01"),
         3: ("Merry Memories", "2026-12-20"), 4: ("Snow Globe Town", "2026-12-12"),  # 4: upcoming, made by Hallmark
         5: ("A Mystery Christmas", "2026-12-14")}  # 5: upcoming on Hallmark Mystery


def _fake(monkeypatch, searched):
    async def providers():
        return [{"id": 290, "name": "Hallmark+ Amazon Channel", "kinds": ["movie"]}]

    async def disc(pid, name, sort, released):
        return {"results": [{"tmdb_id": i, "title": t, "date": d, "poster": None} for i, (t, d) in FILMS.items() if i < 4]}

    async def productions():
        return [{"tmdb_id": i, "title": FILMS[i][0], "date": FILMS[i][1], "poster": None} for i in (2, 4)]

    async def details(tmdb_id):
        t, d = FILMS[tmdb_id]
        return {"tmdb_id": tmdb_id, "imdb_id": None, "title": t, "original_title": t, "year": 2026, "overview": "",
                "poster": None, "runtime": 84, "release_date": d, "digital_release": d}

    async def search_added(movies):
        searched.extend(m["title"] for m in movies)
    monkeypatch.setattr(hallmark, "providers", providers)
    monkeypatch.setattr(metadata, "discover_christmas", disc)
    monkeypatch.setattr(metadata, "hallmark_productions", productions)

    async def premieres():
        return [{"tmdb_id": 5, "title": FILMS[5][0], "date": FILMS[5][1], "poster": None}]
    monkeypatch.setattr(metadata, "hallmark_premieres", premieres)
    monkeypatch.setattr(metadata, "movie_details", details)
    import app.collections_ as c
    monkeypatch.setattr(c, "_search_added", search_added)


def test_off_by_default_then_adds_once(env, monkeypatch):
    searched = []
    _fake(monkeypatch, searched)
    assert asyncio.run(hallmark.sync())["added"] == []  # auto-add off
    db.save_settings({"hallmark_auto": True})

    async def run():
        r = await hallmark.sync()
        await asyncio.sleep(0)
        return r
    assert sorted(asyncio.run(run())["added"]) == sorted(t for t, _ in FILMS.values())
    assert {m["tier"] for m in db.q("SELECT tier FROM movies")} == {"hd"}
    # removed from the library: not added back
    db.ex("DELETE FROM movies WHERE tmdb_id=2")
    assert asyncio.run(hallmark.sync())["added"] == []


def test_page_status(env, monkeypatch):
    _fake(monkeypatch, [])
    db.save_settings({"hallmark_auto": True, "hallmark_tier": "both"})
    asyncio.run(hallmark.sync())
    p = asyncio.run(hallmark.page())
    assert p["auto"] and p["tier"] == "both" and p["services"] == ["Hallmark+ Amazon Channel"]
    m = next(x for x in p["movies"] if x["tmdb_id"] == 3)
    assert sorted(m["tiers"]) == ["hd", "uhd"] and m["airs"] == "2026-12-20" and not m["has_file"]


def test_api(client, monkeypatch):
    _fake(monkeypatch, [])
    r = client.put("/api/hallmark", json={"auto": True, "tier": "hd"})
    assert r.status_code == 200 and len(r.json()["added"]) == 5
    assert client.put("/api/hallmark", json={"tier": "8k"}).status_code == 400
    assert client.get("/api/hallmark").json()["auto"] is True


def test_removed_shows_and_can_be_added_back(client, monkeypatch):
    _fake(monkeypatch, [])
    client.put("/api/hallmark", json={"auto": True, "tier": "hd"})
    db.ex("DELETE FROM movies WHERE tmdb_id=2")
    m = next(x for x in client.get("/api/hallmark").json()["movies"] if x["tmdb_id"] == 2)
    assert m["removed"] and not m["library_id"]
    r = client.post("/api/movies", json={"tmdb_id": 2, "tier": "hd", "christmas": True, "search": False})
    assert r.status_code == 200 and r.json()["christmas"] == 1
    m = next(x for x in client.get("/api/hallmark").json()["movies"] if x["tmdb_id"] == 2)
    assert not m["removed"] and m["library_id"]


def test_hallmark_movie_added_elsewhere_goes_to_christmas_folder(client, monkeypatch):
    _fake(monkeypatch, [])
    client.put("/api/hallmark", json={"auto": True, "tier": "hd"})
    db.ex("DELETE FROM movies WHERE tmdb_id=2")
    r = client.post("/api/movies", json={"tmdb_id": 2, "tier": "hd", "search": False})  # e.g. from its title page
    assert r.json()["christmas"] == 1


def test_premieres_from_channel_notes(env, monkeypatch):
    async def christmas_all(released):
        return {"results": [{"tmdb_id": 7, "title": "Hallmark Mystery Xmas", "date": "2026-12-14", "services": []},
                            {"tmdb_id": 8, "title": "Lifetime Xmas", "date": "2026-12-14", "services": []},
                            {"tmdb_id": 9, "title": "Streams Xmas", "date": "2026-11-01", "services": ["Hallmark+ Apple TV channel"]}]}

    async def tmdb(path, **_):
        note = "Hallmark Mystery" if "/7/" in path else "Lifetime"
        return {"results": [{"iso_3166_1": "US", "release_dates": [{"type": 6, "note": note}]}]}
    monkeypatch.setattr(metadata, "discover_christmas_all", christmas_all)
    monkeypatch.setattr(metadata, "_tmdb", tmdb)
    assert sorted(r["tmdb_id"] for r in asyncio.run(metadata.hallmark_premieres())) == [7, 9]
