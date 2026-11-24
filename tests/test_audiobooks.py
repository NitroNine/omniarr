"""Audiobooks: Audible edition → scored releases → NZBGet → <author>/<series>/Book NN - <title>."""
import asyncio
import os

from app import audiobooks, auth, db, importer, requests_
from app.services import audible, newznab, nzbget

MB = 1024 ** 2

BOOK = {"asin": "B08G9PRS1K", "title": "Project Hail Mary", "subtitle": "", "authors": ["Andy Weir"],
        "narrators": ["Ray Porter"], "minutes": 970, "abridged": False, "release_date": "2021-05-04",
        "series": [], "cover": "", "cover_small": "", "language": "english", "publisher": "Audible Studios",
        "summary": "Ryland Grace is the sole survivor.", "rating": 4.8}
SERIES_BOOK = {**BOOK, "asin": "B07H9Z4QJG", "title": "Skyward", "authors": ["Brandon Sanderson"],
               "narrators": ["Suzy Jackson"], "minutes": 870, "series": [{"title": "Skyward Series", "seq": "1"}]}


def _rel(title, size, guid=None):
    return newznab.Release(title=title, guid=guid or title, link="http://idx/get/" + title, size=size,
                           indexer="FakeIdx", indexer_id=1)


def test_score_follows_spokarr_rules(env):
    b = {**BOOK}
    good = audiobooks.score(b, _rel("Andy Weir - Project Hail Mary (Unabridged) - Ray Porter [M4B]", 900 * MB))
    assert good["ok"] and {"narrator", "author", "unabridged"} <= set(good["plus"])
    assert not audiobooks.score(b, _rel("Andy Weir - The Martian", 600 * MB))["ok"]  # title_mismatch
    german = audiobooks.score(b, _rel("Andy Weir - Der Astronaut Project Hail Mary German Hoerbuch", 900 * MB))
    assert not german["ok"] and "wrong_language" in german["minus"]
    danish = audiobooks.score(b, _rel("Andy_Weir_-_Project_Hail_Mary-AUDiOBOOK-WEB-DK-2023-CRAViNGS_iNT", 512 * MB))
    assert not danish["ok"] and "wrong_language" in danish["minus"]
    assert audiobooks.score(b, _rel("Andy.Weir-Project.Hail.Mary-AUDiOBOOK-LoganFee", 506 * MB))["ok"]
    small = audiobooks.score(b, _rel("Andy Weir - Project Hail Mary", 50 * MB))
    assert "too_small" in small["minus"]
    abridged = audiobooks.score(b, _rel("Andy Weir - Project Hail Mary Abridged", 400 * MB))
    assert "abridged" in abridged["minus"]
    drama = audiobooks.score(b, _rel("Project Hail Mary Full Cast Dramatization Andy Weir", 400 * MB))
    assert "drama" in drama["minus"]


def test_folder_names_like_the_library(env):
    assert audiobooks.relative_folder(BOOK) == os.path.join("Andy Weir", "Project Hail Mary")
    assert audiobooks.relative_folder(SERIES_BOOK) == os.path.join("Brandon Sanderson", "Skyward Series", "Book 01 - Skyward")
    assert audiobooks.relative_folder({**SERIES_BOOK, "series": [{"title": "Skyward", "seq": "2.5"}], "title": "Sunreach: A Novella"}) \
        == os.path.join("Brandon Sanderson", "Skyward", "Book 02.5 - Sunreach - A Novella")


def test_books_already_in_the_folder_are_found(env):
    root = env["data"] / "Audiobooks"
    for d in ("Andy Weir/Project Hail Mary B08GBHW6J1", "Brandon Sanderson/Skyward Series/Book 01 - Skyward",
              "Craig Alanson/Expeditionary Force - Book 03 - Paradise (Unabridged)"):
        (root / d).mkdir(parents=True)
    db.save_settings({"root_audiobooks": str(root)})
    audiobooks.library_index(force=True)
    assert audiobooks.found_in_folder(BOOK) == os.path.join("Andy Weir", "Project Hail Mary B08GBHW6J1")
    assert audiobooks.found_in_folder(SERIES_BOOK)
    assert audiobooks.found_in_folder({**BOOK, "asin": "X", "title": "Paradise"})
    assert not audiobooks.found_in_folder({**BOOK, "asin": "X", "title": "Columbus Day"})


