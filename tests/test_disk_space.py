"""Omniarr mustn't fill the download disk, and a full disk mustn't blocklist good releases."""
import asyncio

from app import db, importer, library, search
from app.services import metadata, newznab, nzbget

GB = 1024 ** 3


def _heat(monkeypatch):
    async def details(_):
        return {"tmdb_id": 1, "imdb_id": None, "title": "Heat", "original_title": "Heat", "year": 1995,
                "overview": "", "poster": None, "runtime": 170, "release_date": "1995-12-15"}
    monkeypatch.setattr(metadata, "movie_details", details)

    async def fake_search(_m):
        return [newznab.Release(title="Heat.1995.1080p.BluRay.x264-AMIABLE", guid="g1", link="http://idx/1",
                                size=12 * GB, indexer="FakeIdx", indexer_id=1)]

    async def fake_fetch(_link):
        return b"<nzb/>"
    monkeypatch.setattr(newznab, "search_movie", fake_search)
    monkeypatch.setattr(newznab, "fetch_nzb", fake_fetch)
    return asyncio.run(library.add_movie(1, "hd"))


def _nzbget(monkeypatch, free_gb, history=None):
    calls = []

    async def fake_call(method, *params):
        calls.append(method)
        if method == "status":
            return {"FreeDiskSpaceMB": free_gb * 1024}
        if method == "append":
            return 5
        if method == "history":
            return history or []
        return []
    monkeypatch.setattr(nzbget, "call", fake_call)
    return calls


def test_no_grab_without_room(env, monkeypatch):
    m = _heat(monkeypatch)
    calls = _nzbget(monkeypatch, free_gb=30)  # 2 x 12 GB + 20 GB reserve = 44 GB needed
    try:
        asyncio.run(search.search_movie(m["id"]))
        raise AssertionError("expected NoSpace")
    except search.NoSpace as e:
        assert "30 GB free" in str(e)
    assert "append" not in calls
    assert db.one("SELECT COUNT(*) c FROM downloads")["c"] == 0
    assert db.one("SELECT last_search FROM movies")["last_search"] is None  # tried again later
    asyncio.run(search.missing_search())  # the backlog search stops quietly
    assert db.one("SELECT COUNT(*) c FROM downloads")["c"] == 0

    _nzbget(monkeypatch, free_gb=50)
    asyncio.run(search.search_movie(m["id"]))
    assert db.one("SELECT COUNT(*) c FROM downloads")["c"] == 1


def test_queue_counts_against_space(env, monkeypatch):
    m = _heat(monkeypatch)
    db.insert("downloads", {"kind": "tv", "release_title": "Other.S01.1080p", "size": 40 * GB,
                            "progress": 50, "status": "downloading"})
    _nzbget(monkeypatch, free_gb=60)  # 2 x (20 left + 12) + 20 = 84 GB needed
    try:
        asyncio.run(search.search_movie(m["id"]))
        raise AssertionError("expected NoSpace")
    except search.NoSpace:
        pass


def test_manual_grab_reports_no_space(client, monkeypatch):
    m = _heat(monkeypatch)
    _nzbget(monkeypatch, free_gb=5)
    r = client.post("/api/grab", json={"kind": "movie", "item_id": m["id"], "title": "Heat.1995.1080p.BluRay.x264-AMIABLE",
                                       "guid": "g1", "link": "http://idx/1", "size": 12 * GB,
                                       "indexer": "FakeIdx", "indexer_id": 1})
    assert r.status_code == 507
    assert "Not enough space" in r.json()["detail"]


def test_disk_full_failure_not_blocklisted(env, monkeypatch, tmp_path):
    m = _heat(monkeypatch)
    dl_root = tmp_path / "dl"
    partial = dl_root / "intermediate" / "Heat.1995.1080p.BluRay.x264-AMIABLE.#5"
    partial.mkdir(parents=True)
    (partial / "part.rar").write_bytes(b"x")
    db.save_settings({"download_dir": str(dl_root)})
    _nzbget(monkeypatch, free_gb=100)
    asyncio.run(search.search_movie(m["id"]))
    calls = _nzbget(monkeypatch, free_gb=100,
                    history=[{"NZBID": 5, "Status": "FAILURE/MOVE", "DestDir": str(partial)}])
    asyncio.run(importer.process_downloads())
    dl = db.one("SELECT * FROM downloads")
    assert dl["status"] == "failed" and "ran out of space" in dl["message"]
    assert db.one("SELECT COUNT(*) c FROM blocklist")["c"] == 0
    assert not partial.exists()  # half-finished files removed
    assert "editqueue" in calls  # dropped from NZBGet's history
    assert db.one("SELECT COUNT(*) c FROM downloads")["c"] == 1  # no instant re-grab storm


def test_failure_while_disk_nearly_full_not_blocklisted(env, monkeypatch):
    m = _heat(monkeypatch)
    _nzbget(monkeypatch, free_gb=100)
    asyncio.run(search.search_movie(m["id"]))
    _nzbget(monkeypatch, free_gb=1, history=[{"NZBID": 5, "Status": "FAILURE/UNPACK", "DestDir": "/nope"}])
    asyncio.run(importer.process_downloads())
    assert db.one("SELECT COUNT(*) c FROM blocklist")["c"] == 0


def test_failure_retries_capped(env, monkeypatch):
    m = _heat(monkeypatch)
    n = {"i": 0}

    async def many(_m):  # a fresh release on every search
        n["i"] += 1
        return [newznab.Release(title=f"Heat.1995.1080p.BluRay.x264-GRP{n['i']}", guid=f"g{n['i']}",
                                link=f"http://idx/{n['i']}", size=12 * GB, indexer="FakeIdx", indexer_id=1)]
    monkeypatch.setattr(newznab, "search_movie", many)
    nid = {"v": 0}

    async def fake_call(method, *params):
        if method == "append":
            nid["v"] += 1
            return nid["v"]
        if method == "history":  # everything fails its repair
            return [{"NZBID": i, "Status": "FAILURE/PAR", "DestDir": "/nope"} for i in range(1, nid["v"] + 1)]
        if method == "status":
            return {"FreeDiskSpaceMB": 500 * 1024}
        return []
    monkeypatch.setattr(nzbget, "call", fake_call)
    asyncio.run(search.search_movie(m["id"]))
    for _ in range(10):
        asyncio.run(importer.process_downloads())
    # the first grab plus 3 retries, then it waits for the backlog search
    assert db.one("SELECT COUNT(*) c FROM downloads")["c"] == 4
    assert db.one("SELECT COUNT(*) c FROM blocklist")["c"] == 4
