"""1080p and 4K versions of the same title, side by side in one shared folder."""
import asyncio
import json
import os
import sqlite3

from app import db, importer, library, scanner
from app.services import metadata

GB = 1024 ** 3


def _shared_roots(env):
    return db.setting("root_movies"), db.setting("root_tv")


def _fake_movie(monkeypatch):
    async def details(tmdb_id):
        return {"tmdb_id": tmdb_id, "imdb_id": "tt15239678", "title": "Dune: Part Two", "original_title": "Dune: Part Two",
                "year": 2024, "overview": "", "poster": None, "runtime": 166, "release_date": "2024-02-27"}
    monkeypatch.setattr(metadata, "movie_details", details)


def _fake_show(monkeypatch):
    async def details(tvdb_id=None, tmdb_id=None):
        return {"tvdb_id": 403245, "tmdb_id": 136315, "title": "The Bear", "year": 2022, "overview": "", "poster": None,
                "imdb_id": None, "runtime": 30, "status": "Continuing", "series_type": "standard"}

    async def episodes(_s):
        return [{"tvdb_id": e, "season": 1, "episode": e, "absolute": e, "title": f"Ep {e}",
                 "air_date": f"2022-06-0{e}", "overview": ""} for e in (1, 2)]
    monkeypatch.setattr(metadata, "series_details", details)
    monkeypatch.setattr(metadata, "series_episodes", episodes)


def _touch(path, size=1000):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"x" * size)


def _import(dl_dir, files, **dl):
    for name, size in files.items():
        _touch(os.path.join(dl_dir, name), size)
    row = {"kind": "movie", "episode_ids": "[]", "custom_formats": "[]", "score": 0, **dl}
    if row["kind"] == "movie":
        asyncio.run(importer.import_movie(row, dl_dir))
    else:
        asyncio.run(importer.import_episodes(row, dl_dir))


def test_movie_in_both_versions_shares_one_folder(env, monkeypatch, client):
    movies_root, _ = _shared_roots(env)
    _fake_movie(monkeypatch)
    r = client.post("/api/movies", json={"tmdb_id": 693134, "tier": "both", "search": False}).json()
    assert [a["tier"] for a in r["added"]] == ["hd", "uhd"]
    hd = db.one("SELECT * FROM movies WHERE tier='hd'")
    uhd = db.one("SELECT * FROM movies WHERE tier='uhd'")
    assert hd["folder"] == uhd["folder"] and library.movie_dir(hd) == library.movie_dir(uhd)
    assert library.shares_folder("movie", hd)["id"] == uhd["id"]

    # each version downloads and imports on its own, into the same folder
    _import(str(env["tmp"] / "dl1"), {"dune.mkv": 1000}, movie_id=hd["id"],
            release_title="Dune.Part.Two.2024.1080p.BluRay.x264-SPARKS", quality="Bluray-1080p")
    _import(str(env["tmp"] / "dl2"), {"dune.mkv": 2000}, movie_id=uhd["id"],
            release_title="Dune.Part.Two.2024.2160p.UHD.BluRay.x265-W4NK3R", quality="Bluray-2160p")
    hd, uhd = (db.one("SELECT * FROM movies WHERE id=?", (x["id"],)) for x in (hd, uhd))
    assert os.path.dirname(hd["file_path"]) == os.path.dirname(uhd["file_path"]) == library.movie_dir(hd)
    assert hd["file_path"] != uhd["file_path"] and os.path.exists(hd["file_path"]) and os.path.exists(uhd["file_path"])
    assert "Bluray-1080p" in hd["file_path"] and "Bluray-2160p" in uhd["file_path"]

    # rescanning each version only picks up its own file, even though the 4K file is bigger
    asyncio.run(scanner.rescan_movie(hd["id"]))
    asyncio.run(scanner.rescan_movie(uhd["id"]))
    assert db.one("SELECT file_quality FROM movies WHERE id=?", (hd["id"],))["file_quality"] == "Bluray-1080p"
    assert db.one("SELECT file_quality FROM movies WHERE id=?", (uhd["id"],))["file_quality"] == "Bluray-2160p"

    # the detail page lists both versions
    v = client.get(f"/api/movies/{hd['id']}").json()["versions"]
    assert [(x["tier"], x["have"]) for x in v] == [("hd", 1), ("uhd", 1)]
    # the lookup says both versions are in the library
    monkeypatch.setattr(metadata, "search_movies", lambda term: _async([{"tmdb_id": 693134, "title": "Dune"}]))
    hit = client.get("/api/lookup/movie", params={"term": "dune"}).json()[0]
    assert hit["library_tiers"] == ["hd", "uhd"] and hit["in_library"]

    # can't add a version twice, or move a version onto the other one
    assert client.post("/api/movies", json={"tmdb_id": 693134, "tier": "uhd", "search": False}).status_code == 400
    assert client.put(f"/api/movies/{hd['id']}", json={"tier": "uhd"}).status_code == 400

    # removing the 4K version with its files leaves the 1080p file alone
    client.delete(f"/api/movies/{uhd['id']}?delete_files=true")
    assert not os.path.exists(uhd["file_path"]) and os.path.exists(hd["file_path"])
    assert [m["tier"] for m in db.q("SELECT tier FROM movies")] == ["hd"]

    # with no other version, moving to 4K in a shared folder just switches the profile
    library.change_tier("movie", hd["id"], "uhd")
    m = db.one("SELECT * FROM movies WHERE id=?", (hd["id"],))
    assert m["tier"] == "uhd" and m["file_path"] == hd["file_path"] and os.path.exists(m["file_path"])


