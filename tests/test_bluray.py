import asyncio
import time

from app import db
from app.services import bluray, metadata

FEED = """<?xml version="1.0"?><rss><channel><title>New Releases</title>
<item><title>Test Film 4K (Blu-ray)</title><link>https://www.blu-ray.com/movies/Test-Film-4K-Blu-ray/1/</link>
<description>&lt;b&gt;&lt;font color="#666666"&gt;Some Studio | 2006 | 123 mins | Rated PG-13 | {d1}&lt;/font&gt;&lt;/b&gt;&lt;br&gt;Plot.</description></item>
<item><title>Other Film (Blu-ray)</title><link>https://www.blu-ray.com/movies/Other-Film-Blu-ray/2/</link>
<description>&lt;b&gt;&lt;font&gt;Studio | 2020 | 90 mins | {d1}&lt;/font&gt;&lt;/b&gt;</description></item>
<item><title>Another One 4K (Blu-ray)</title><link>https://www.blu-ray.com/movies/Another-One-4K-Blu-ray/3/</link>
<description>&lt;b&gt;&lt;font&gt;Label | 1999 | 100 mins | Unrated | {d2}&lt;/font&gt;&lt;/b&gt;</description></item>
</channel></rss>"""


def _days(n):
    return time.time() - n * 86400


def _feed():
    fmt = lambda t: time.strftime("%b %d, %Y", time.localtime(t))  # noqa: E731
    return FEED.format(d1=fmt(_days(3)), d2=fmt(_days(5)))


def test_parse_keeps_4k_only():
    rows = bluray.parse(_feed())
    assert [(r["title"], r["year"], r["studio"]) for r in rows] == [("Test Film", 2006, "Some Studio"), ("Another One", 1999, "Label")]
    assert rows[0]["release_date"] == time.strftime("%Y-%m-%d", time.localtime(_days(3)))


def test_sync_matches_and_merges(client, monkeypatch):
    class Resp:
        text = _feed()
        def raise_for_status(self): pass

    class Client:
        def __init__(self, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def get(self, url): return Resp()
    monkeypatch.setattr(bluray.httpx, "AsyncClient", Client)

    async def search(term):
        return {"Test Film 2006": [{"tmdb_id": 11, "title": "Test Film", "original_title": "Test Film", "year": 2006}],
                "Another One 1999": [{"tmdb_id": 99, "title": "Something Else", "year": 1999},
                                     {"tmdb_id": 12, "title": "Another One!", "year": 2000}]}.get(term, [])
    monkeypatch.setattr(metadata, "search_movies", search)
    assert asyncio.run(bluray.sync()) == 2
    assert asyncio.run(bluray.sync()) == 0  # already seen
    assert {r["title"]: r["tmdb_id"] for r in db.q("SELECT title, tmdb_id FROM bluray_releases")} == {"Test Film": 11, "Another One": 12}

    async def tmdb(path, **params):  # TMDB's own notes list: Test Film (other date) + Film 5
        if path.endswith("/release_dates"):
            d = time.strftime("%Y-%m-%d", time.localtime(_days(20)))
            return {"results": [{"iso_3166_1": "US", "release_dates": [{"type": 5, "release_date": d + "T00:00:00.000Z", "note": "4K Ultra HD"}]}]}
        return {"page": 1, "total_pages": 1, "results": [{"id": 11, "title": "Test Film"}, {"id": 5, "title": "Film 5"}]}

    async def details(tid):
        return {"tmdb_id": tid, "title": "Another One", "year": 2000, "overview": "", "poster": None}
    monkeypatch.setattr(metadata, "_tmdb", tmdb)
    monkeypatch.setattr(metadata, "movie_details", details)
    metadata._info_cache.clear()
    d = client.get("/api/new/movie?where=disc4k").json()
    got = [(r["tmdb_id"], r["date"], bool(r.get("bluray"))) for r in d["results"]]
    day = lambda n: time.strftime("%Y-%m-%d", time.localtime(_days(n)))  # noqa: E731
    assert got == [(11, day(3), True), (12, day(5), True), (5, day(20), False)]  # Blu-ray.com's date wins