def _fakes(monkeypatch, releases, done_dir=None, sent=None):
    async def get_book(asin):
        return {"B08G9PRS1K": BOOK, "B07H9Z4QJG": SERIES_BOOK}[asin]

    async def search_ab(queries, enough=None):
        sent.setdefault("queries", []).extend(queries) if sent is not None else None
        return releases

    async def fetch(_link):
        return b"<?xml version='1.0'?><nzb></nzb>"

    async def call(method, *params):
        if method == "append":
            if sent is not None:
                sent.update(name=params[0], cat=params[2], dupe=params[6])
            return 77
        if method == "listgroups":
            return []
        if method == "history":
            return [{"NZBID": 77, "Status": "SUCCESS/ALL", "DestDir": str(done_dir), "FinalDir": ""}] if done_dir else []
        if method == "status":
            return {"FreeDiskSpaceMB": 10 ** 7}
        return None

    async def no_cover(_book, _dest):
        return None
    monkeypatch.setattr(audible, "get_book", get_book)
    monkeypatch.setattr(newznab, "search_audiobook", search_ab)
    monkeypatch.setattr(newznab, "fetch_nzb", fetch)
    monkeypatch.setattr(nzbget, "call", call)
    monkeypatch.setattr(audiobooks, "_save_cover", no_cover)


def test_audiobook_end_to_end(env, monkeypatch):
    root = env["data"] / "Audiobooks"
    root.mkdir(parents=True)
    db.save_settings({"root_audiobooks": str(root)})
    done = env["tmp"] / "usenet" / "completed" / "audiobooks" / "Skyward.Brandon.Sanderson.Suzy.Jackson.MP3"
    (done / "CD1").mkdir(parents=True)
    (done / "CD2").mkdir(parents=True)
    (done / "CD1" / "01.mp3").write_bytes(b"a" * 1000)
    (done / "CD2" / "01.mp3").write_bytes(b"b" * 1000)
    (done / "folder.jpg").write_bytes(b"\xff\xd8\xff")
    (done / "x.par2").write_bytes(b"p")
    sent = {}
    _fakes(monkeypatch, [
        _rel("Brandon Sanderson - Skyward - Suzy Jackson (Unabridged) MP3", 700 * MB),
        _rel("Brandon Sanderson - Starsight", 700 * MB),
        _rel("Skyward Brandon Sanderson German Hoerbuch", 700 * MB),
    ], done_dir=done, sent=sent)

    b = asyncio.run(audiobooks.add("B07H9Z4QJG", search=False))
    assert b["folder"] is None
    results = asyncio.run(audiobooks.search_book(b["id"]))
    assert results[0]["ok"] and results[0]["title"].startswith("Brandon Sanderson - Skyward")
    assert sent["cat"] == "audiobooks" and sent["dupe"] == f"omniarr-audiobook-{b['id']}"
    dl = db.one("SELECT * FROM downloads WHERE kind='audiobook'")
    assert dl["audiobook_id"] == b["id"] and dl["status"] == "queued"

    asyncio.run(importer.process_downloads())
    dest = root / "Brandon Sanderson" / "Skyward Series" / "Book 01 - Skyward"
    assert (dest / "CD1" / "01.mp3").exists() and (dest / "CD2" / "01.mp3").exists()
    assert (dest / "folder.jpg").exists() and not (dest / "x.par2").exists()
    row = audiobooks.get(b["id"])
    assert row["folder"] == os.path.join("Brandon Sanderson", "Skyward Series", "Book 01 - Skyward")
    assert db.one("SELECT status FROM downloads WHERE id=?", (dl["id"],))["status"] == "imported"
    assert not done.exists()  # moved out, leftovers cleaned up


