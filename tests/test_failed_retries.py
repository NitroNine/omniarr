import asyncio

from app import db, importer, search


def test_four_replacement_nzbs_after_health_failures(env, monkeypatch):
    assert db.setting("failed_retry_limit") == 4
    mid = db.insert("movies", {"tmdb_id": 1, "title": "Film", "tier": "uhd", "monitored": 1})
    searches = []

    async def research(movie_id, *a, **k):
        searches.append(movie_id)
    monkeypatch.setattr(search, "search_movie", research)
    for n in range(6):  # the first grab and five replacements all fail their health check
        did = db.insert("downloads", {"kind": "movie", "movie_id": mid, "release_title": f"Film.2026.Remux-{n}",
                                      "guid": f"g{n}", "status": "downloading"})
        asyncio.run(importer.fail_download(db.one("SELECT * FROM downloads WHERE id=?", (did,)), "FAILURE/HEALTH"))
    assert len(searches) == 4  # 4 different NZBs after the first failure, then the backlog search takes over
    assert db.one("SELECT COUNT(*) n FROM blocklist")["n"] == 6
