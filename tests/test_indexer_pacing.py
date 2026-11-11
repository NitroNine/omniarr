"""Indexer requests are spaced out, a 429 pauses that indexer, and API keys never reach the log."""
import asyncio
import time

import httpx
import pytest

from app import db, library, search
from app.services import metadata, newznab


@pytest.fixture()
def net(monkeypatch):
    """Fake indexer: records request times; answers 429 while `limit` is set."""
    state = {"times": [], "limit": False, "retry_after": None}

    def handler(req: httpx.Request):
        state["times"].append(time.monotonic())
        if state["limit"]:
            h = {"Retry-After": state["retry_after"]} if state["retry_after"] else {}
            return httpx.Response(429, headers=h, text=state.get("body", ""))
        if req.url.params.get("t") == "get":
            return httpx.Response(200, content=b'<?xml version="1.0"?><nzb></nzb>')
        return httpx.Response(200, text='<rss><channel><item><title>Heat.1995.1080p.BluRay.x264-AMIABLE</title>'
                                        '<guid>g1</guid><link>http://idx.test/api?t=get&amp;id=1&amp;apikey=SECRET</link>'
                                        '<enclosure url="http://idx.test/api?t=get&amp;id=1&amp;apikey=SECRET" length="12884901888"/>'
                                        '</item></channel></rss>')
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(newznab, "MIN_INTERVAL", 0.2)
    newznab._gates.clear()
    newznab.resume_at = 0.0
    db.ex("UPDATE indexers SET url='http://idx.test', apikey='SECRET'")
    yield state
    newznab._gates.clear()


def test_requests_are_spaced(env, net):
    ix = newznab.enabled_indexers()[0]

    async def burst():
        await asyncio.gather(*(newznab._query(ix, {"t": "search", "q": f"x{i}"}) for i in range(4)))
    asyncio.run(burst())
    gaps = [b - a for a, b in zip(net["times"], net["times"][1:])]
    assert len(net["times"]) == 4 and min(gaps) >= 0.19


def test_429_pauses_indexer_and_hides_key(env, net):
    ix = newznab.enabled_indexers()[0]
    net["limit"], net["retry_after"] = True, "120"
    with pytest.raises(newznab.IndexerBusy) as e:
        asyncio.run(newznab._query(ix, {"t": "search", "q": "x"}))
    assert 115 <= e.value.until - time.time() <= 121
    assert newznab.resume_at == e.value.until + 5  # the backlog search re-runs once the pause ends
    n = len(net["times"])
    with pytest.raises(newznab.IndexerBusy):  # paused: doesn't even ask
        asyncio.run(newznab.fetch_nzb("http://idx.test/api?t=get&id=1&apikey=SECRET"))
    assert len(net["times"]) == n
    logs = [r["message"] for r in db.q("SELECT message FROM events")]
    assert sum("too many requests" in m for m in logs) == 1
    assert not any("SECRET" in m for m in logs)


def test_search_waits_for_paused_indexer(env, net, monkeypatch):
    async def details(_):
        return {"tmdb_id": 1, "imdb_id": None, "title": "Heat", "original_title": "Heat", "year": 1995,
                "overview": "", "poster": None, "runtime": 170, "release_date": "1995-12-15"}
    monkeypatch.setattr(metadata, "movie_details", details)
    m = asyncio.run(library.add_movie(1, "hd"))
    net["limit"] = True
    with pytest.raises(newznab.IndexerBusy):
        asyncio.run(search.search_movie(m["id"]))
    assert db.one("SELECT last_search FROM movies")["last_search"] is None  # searched again later
    asyncio.run(search.missing_search())  # stops quietly
    assert not db.q("SELECT 1 FROM events WHERE level='error'")


def test_redact():
    assert newznab.redact("https://x/api?t=get&apikey=abc123&id=9") == "https://x/api?t=get&apikey=***&id=9"
    assert newznab.redact("https://x/rss?r=tok&i=5") == "https://x/rss?r=***&i=5"
    db_msg = "Client error for url 'https://nzbs.test/api?t=get&apikey=awngo&id=1'"
    assert "awngo" not in newznab.redact(db_msg)