def test_failed_download_is_blocklisted_and_searched_again(env, monkeypatch):
    first = _rel("Andy Weir - Project Hail Mary - Ray Porter", 900 * MB, guid="g1")
    second = _rel("Project Hail Mary Andy Weir Unabridged", 800 * MB, guid="g2")
    _fakes(monkeypatch, [first, second])
    b = asyncio.run(audiobooks.add("B08G9PRS1K", search=False))
    asyncio.run(audiobooks.search_book(b["id"]))
    dl = db.one("SELECT * FROM downloads WHERE kind='audiobook'")
    assert dl["guid"] == "g1"
    asyncio.run(importer.fail_download(dl, "FAILURE/PAR"))
    assert db.one("SELECT 1 FROM blocklist WHERE guid='g1'")
    newest = db.one("SELECT * FROM downloads WHERE kind='audiobook' ORDER BY id DESC")
    assert newest["guid"] == "g2" and newest["status"] == "queued"


def test_book_already_on_disk_is_not_downloaded(env, monkeypatch):
    root = env["data"] / "Audiobooks"
    (root / "Andy Weir" / "Project Hail Mary").mkdir(parents=True)
    db.save_settings({"root_audiobooks": str(root)})
    audiobooks.library_index(force=True)
    _fakes(monkeypatch, [])
    b = asyncio.run(audiobooks.add("B08G9PRS1K"))
    assert b["folder"] == os.path.join("Andy Weir", "Project Hail Mary")
    assert not db.q("SELECT 1 FROM downloads")


def test_user_request_then_admin_approves(env, monkeypatch):
    _fakes(monkeypatch, [])
    user = auth.create_user("kid", "password123")
    req = asyncio.run(requests_.create(user, {"kind": "audiobook", "asin": "B08G9PRS1K"}))
    assert req["status"] == "pending" and req["source"] == "B08G9PRS1K" and req["title"] == "Project Hail Mary"
    again = asyncio.run(requests_.create(user, {"kind": "audiobook", "asin": "b08g9prs1k"}))
    assert again.get("duplicate")
    admin = auth.create_user("boss", "password123", role="admin")
    done = asyncio.run(requests_.approve(req["id"], admin))
    assert done["status"] == "approved" and done["item_id"]
    assert audiobooks.get(done["item_id"])["asin"] == "B08G9PRS1K"


def test_api(client, env, monkeypatch):
    _fakes(monkeypatch, [_rel("Andy Weir - Project Hail Mary - Ray Porter", 900 * MB)])

    async def search(term):
        return [dict(BOOK)]
    monkeypatch.setattr(audible, "search", search)
    r = client.get("/api/audiobooks/lookup", params={"term": "hail mary"}).json()
    assert r[0]["asin"] == "B08G9PRS1K" and r[0]["library"] is None and not r[0]["in_folder"]
    b = client.post("/api/audiobooks", json={"asin": "B08G9PRS1K", "search": False}).json()
    assert client.post("/api/audiobooks", json={"asin": "B08G9PRS1K"}).status_code == 400  # already there
    rels = client.post(f"/api/audiobooks/{b['id']}/search", params={"auto": "false"}).json()
    assert rels[0]["ok"] and not db.q("SELECT 1 FROM downloads")
    assert client.post(f"/api/audiobooks/{b['id']}/grab", json=rels[0]).status_code == 200
    q = client.get("/api/queue").json()
    assert q[0]["audiobook_title"] == "Project Hail Mary"
    lib = client.get("/api/audiobooks").json()
    assert lib["books"][0]["download"]["status"] == "queued"
    assert client.delete(f"/api/audiobooks/{b['id']}").json()["ok"]
    # users may browse and request, not add directly
    auth.create_user("kid", "password123")
    from fastapi.testclient import TestClient

    from app.main import app
    u = TestClient(app)
    u.post("/api/auth/login", json={"username": "kid", "password": "password123"})
    assert u.get("/api/audiobooks/lookup", params={"term": "x"}).status_code == 200
    assert u.post("/api/audiobooks", json={"asin": "B08G9PRS1K"}).status_code == 403
