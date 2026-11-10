"""TRaSH scoring and an end-to-end movie + episode flow against fake services."""
import asyncio
import os

from app import db, decision, importer, library, search, trash
from app.parser import parse
from app.services import metadata, newznab, nzbget


def test_trash_profile_and_scores(env):
    p = decision.profile_for("movie", "hd")
    assert p.name == "HD Bluray + WEB"
    assert p.quality_rank("Bluray-2160p") is None  # 4K not allowed in the 1080p library
    assert p.quality_rank("WEBDL-1080p") is not None
    score, cfs = trash.score_release(p, parse("Movie.2024.1080p.AMZN.WEB-DL.DDP5.1.H.264-FLUX"))
    assert "WEB Tier 01" in cfs and score > 0
    score, cfs = trash.score_release(p, parse("Movie.2024.COMPLETE.BLURAY.AVC-GRP"))
    assert "BR-DISK" in cfs and score < 0  # pulled in from the Unwanted cf-group
    p4 = decision.profile_for("movie", "uhd")
    assert p4.quality_rank("Bluray-2160p") is not None and p4.quality_rank("WEBDL-720p") is None


def _feed(titles):
    return [newznab.Release(title=t, guid=t, link="http://idx/get/" + t, size=s, indexer="FakeIdx", indexer_id=1)
            for t, s in titles]


def test_movie_end_to_end(env, monkeypatch):
    async def details(_tmdb_id):
        return {"tmdb_id": 693134, "imdb_id": "tt15239678", "title": "Dune: Part Two", "original_title": "Dune: Part Two",
                "year": 2024, "overview": "", "poster": None, "runtime": 166, "release_date": "2024-02-27"}
    monkeypatch.setattr(metadata, "movie_details", details)
    gb = 1024 ** 3
    releases = _feed([
        ("Dune.Part.Two.2024.1080p.BluRay.x264-SPARKS", 12 * gb),
        ("Dune.Part.Two.2024.1080p.AMZN.WEB-DL.DDP5.1.H.264-FLUX", 9 * gb),
        ("Dune.Part.Two.2024.2160p.WEB-DL.DDP5.1.HDR.H.265-FLUX", 20 * gb),   # wrong tier
        ("Dune.Part.Two.2024.COMPLETE.BLURAY-GRP", 45 * gb),                    # BR-DISK
        ("Dune.2021.1080p.BluRay.x264-SPARKS", 12 * gb),                        # wrong movie
        ("Dune.Part.Two.2024.1080p.WEB-DL.H.264-TINY", 50 * 1024 ** 2),         # too small
    ])

    async def fake_search(_movie):
        return releases
    sent = {}

    async def fake_fetch(link):
        return b"<?xml version='1.0'?><nzb></nzb>"

    async def fake_call(method, *params):
        if method == "append":
            sent["name"], sent["cat"] = params[0], params[2]
            return 42
        if method == "listgroups":
            return []
        if method == "history":
            return [{"NZBID": 42, "Status": "SUCCESS/ALL", "DestDir": str(dl_dir), "FinalDir": ""}]
        return None
    monkeypatch.setattr(newznab, "search_movie", fake_search)
    monkeypatch.setattr(newznab, "fetch_nzb", fake_fetch)
    monkeypatch.setattr(nzbget, "call", fake_call)

    m = asyncio.run(library.add_movie(693134, "hd"))
    assert m["folder"] == "Dune Part Two (2024) {tmdb-693134}"
    results = asyncio.run(search.search_movie(m["id"]))
    accepted = [r for r in results if r["accepted"]]
    titles = {r["title"]: r for r in results}
    assert not titles["Dune.Part.Two.2024.2160p.WEB-DL.DDP5.1.HDR.H.265-FLUX"]["accepted"]
    assert not titles["Dune.Part.Two.2024.COMPLETE.BLURAY-GRP"]["accepted"]
    assert not titles["Dune.2021.1080p.BluRay.x264-SPARKS"]["accepted"]
    assert not titles["Dune.Part.Two.2024.1080p.WEB-DL.H.264-TINY"]["accepted"]
    # TRaSH HD Bluray + WEB ranks Bluray-1080p above WEB 1080p
    assert accepted[0]["title"] == "Dune.Part.Two.2024.1080p.BluRay.x264-SPARKS"
    assert sent["cat"] == "movies"

    # NZBGet finishes -> import
    dl_dir = env["tmp"] / "usenet" / "Dune.Part.Two.2024.1080p.BluRay.x264-SPARKS"
    dl_dir.mkdir(parents=True)
    (dl_dir / "dune.part.two.mkv").write_bytes(b"x" * 1000)
    (dl_dir / "sample.mkv").write_bytes(b"x" * 10)
    (dl_dir / "dune.part.two.en.srt").write_text("1")
    asyncio.run(importer.process_downloads())
    movie = db.one("SELECT * FROM movies WHERE id=?", (m["id"],))
    expected_dir = os.path.join(db.setting("root_movies_hd"), "Dune Part Two (2024) {tmdb-693134}")
    assert movie["file_path"].startswith(expected_dir)
    assert os.path.basename(movie["file_path"]) == "Dune Part Two (2024) {tmdb-693134} [Bluray-1080p][x264]-SPARKS.mkv"
    assert os.path.exists(movie["file_path"])
    assert os.path.exists(movie["file_path"][:-4] + ".en.srt")
    assert db.one("SELECT status FROM downloads")["status"] == "imported"

    # Switching to the 4K library moves the folder
    library.change_tier("movie", m["id"], "uhd")
    movie = db.one("SELECT * FROM movies WHERE id=?", (m["id"],))
    assert movie["file_path"].startswith(db.setting("root_movies_uhd")) and os.path.exists(movie["file_path"])