async def _coro(v):
    return v


def _async(v):
    return _coro(v)


def test_same_file_name_never_overwrites_the_other_version(env, monkeypatch):
    _shared_roots(env)
    _fake_movie(monkeypatch)
    db.save_settings({"naming_movie_file": "original"})  # a naming scheme without the quality in it
    hd, uhd = asyncio.run(library.add_movie_tiers(693134, "both"))
    _import(str(env["tmp"] / "a"), {"a.mkv": 10}, movie_id=hd["id"], release_title="Dune.Part.Two.2024.1080p.WEB-DL-X",
            quality="WEBDL-1080p")
    _import(str(env["tmp"] / "b"), {"b.mkv": 20}, movie_id=uhd["id"], release_title="Dune.Part.Two.2024.2160p.WEB-DL-X",
            quality="WEBDL-2160p")
    a = db.one("SELECT file_path FROM movies WHERE id=?", (hd["id"],))["file_path"]
    b = db.one("SELECT file_path FROM movies WHERE id=?", (uhd["id"],))["file_path"]
    assert a != b and os.path.exists(a) and os.path.exists(b)


def test_show_in_both_versions(env, monkeypatch, client):
    _, tv_root = _shared_roots(env)
    _fake_show(monkeypatch)
    hd = client.post("/api/series", json={"tmdb_id": 136315, "tier": "hd", "monitor": "all", "search": False}).json()
    db.ex("UPDATE episodes SET monitored=0 WHERE series_id=? AND episode=2", (hd["id"],))
    # add the 4K version later, copying which episodes are monitored
    uhd = client.post("/api/series", json={"tmdb_id": 136315, "tier": "uhd", "monitor": "same", "search": False}).json()
    assert uhd["folder"] == hd["folder"]
    mon = {(e["series_id"], e["episode"]): e["monitored"] for e in db.q("SELECT * FROM episodes")}
    assert mon[(uhd["id"], 1)] == 1 and mon[(uhd["id"], 2)] == 0

    s = db.one("SELECT * FROM series WHERE id=?", (hd["id"],))
    ep1 = {e["series_id"]: e["id"] for e in db.q("SELECT * FROM episodes WHERE episode=1")}
    _import(str(env["tmp"] / "e1"), {"ep.mkv": 10}, kind="tv", series_id=hd["id"], episode_ids=json.dumps([ep1[hd["id"]]]),
            release_title="The.Bear.S01E01.1080p.WEB-DL.H.264-NTb", quality="WEBDL-1080p")
    _import(str(env["tmp"] / "e2"), {"ep.mkv": 20}, kind="tv", series_id=uhd["id"],
            episode_ids=json.dumps([ep1[uhd["id"]]]),
            release_title="The.Bear.S01E01.2160p.WEB-DL.H.265-NTb", quality="WEBDL-2160p")
    season = os.path.join(library.series_dir(s), "Season 01")
    assert len(os.listdir(season)) == 2

    # rescans keep each version to its own files
    asyncio.run(scanner.rescan_series(hd["id"]))
    asyncio.run(scanner.rescan_series(uhd["id"]))
    q = {e["series_id"]: e["file_quality"] for e in db.q("SELECT * FROM episodes WHERE episode=1")}
    assert q == {hd["id"]: "WEBDL-1080p", uhd["id"]: "WEBDL-2160p"}
    assert [v["tier"] for v in client.get(f"/api/series/{uhd['id']}").json()["versions"]] == ["hd", "uhd"]


