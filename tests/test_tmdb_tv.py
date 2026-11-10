"""TV comes from TMDB: shows, episodes and the TheTVDB/IMDb ids indexers need."""
import asyncio
import sqlite3

from app import db, library
from app.services import metadata, newznab

TV = {
    1399: {"id": 1399, "name": "Game of Thrones", "first_air_date": "2011-04-17", "overview": "", "poster_path": "/g.jpg",
           "episode_run_time": [60], "status": "Ended", "genres": [{"name": "Drama"}], "original_language": "en",
           "seasons": [{"season_number": 0}, {"season_number": 1}, {"season_number": 2}],
           "external_ids": {"tvdb_id": 121361, "imdb_id": "tt0944947"}},
    999: {"id": 999, "name": "Brand New Show", "first_air_date": "2026-01-05", "overview": "", "poster_path": None,
          "episode_run_time": [], "status": "Returning Series", "genres": [], "original_language": "en",
          "seasons": [{"season_number": 1}], "external_ids": {"tvdb_id": None, "imdb_id": "tt9999999"}},
}
SEASONS = {
    (1399, 0): [{"episode_number": 1, "name": "Making Of", "air_date": "2011-01-01"}],
    (1399, 1): [{"episode_number": i, "name": f"S1E{i}", "air_date": f"2011-04-{10 + i}"} for i in (1, 2, 3)],
    (1399, 2): [{"episode_number": 1, "name": "The North Remembers", "air_date": "2012-04-01"},
                {"episode_number": 2, "name": "Future ep", "air_date": None}],
    (999, 1): [{"episode_number": 1, "name": "Pilot", "air_date": "2026-01-05"}],
}
CALLS = []


def _fake_tmdb(monkeypatch):
    async def tmdb(path, **params):
        CALLS.append((path, params))
        parts = path.strip("/").split("/")
        if parts[0] == "search" and parts[1] == "tv":
            return {"results": [{k: v for k, v in TV[1399].items() if k in ("id", "name", "first_air_date", "overview", "poster_path")}]}
        if parts[0] == "tv" and len(parts) == 2:
            return TV[int(parts[1])]
        if parts[0] == "tv" and parts[2] == "season":
            return {"episodes": SEASONS[(int(parts[1]), int(parts[3]))]}
        if parts[0] == "find":
            hit = [t for t in TV.values() if str(t["external_ids"]["tvdb_id"]) == parts[1]
                   or t["external_ids"]["imdb_id"] == parts[1]]
            return {"tv_results": [{"id": hit[0]["id"]}] if hit else []}
        raise AssertionError(path)
    monkeypatch.setattr(metadata, "_tmdb", tmdb)
    CALLS.clear()


def test_old_tvdb_settings_are_removed(tmp_path):
    path = str(tmp_path / "o.db")
    db.reset_for_tests(path)
    db.ex("INSERT OR REPLACE INTO settings (key, value) VALUES ('tvdb_api_key', 'old'), ('tv_metadata_source', 'tvdb')")
    db.reset_for_tests(path)
    assert db.q("SELECT key FROM settings WHERE key IN ('tvdb_api_key', 'tv_metadata_source')") == []


def test_add_show_from_tmdb(env, monkeypatch):
    _fake_tmdb(monkeypatch)
    [r] = asyncio.run(metadata.search_series("game of thrones"))
    assert r["tmdb_id"] == 1399 and r["title"] == "Game of Thrones" and r["year"] == 2011
    s = asyncio.run(library.add_series(None, "uhd", tmdb_id=1399))
    row = db.one("SELECT * FROM series WHERE id=?", (s["id"],))
    assert (row["tmdb_id"], row["tvdb_id"], row["imdb_id"], row["runtime"]) == (1399, 121361, "tt0944947", 60)
    assert row["folder"] == "Game of Thrones (2011) {tvdb-121361}"  # TheTVDB id still known via TMDB
    eps = db.q("SELECT season, episode, absolute, title, monitored FROM episodes ORDER BY season, episode")
    assert [(e["season"], e["episode"]) for e in eps] == [(0, 1), (1, 1), (1, 2), (1, 3), (2, 1), (2, 2)]
    assert [e["absolute"] for e in eps] == [None, 1, 2, 3, 4, 5]
    assert eps[0]["monitored"] == 0  # specials not monitored
    # adding the same show by its TheTVDB id is caught as a duplicate
    try:
        asyncio.run(library.add_series(121361, "hd"))
        raise AssertionError("duplicate allowed")
    except ValueError as e:
        assert "already" in str(e)
    # refresh keeps working
    SEASONS[(1399, 2)][1]["air_date"] = "2012-04-08"
    asyncio.run(library.refresh_series(s["id"]))
    assert db.one("SELECT air_date FROM episodes WHERE season=2 AND episode=2")["air_date"] == "2012-04-08"