def test_old_log_lines_scrubbed(env):
    db.ex("INSERT INTO events(level, message) VALUES('warn', ?)", ("failed 'https://n/api?t=get&apikey=LEAK&id=1'",))
    db._migrate(db.connect())
    assert "LEAK" not in db.one("SELECT message FROM events ORDER BY id DESC")["message"]


def test_backlog_searches_every_new_title(env, monkeypatch):
    """Unreleased titles at the front of the backlog don't crowd out new adds; re-searches stay capped."""
    for i in range(40):  # 15 unreleased, 25 new and never searched
        db.ex("INSERT INTO movies (tmdb_id, title, release_date) VALUES (?,?,?)",
              (1000 + i, f"M{i}", "2099-01-01" if i < 15 else "2020-01-01"))
    for i in range(12):  # searched two days ago
        db.ex("INSERT INTO movies (tmdb_id, title, release_date, last_search) VALUES (?,?,?,datetime('now','-2 days'))",
              (2000 + i, f"Old{i}", "2020-01-01"))
    monkeypatch.setattr(library, "movie_available", lambda m: m["release_date"] < "2050")
    searched = []

    async def fake(mid, auto_grab=True):
        searched.append(db.one("SELECT title FROM movies WHERE id=?", (mid,))["title"])
    monkeypatch.setattr(search, "search_movie", fake)
    asyncio.run(search.missing_search(limit=5))
    assert sum(not t.startswith("Old") for t in searched) == 25
    assert sum(t.startswith("Old") for t in searched) == 5


def test_daily_limit_pauses_till_midnight(env, net):
    """nzbs.in: 429 "Try again after midnight" -> retry at most hourly, and say why in the log."""
    net["limit"] = True
    net["body"] = '<?xml version="1.0"?><response><error>Too many API requests. Slow down. Try again after midnight.</error></response>'
    with pytest.raises(newznab.IndexerBusy) as e:
        asyncio.run(newznab.fetch_nzb("http://idx.test/api?t=get&id=1&apikey=SECRET"))
    assert 60 <= e.value.until - time.time() <= 3600
    msg = db.one("SELECT message FROM events WHERE message LIKE '%429%'")["message"]
    assert "Try again after midnight" in msg and "SECRET" not in msg


def test_grab_falls_back_to_other_indexer(env, monkeypatch):
    """Best release is on an indexer that refuses downloads today: take the next one from another indexer."""
    from types import SimpleNamespace as NS
    db.ex("INSERT INTO movies (tmdb_id, title, release_date) VALUES (1, 'Heat', '1995-12-15')")
    mid = db.one("SELECT id FROM movies")["id"]
    ds = [NS(accepted=True, release=NS(link=f"http://{h}/api?t=get&id=1", indexer=h), to_dict=dict)
          for h in ("busy.test", "other.test")]

    async def none(_):
        return []
    monkeypatch.setattr(newznab, "search_movie", none)
    monkeypatch.setattr(search.decision, "decide_movie", lambda m, r: ds)
    monkeypatch.setattr(search.decision, "active_download_for", lambda k, i: False)
    tried = []

    async def grab(d, kind, movie_id=None):
        tried.append(d.release.indexer)
        if d.release.indexer == "busy.test":
            raise newznab.IndexerBusy("busy.test", time.time() + 60)
        return 1
    monkeypatch.setattr(search, "grab", grab)
    asyncio.run(search.search_movie(mid))
    assert tried == ["busy.test", "other.test"]
    assert db.one("SELECT last_search FROM movies")["last_search"]
    ds.pop()  # only the busy indexer has it: no stamp, raise so it's retried later
    db.ex("UPDATE movies SET last_search=NULL")
    with pytest.raises(newznab.IndexerBusy):
        asyncio.run(search.search_movie(mid))
    assert db.one("SELECT last_search FROM movies")["last_search"] is None