def test_library_import_from_shared_folder_finds_both_versions(env, monkeypatch):
    movies_root, _ = _shared_roots(env)
    _fake_movie(monkeypatch)
    folder = os.path.join(movies_root, "Dune Part Two (2024) {tmdb-693134}")
    _touch(os.path.join(folder, "Dune.Part.Two.2024.1080p.BluRay.x264-SPARKS.mkv"), 1000)
    _touch(os.path.join(folder, "Dune.Part.Two.2024.2160p.UHD.BluRay.x265-W4NK3R.mkv"), 2000)

    [cand] = scanner.scan("movie")
    assert cand["tier"] == "auto"
    asyncio.run(scanner.import_one({"kind": "movie", "tier": "auto", "folder": cand["folder"], "id": 693134}))
    rows = {m["tier"]: m for m in db.q("SELECT * FROM movies")}
    assert set(rows) == {"hd", "uhd"}
    assert rows["hd"]["file_quality"] == "Bluray-1080p" and rows["uhd"]["file_quality"] == "Bluray-2160p"
    assert scanner.scan("movie") == []


def test_old_database_is_upgraded_to_versions(tmp_path):
    path = tmp_path / "old.db"
    c = sqlite3.connect(path)
    c.executescript("""
      CREATE TABLE movies (id INTEGER PRIMARY KEY, tmdb_id INTEGER UNIQUE NOT NULL, imdb_id TEXT, title TEXT NOT NULL,
        original_title TEXT, year INTEGER, overview TEXT, poster TEXT, runtime INTEGER, release_date TEXT,
        tier TEXT NOT NULL DEFAULT 'hd', monitored INTEGER DEFAULT 1, folder TEXT, file_path TEXT, file_quality TEXT,
        file_score INTEGER, file_size INTEGER, added TEXT DEFAULT CURRENT_TIMESTAMP, last_search TEXT,
        collection_id INTEGER, collection_name TEXT);
      INSERT INTO movies(id, tmdb_id, title, tier, folder, file_path, collection_id)
        VALUES (7, 603, 'The Matrix', 'hd', 'The Matrix (1999)', '/m.mkv', 2344);
    """)
    c.commit()
    c.close()
    db.reset_for_tests(str(path))
    m = db.one("SELECT * FROM movies WHERE id=7")
    assert (m["tmdb_id"], m["file_path"], m["collection_id"]) == (603, "/m.mkv", 2344)
    db.insert("movies", {"tmdb_id": 603, "title": "The Matrix", "tier": "uhd", "folder": "The Matrix (1999)"})
    assert db.one("SELECT COUNT(*) c FROM movies")["c"] == 2
    try:
        db.insert("movies", {"tmdb_id": 603, "title": "The Matrix", "tier": "uhd"})
        raise AssertionError("same version twice")
    except sqlite3.IntegrityError:
        pass


def test_old_split_root_folders_become_one(tmp_path):
    db.reset_for_tests(str(tmp_path / "o.db"))
    db.connect()
    db.ex("INSERT INTO settings(key, value) VALUES ('root_movies_hd', '\"/data/Movies\"'), "
          "('root_movies_uhd', '\"/data/Movies-4K\"'), ('root_tv_uhd', '\"/data/TV-4K\"')")
    db.reset_for_tests(str(tmp_path / "o.db"))
    assert db.setting("root_movies") == "/data/Movies"  # the 1080p folder is kept
    assert db.setting("root_tv") == "/data/media/tv"    # nothing set: default
    assert not db.q("SELECT key FROM settings WHERE key LIKE 'root_%\\_%' ESCAPE '\\'")
