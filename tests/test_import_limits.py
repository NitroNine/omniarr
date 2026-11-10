"""Library import of existing media + per-library quality limits."""
import asyncio
import os

from app import db, decision, library, scanner
from app.services import metadata, newznab


def test_parse_folder():
    f = scanner.parse_folder
    assert f("Dune Part Two (2024) {tmdb-693134}") == {"title": "Dune Part Two", "year": 2024, "tmdb_id": 693134,
                                                       "tvdb_id": None, "imdb_id": None}
    assert f("The Bear (2022) [tvdbid-403245]")["tvdb_id"] == 403245
    assert f("Heat (1995) {imdb-tt0113277}")["imdb_id"] == "tt0113277"
    r = f("Arrival.2016.1080p.BluRay.x264-SPARKS")
    assert (r["title"], r["year"]) == ("Arrival", 2016)
    assert f("Blade Runner 2049 (2017)")["title"] == "Blade Runner 2049"
    assert f("The Office (US)")["title"] == "The Office (US)"


def _touch(path, size=1000):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(b"x" * size)


MOVIES = {
    693134: ("Dune: Part Two", 2024), 949: ("Heat", 1995), 329865: ("Arrival", 2016),
}


def _fake_movies(monkeypatch):
    async def details(tmdb_id):
        t, y = MOVIES[tmdb_id]
        return {"tmdb_id": tmdb_id, "imdb_id": None, "title": t, "original_title": t, "year": y, "overview": "",
                "poster": None, "runtime": 120, "release_date": f"{y}-01-01"}

    async def search(term):
        return [{"tmdb_id": k, "title": t, "year": y, "poster": None, "overview": ""}
                for k, (t, y) in MOVIES.items() if t.split(":")[0].lower() in term.lower()]
    monkeypatch.setattr(metadata, "movie_details", details)
    monkeypatch.setattr(metadata, "search_movies", search)


def test_movie_library_import(env, monkeypatch):
    _fake_movies(monkeypatch)
    hd = db.setting("root_movies_hd")
    _touch(os.path.join(hd, "Dune Part Two (2024) {tmdb-693134}", "Dune.Part.Two.2024.1080p.AMZN.WEB-DL.DDP5.1.H.264-FLUX.mkv"))
    _touch(os.path.join(hd, "Dune Part Two (2024) {tmdb-693134}", "Sample", "sample.mkv"), 10)
    _touch(os.path.join(hd, "Heat (1995)", "Heat.1995.BluRay.1080p.x264-AMIABLE.mkv"))
    _touch(os.path.join(hd, "Arrival.2016.1080p.BluRay.x264-SPARKS.mkv"))  # loose file in the root
    os.makedirs(os.path.join(hd, ".Recycle.Bin"))

    found = scanner.scan("movie")
    assert {c["folder"] for c in found} == {"Dune Part Two (2024) {tmdb-693134}", "Heat (1995)",
                                            "Arrival.2016.1080p.BluRay.x264-SPARKS.mkv"}
    matched = asyncio.run(scanner.match_many(found))
    by = {c["folder"]: c for c in matched}
    assert by["Dune Part Two (2024) {tmdb-693134}"]["confidence"] == "id"
    assert by["Heat (1995)"]["match"]["tmdb_id"] == 949 and by["Heat (1995)"]["confidence"] == "exact"

    items = [{"kind": "movie", "tier": c["tier"], "folder": c["folder"], "loose": c["loose"],
              "id": c["match"]["tmdb_id"], "rename": False} for c in matched]
    asyncio.run(scanner.run_import(items))
    assert scanner.JOB["imported"] == 3, scanner.JOB["errors"]

    dune = db.one("SELECT * FROM movies WHERE tmdb_id=693134")
    assert dune["folder"] == "Dune Part Two (2024) {tmdb-693134}" and dune["tier"] == "hd"
    assert dune["file_quality"] == "WEBDL-1080p" and dune["file_score"] > 0  # TRaSH scored (WEB Tier 01 + AMZN)
    assert dune["file_path"].endswith("FLUX.mkv")  # not renamed
    heat = db.one("SELECT * FROM movies WHERE tmdb_id=949")
    assert heat["file_quality"] == "Bluray-1080p" and heat["folder"] == "Heat (1995)"
    arrival = db.one("SELECT * FROM movies WHERE tmdb_id=329865")
    # loose file got its own TRaSH-named folder and file name
    assert arrival["file_path"] == os.path.join(hd, "Arrival (2016) {tmdb-329865}",
                                                "Arrival (2016) {tmdb-329865} [Bluray-1080p][x264]-SPARKS.mkv")
    assert os.path.exists(arrival["file_path"])
    assert scanner.scan("movie") == []  # everything is in the library now

    # rescan with rename
    asyncio.run(scanner.rescan_movie(dune["id"], rename=True))
    dune = db.one("SELECT * FROM movies WHERE tmdb_id=693134")
    assert os.path.basename(dune["file_path"]) == "Dune Part Two (2024) {tmdb-693134} [WEBDL-1080p][EAC3 5.1][h264]-FLUX.mkv"


