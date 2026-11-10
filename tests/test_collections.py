import asyncio

from app import collections_, db
from app.services import metadata

LOTR = {120: ("The Fellowship of the Ring", "2001-12-18"), 121: ("The Two Towers", "2002-12-18"),
        122: ("The Return of the King", "2003-12-17")}
MOVIES = dict(LOTR)


def _fake(monkeypatch, parts):
    async def details(tid):
        t, rd = MOVIES[tid]
        coll = 119 if tid in LOTR or tid == 999 else None
        return {"tmdb_id": tid, "imdb_id": None, "title": t, "original_title": t, "year": int(rd[:4]),
                "overview": "", "poster": None, "runtime": 180, "release_date": rd,
                "collection_id": coll, "collection_name": "The Lord of the Rings Collection" if coll else None}

    async def collection(source, cid):
        return {"source": source, "id": cid, "name": "The Lord of the Rings Collection", "overview": "",
                "poster": None, "parts": [{"tmdb_id": t, "title": MOVIES[t][0], "year": int(MOVIES[t][1][:4]),
                                           "release_date": MOVIES[t][1], "poster": None} for t in parts]}
    monkeypatch.setattr(metadata, "movie_details", details)
    monkeypatch.setattr(metadata, "collection", collection)


def test_add_collection_with_exclusions_and_refresh(env, monkeypatch):
    parts = [120, 121, 122]
    _fake(monkeypatch, parts)
    # leave out The Two Towers
    r = asyncio.run(collections_.add("collection", 119, "uhd", tmdb_ids=[120, 122], monitor=True, search=False))
    assert (r["added"], r["total"]) == (2, 3)
    movies = db.q("SELECT tmdb_id, tier, collection_id FROM movies ORDER BY tmdb_id")
    assert [m["tmdb_id"] for m in movies] == [120, 122]
    assert all(m["tier"] == "uhd" and m["collection_id"] == 119 for m in movies)

    # details show library state
    d = asyncio.run(collections_.details("collection", 119))
    assert [p["in_library"] for p in d["parts"]] == [True, False, True]

    # a sequel gets announced: refresh adds it, but never the excluded movie
    MOVIES[999] = ("The War of the Rohirrim", "2099-12-13")
    parts.append(999)
    assert asyncio.run(collections_.refresh()) == 1
    ids = {m["tmdb_id"] for m in db.q("SELECT tmdb_id FROM movies")}
    assert ids == {120, 122, 999}
    assert asyncio.run(collections_.refresh()) == 0  # idempotent

    [c] = collections_.list_all()
    assert c["total"] == 3 and len(c["movies"]) == 3 and c["excluded"] == [121]

    # unmonitored collections aren't refreshed
    db.ex("UPDATE collections SET monitored=0")
    MOVIES[1000] = ("Another One", "2100-01-01")
    parts.append(1000)
    assert asyncio.run(collections_.refresh()) == 0


def test_add_whole_collection_skips_existing(env, monkeypatch):
    _fake(monkeypatch, [120, 121, 122])
    from app import library
    asyncio.run(library.add_movie(121, "hd"))
    r = asyncio.run(collections_.add("collection", 119, "hd", search=False))
    assert r["added"] == 2 and db.one("SELECT COUNT(*) c FROM movies")["c"] == 3


def test_keyword_universe(env, monkeypatch):
    _fake(monkeypatch, [120, 121])
    r = asyncio.run(collections_.add("keyword", 180547, "hd", search=False))
    assert r["added"] == 2
    [c] = collections_.list_all()
    assert c["source"] == "keyword" and c["total"] == 2 and len(c["movies"]) == 2


def test_movie_lookup_flags_collection(client, monkeypatch):
    _fake(monkeypatch, [120])

    async def search(term):
        return [{"tmdb_id": 120, "title": "The Fellowship of the Ring", "year": 2001}]
    monkeypatch.setattr(metadata, "search_movies", search)
    res = client.get("/api/lookup/movie", params={"term": "fellowship"}).json()
    assert res[0]["collection"] == {"id": 119, "name": "The Lord of the Rings Collection"}
