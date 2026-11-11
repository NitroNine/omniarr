import asyncio
import os

from app import db, library, scanner, search
from app.services import metadata

TITLES = {7: ("Merry Little Mix-Up", "2026-11-20"), 8: ("Home Alone", "1990-11-16")}


def _fake_details(monkeypatch):
    async def details(tmdb_id):
        t, d = TITLES[tmdb_id]
        return {"tmdb_id": tmdb_id, "imdb_id": None, "title": t, "original_title": t, "year": int(d[:4]),
                "overview": "", "poster": None, "runtime": 90, "release_date": d}
    monkeypatch.setattr(metadata, "movie_details", details)


def test_added_into_christmas_folder_and_both_versions_follow(env, monkeypatch):
    _fake_details(monkeypatch)
    hd = asyncio.run(library.add_movie(7, "hd", christmas=True))
    assert hd["folder"] == "Christmas Movies/Merry Little Mix-Up (2026) {tmdb-7}" and hd["christmas"] == 1
    assert library.movie_dir(hd) == os.path.join(db.setting("root_movies"), "Christmas Movies", "Merry Little Mix-Up (2026) {tmdb-7}")
    uhd = asyncio.run(library.add_movie(7, "uhd"))  # the other version shares the folder
    assert uhd["folder"] == hd["folder"] and uhd["christmas"] == 1


def test_move_existing_movie_in_and_out(env, monkeypatch):
    _fake_details(monkeypatch)
    m = asyncio.run(library.add_movie(8, "hd"))
    d = library.movie_dir(m)
    os.makedirs(d)
    f = os.path.join(d, "Home Alone (1990).mkv")
    open(f, "w").close()
    db.update("movies", m["id"], {"file_path": f})
    m = library.set_christmas(m["id"], True)
    root = db.setting("root_movies")
    assert m["folder"] == "Christmas Movies/Home Alone (1990) {tmdb-8}" and m["christmas"] == 1
    assert m["file_path"] == os.path.join(root, "Christmas Movies", "Home Alone (1990) {tmdb-8}", "Home Alone (1990).mkv")
    assert os.path.isfile(m["file_path"]) and not os.path.exists(d)
    m = library.set_christmas(m["id"], False)
    assert m["folder"] == "Home Alone (1990) {tmdb-8}" and os.path.isfile(os.path.join(d, "Home Alone (1990).mkv"))


def test_christmas_download_category(env, monkeypatch):
    _fake_details(monkeypatch)
    m = asyncio.run(library.add_movie(7, "hd", christmas=True))
    from app.services import downloader, nzbget
    assert "christmas" in [v for k, v in downloader.options().items() if k.endswith(".Name") and k.startswith("Category")]
    sent = {}

    async def add(nzb, name, cat, **kw):
        sent["cat"] = cat
        return 1
    monkeypatch.setattr(nzbget, "add", add)
    monkeypatch.setattr(search, "check_space", lambda size: asyncio.sleep(0))
    from app import decision
    from app.services.newznab import Release
    rel = Release(title="Merry.Little.Mix-Up.2026.1080p.WEB-DL.H264-GRP", guid="x", link="", size=1 << 30,
                  indexer="FakeIdx", indexer_id=1)
    d = decision.evaluate(rel, "movie", decision.profile_for("movie", "hd"))
    asyncio.run(search.grab(d, "movie", movie_id=m["id"], nzb=b"<nzb></nzb>"))
    assert sent["cat"] == "christmas"


def test_library_scan_looks_inside_christmas_folder(env):
    root = db.setting("root_movies")
    os.makedirs(os.path.join(root, "Christmas Movies", "Elf (2003)"))
    os.makedirs(os.path.join(root, "Heat (1995)"))
    open(os.path.join(root, "Christmas Movies", "Elf (2003)", "Elf.mkv"), "w").close()
    open(os.path.join(root, "Heat (1995)", "Heat.mkv"), "w").close()
    folders = {r["folder"]: r for r in scanner.scan("movie")}
    assert set(folders) == {"Christmas Movies/Elf (2003)", "Heat (1995)"}
    assert folders["Christmas Movies/Elf (2003)"]["title"] == "Elf"


def test_christmas_tab_list(env, monkeypatch):
    async def tmdb(path, **p):
        if path == "/search/keyword":
            return {"total_pages": 1, "results": [{"id": 207317, "name": "christmas"}, {"id": 1, "name": "holiday"},
                                                  {"id": 2, "name": "holiday inn"}]}
        if path == "/discover/movie" and "with_keywords" in p:
            assert p["with_keywords"] == "207317|1"
            return {"total_pages": 1, "results": [{"id": 10, "title": "Elf Again", "release_date": "2026-11-27"}]}
        if path == "/discover/movie":
            assert p["with_release_type"] == "6"
            return {"total_pages": 1, "results": [{"id": 11, "title": "Mistletoe and Mimosas", "release_date": "2026-11-15"},
                                                  {"id": 12, "title": "A Lifetime Thriller", "release_date": "2026-11-14"}]}
        if path.endswith("/keywords"):
            return {"keywords": []}
        if path == "/movie/11/watch/providers":
            return {"results": {"US": {"flatrate": [{"provider_name": "Hallmark+ Amazon Channel"}]}}}
        if path.endswith("/watch/providers"):
            return {"results": {}}
        raise AssertionError(path)
    monkeypatch.setattr(metadata, "_tmdb", tmdb)
    metadata._info_cache.clear()
    r = asyncio.run(metadata.discover_christmas_all())
    assert [x["title"] for x in r["results"]] == ["Elf Again", "Mistletoe and Mimosas"]
    assert r["results"][1]["services"] == ["Hallmark+ Amazon Channel"]