def test_show_without_tvdb_id_and_indexer_fallback(env, monkeypatch):
    _fake_tmdb(monkeypatch)
    s = asyncio.run(library.add_series(None, "hd", tmdb_id=999))
    row = db.one("SELECT * FROM series WHERE id=?", (s["id"],))
    assert row["tvdb_id"] is None and row["tmdb_id"] == 999
    assert row["folder"] == "Brand New Show (2026)"

    queries = []

    async def fake_query(ix, params):
        queries.append({k: v for k, v in params.items() if k in ("tvdbid", "imdbid", "q")})
        return []
    monkeypatch.setattr(newznab, "_query", fake_query)
    asyncio.run(newznab.search_episodes(row, 1, 1))
    assert queries == [{"imdbid": "9999999"}, {"q": "Brand New Show"}]

    queries.clear()
    row2 = {**row, "tvdb_id": 121361, "title": "Game of Thrones"}
    asyncio.run(newznab.search_episodes(row2, 1, 1))
    assert queries[0] == {"tvdbid": 121361}


def test_request_tv_without_tvdb(client, monkeypatch):
    _fake_tmdb(monkeypatch)
    from app import auth
    from fastapi.testclient import TestClient

    from app.main import app
    auth.create_user("viewer", "viewerpass", auto_approve=True)
    v = TestClient(app)
    v.post("/api/auth/login", json={"username": "viewer", "password": "viewerpass"})
    r = v.post("/api/requests", json={"kind": "tv", "tmdb_id": 999, "search": False}).json()
    assert r["status"] == "approved" and r["source"] == "tmdb" and r["title"] == "Brand New Show"
    assert db.one("SELECT tmdb_id FROM series")["tmdb_id"] == 999
    # lookups mark it as in the library
    assert client.get("/api/lookup/series", params={"term": "tmdb:999"}).json()[0]["in_library"]


def test_migrates_v1_series_table(tmp_path):
    """Databases from before this change required a TheTVDB id; they're upgraded in place."""
    path = tmp_path / "old.db"
    c = sqlite3.connect(path)
    c.executescript("""
      CREATE TABLE series (id INTEGER PRIMARY KEY, tvdb_id INTEGER UNIQUE NOT NULL, imdb_id TEXT, tmdb_id INTEGER,
        title TEXT NOT NULL, year INTEGER, overview TEXT, poster TEXT, runtime INTEGER, status TEXT,
        series_type TEXT DEFAULT 'standard', tier TEXT NOT NULL DEFAULT 'hd', monitored INTEGER DEFAULT 1,
        folder TEXT, added TEXT DEFAULT CURRENT_TIMESTAMP, last_refresh TEXT);
      CREATE TABLE episodes (id INTEGER PRIMARY KEY, series_id INTEGER NOT NULL REFERENCES series(id) ON DELETE CASCADE,
        tvdb_id INTEGER, season INTEGER NOT NULL, episode INTEGER NOT NULL, absolute INTEGER, title TEXT, air_date TEXT,
        overview TEXT, monitored INTEGER DEFAULT 1, file_path TEXT, file_quality TEXT, file_score INTEGER,
        file_size INTEGER, last_search TEXT, UNIQUE(series_id, season, episode));
      INSERT INTO series(id, tvdb_id, title, tier, folder) VALUES (1, 81189, 'Breaking Bad', 'hd', 'Breaking Bad (2008)');
      INSERT INTO episodes(series_id, season, episode, file_path) VALUES (1, 1, 1, '/x.mkv');
    """)
    c.commit()
    c.close()
    db.reset_for_tests(str(path))
    db.connect()
    assert db.one("SELECT title FROM series WHERE tvdb_id=81189")["title"] == "Breaking Bad"
    assert db.one("SELECT file_path FROM episodes")["file_path"] == "/x.mkv"
    db.insert("series", {"tmdb_id": 999, "title": "No TVDB", "tier": "hd", "folder": "x"})  # tvdb_id now optional
    assert db.one("SELECT COUNT(*) c FROM series")["c"] == 2
    db.ex("DELETE FROM series WHERE id=1")  # foreign keys still enforced after the rebuild
    assert db.one("SELECT COUNT(*) c FROM episodes")["c"] == 0