def test_series_library_import(env, monkeypatch):
    async def details(tvdb_id=None, tmdb_id=None):
        return {"tvdb_id": 403245, "title": "The Bear", "year": 2022, "overview": "", "poster": None,
                "imdb_id": None, "tmdb_id": None, "runtime": 30, "status": "Continuing", "series_type": "standard"}

    async def episodes(_id):
        return [{"tvdb_id": s * 100 + e, "season": s, "episode": e, "absolute": None, "title": f"Ep {e}",
                 "air_date": f"2022-0{s}-{e:02d}", "overview": ""} for s in (1, 2) for e in (1, 2, 3)]
    monkeypatch.setattr(metadata, "series_details", details)
    monkeypatch.setattr(metadata, "series_episodes", episodes)
    root = os.path.join(db.setting("root_tv_uhd"), "The Bear (2022) [tvdbid-403245]")
    _touch(os.path.join(root, "Season 01", "The.Bear.S01E01.2160p.WEB-DL.DDP5.1.H.265-NTb.mkv"))
    _touch(os.path.join(root, "Season 01", "The.Bear.S01E02E03.2160p.WEB-DL.DDP5.1.H.265-NTb.mkv"))
    _touch(os.path.join(root, "Season 2", "Episode 1.mkv"))  # folder + episode-number fallback

    [cand] = scanner.scan("tv")
    assert cand["tier"] == "uhd" and cand["tvdb_id"] == 403245
    c = asyncio.run(scanner.match(cand))
    asyncio.run(scanner.import_one({"kind": "tv", "tier": "uhd", "folder": c["folder"], "id": c["match"]["tvdb_id"],
                                    "monitor": "existing"}))
    eps = {(e["season"], e["episode"]): e for e in db.q("SELECT * FROM episodes")}
    assert eps[(1, 1)]["file_quality"] == "WEBDL-2160p"
    assert eps[(1, 2)]["file_path"] == eps[(1, 3)]["file_path"]
    assert eps[(2, 1)]["file_path"]
    assert not eps[(2, 2)]["file_path"] and not eps[(2, 2)]["monitored"]  # "only episodes I have"
    assert eps[(1, 1)]["monitored"]


def _rel(title, gb=10):
    return newznab.Release(title=title, guid=title, link="x", size=gb * 1024 ** 3, indexer="I", indexer_id=1)


def test_quality_limits(env, monkeypatch):
    _fake_movies(monkeypatch)
    m = asyncio.run(library.add_movie(949, "hd"))
    web = _rel("Heat.1995.1080p.AMZN.WEB-DL.DDP5.1.H.264-FLUX")
    bluray = _rel("Heat.1995.1080p.BluRay.x264-AMIABLE")
    hd720 = _rel("Heat.1995.720p.BluRay.x264-AMIABLE", 5)

    # Max quality Bluray-1080p, allow WEB 1080p, no 720p, stop upgrading once Bluray-1080p is reached.
    db.save_settings({"quality_overrides": {"movies_hd": {
        "allowed": ["Bluray-1080p", "WEB 1080p"], "cutoff": "Bluray-1080p", "cutoff_score": 0}}})
    p = decision.profile_for("movie", "hd")
    assert p.quality_rank("Bluray-720p") is None and p.cutoff == "Bluray-1080p" and p.cutoff_format_score == 0

    # Only a WEB-DL is out: grab it.
    movie = db.one("SELECT * FROM movies WHERE id=?", (m["id"],))
    [d] = decision.decide_movie(movie, [web])
    assert d.accepted
    # 720p is never accepted now
    assert not decision.decide_movie(movie, [hd720])[0].accepted

    # Have the WEB-DL: the Bluray is an upgrade
    db.update("movies", m["id"], {"file_path": "/x.mkv", "file_quality": "WEBDL-1080p", "file_score": d.score})
    movie = db.one("SELECT * FROM movies WHERE id=?", (m["id"],))
    assert decision.decide_movie(movie, [bluray])[0].accepted
    assert not library.cutoff_met(decision.profile_for("movie", "hd"), "WEBDL-1080p", d.score)

    # Have the Bluray: cutoff reached, nothing else is grabbed
    db.update("movies", m["id"], {"file_quality": "Bluray-1080p", "file_score": 0})
    movie = db.one("SELECT * FROM movies WHERE id=?", (m["id"],))
    better = _rel("Heat.1995.1080p.BluRay.x264-DON")  # HD Bluray Tier 01 group in TRaSH
    r = decision.decide_movie(movie, [better])[0]
    assert not r.accepted and "cutoff" in r.reasons[0]
    assert library.cutoff_met(decision.profile_for("movie", "hd"), "Bluray-1080p", 0)

    # Upgrades switched off entirely
    db.save_settings({"quality_overrides": {"movies_hd": {"upgrades": False}}})
    db.update("movies", m["id"], {"file_quality": "WEBDL-1080p", "file_score": 0})
    movie = db.one("SELECT * FROM movies WHERE id=?", (m["id"],))
    assert not decision.decide_movie(movie, [bluray])[0].accepted

    # Reset -> TRaSH defaults (720p allowed again in HD Bluray + WEB)
    db.save_settings({"quality_overrides": {}})
    assert decision.profile_for("movie", "hd").quality_rank("Bluray-720p") is not None