def test_series_end_to_end(env, monkeypatch):
    async def details(tvdb_id=None, tmdb_id=None):
        return {"tvdb_id": 403245, "title": "The Bear", "year": 2022, "overview": "", "poster": None,
                "imdb_id": "tt14452776", "tmdb_id": 136315, "runtime": 30, "status": "Continuing",
                "series_type": "standard"}

    async def episodes(_id):
        return [{"tvdb_id": i, "season": 1, "episode": i, "absolute": i, "title": f"Ep {i}",
                 "air_date": f"2022-06-{i:02d}", "overview": ""} for i in range(1, 4)] + \
               [{"tvdb_id": 99, "season": 2, "episode": 1, "absolute": 4, "title": "Future",
                 "air_date": "2099-01-01", "overview": ""}]
    monkeypatch.setattr(metadata, "series_details", details)
    monkeypatch.setattr(metadata, "series_episodes", episodes)
    gb = 1024 ** 3

    async def fake_search(_s, season, ep=None):
        return _feed([("The.Bear.S01.1080p.HULU.WEB-DL.DDP5.1.H.264-NTb", 6 * gb),
                      ("The.Bear.S01E01.1080p.WEB.h264-ETHEL", 1 * gb),
                      ("The.Bear.S01.2160p.WEB-DL.DDP5.1.H.265-NTb", 20 * gb)])
    hist = []

    async def fake_call(method, *params):
        if method == "append":
            return 7
        if method == "history":
            return hist
        return []

    async def fake_fetch(link):
        return b"<nzb/>"
    monkeypatch.setattr(newznab, "search_episodes", fake_search)
    monkeypatch.setattr(newznab, "fetch_nzb", fake_fetch)
    monkeypatch.setattr(nzbget, "call", fake_call)

    s = asyncio.run(library.add_series(403245, "hd"))
    assert len(library.wanted_episodes(s["id"])) == 3  # future episode not wanted yet
    asyncio.run(search.search_series(s["id"]))
    dls = db.q("SELECT * FROM downloads")
    assert len(dls) == 1 and dls[0]["release_title"] == "The.Bear.S01.1080p.HULU.WEB-DL.DDP5.1.H.264-NTb"

    d = env["tmp"] / "usenet" / "The.Bear.S01"
    d.mkdir(parents=True)
    for i in range(1, 4):
        (d / f"The.Bear.S01E0{i}.1080p.HULU.WEB-DL.DDP5.1.H.264-NTb.mkv").write_bytes(b"x")
    hist.append({"NZBID": 7, "Status": "SUCCESS/ALL", "DestDir": str(d)})
    asyncio.run(importer.process_downloads())
    eps = db.q("SELECT * FROM episodes WHERE season=1 ORDER BY episode")
    assert all(e["file_path"] for e in eps)
    assert eps[0]["file_path"] == os.path.join(
        db.setting("root_tv_hd"), "The Bear (2022) {tvdb-403245}", "Season 01",
        "The Bear (2022) - S01E01 - Ep 1 [WEBDL-1080p][EAC3 5.1][h264]-NTb.mkv")


def test_failed_download_blocklists(env, monkeypatch):
    async def details(_):
        return {"tmdb_id": 1, "imdb_id": None, "title": "Heat", "original_title": "Heat", "year": 1995,
                "overview": "", "poster": None, "runtime": 170, "release_date": "1995-12-15"}
    monkeypatch.setattr(metadata, "movie_details", details)
    calls = {"n": 0}

    async def fake_search(_m):
        calls["n"] += 1
        return _feed([("Heat.1995.1080p.BluRay.x264-AMIABLE", 12 * 1024 ** 3)])

    async def fake_call(method, *params):
        if method == "append":
            return 5
        if method == "history":
            return [{"NZBID": 5, "Status": "FAILURE/PAR", "DestDir": "/nope"}]
        return []

    async def fake_fetch(link):
        return b"<nzb/>"
    monkeypatch.setattr(newznab, "search_movie", fake_search)
    monkeypatch.setattr(newznab, "fetch_nzb", fake_fetch)
    monkeypatch.setattr(nzbget, "call", fake_call)
    m = asyncio.run(library.add_movie(1, "hd"))
    asyncio.run(search.search_movie(m["id"]))
    asyncio.run(importer.process_downloads())
    assert db.one("SELECT status FROM downloads")["status"] == "failed"
    assert db.one("SELECT release_title FROM blocklist")["release_title"] == "Heat.1995.1080p.BluRay.x264-AMIABLE"
    assert calls["n"] == 2  # re-searched, but the blocklisted release isn't grabbed again
    assert db.one("SELECT COUNT(*) c FROM downloads")["c"] == 1
