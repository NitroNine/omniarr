import asyncio
import json
import os
import stat

from app import db, music
from app.services import deemix, deezer

ART = {"deezer_id": 10, "name": "The Zutons", "picture": None, "albums": 4, "fans": 1}
ALBUMS = [
    {"deezer_id": 101, "title": "Big Black Hole", "record_type": "album", "release_date": "2030-05-01", "explicit": False, "tracks": 2},
    {"deezer_id": 102, "title": "Tired of Hanging Around (Deluxe)", "record_type": "album", "release_date": "2006-04-17", "explicit": True, "tracks": 3},
    {"deezer_id": 103, "title": "Tired of Hanging Around", "record_type": "album", "release_date": "2006-04-17", "explicit": False, "tracks": 3},
    {"deezer_id": 104, "title": "Who Killed... The Zutons", "record_type": "album", "release_date": "2004-04-19", "explicit": False, "tracks": 2},
    {"deezer_id": 105, "title": "Valerie", "record_type": "single", "release_date": "2006-06-01", "explicit": False, "tracks": 1},
]


def _fake(monkeypatch, albums):
    async def artist(_id):
        return dict(ART)

    async def artist_albums(_id):
        return [{**a, "artist_id": 10, "artist": "The Zutons", "cover": None} for a in albums]

    async def album(aid):
        a = next(x for x in albums if x["deezer_id"] == aid)
        return {**a, "artist_id": 10, "artist": "The Zutons", "cover": None}
    monkeypatch.setattr(deezer, "artist", artist)
    monkeypatch.setattr(deezer, "artist_albums", artist_albums)
    monkeypatch.setattr(deezer, "album", album)


def _roots(env):
    music_dir = env["data"] / "music"
    flac, comp = music_dir / "FLAC", music_dir / "Compressed"
    old = comp / "Z" / "The Zutons" / "Who Killed․․․ The Zutons"  # in the lossy folder already
    old.mkdir(parents=True)
    (old / "01- Zuton Fever.opus").write_bytes(b"x")
    flac.mkdir(parents=True)
    db.save_settings({"root_music": str(flac)})
    music.forget_index()
    return flac, comp


def test_letters_and_titles():
    assert music.letter("The Zutons") == "Z" and music.letter("POLYSICS") == "P" and music.letter("+44") == "#"
    assert music.letter("éclair") == "#" or music.letter("éclair") == "E"
    assert music.base("Tired of Hanging Around (Deluxe)") == music.base("Tired of Hanging Around")
    assert music.base("Open Up And Say...Ahh! - 20th Anniversary Edition") == music.base("Open Up And Say... Ahh!")


def test_existing_albums_found_in_sibling_folder(env):
    flac, comp = _roots(env)
    assert music.library_folders() == [str(flac), str(comp)]
    assert music.find_album("The Zutons", "Who Killed... The Zutons")
    assert music.find_album("Zutons", "Who Killed... The Zutons")  # "The" ignored
    assert not music.find_album("The Zutons", "Big Black Hole")


def test_add_artist_all_wants_missing_albums_once(env, monkeypatch):
    _roots(env)
    _fake(monkeypatch, ALBUMS)
    st = asyncio.run(music.add_artist(10, "all"))
    by = {a["deezer_id"]: a for a in st["albums"]}
    assert by[104]["state"] == "have"  # already in Compressed
    assert by[102]["state"] == "wanted" and by[103]["state"] == ""  # explicit edition only
    assert by[101]["state"] == "wanted" and by[101]["upcoming"]  # not out yet: waits
    assert by[105]["state"] == ""  # singles aren't in music_types
    assert music.next_album()["deezer_id"] == 102  # 101 isn't released


def test_follow_new_releases_only(env, monkeypatch):
    _roots(env)
    _fake(monkeypatch, ALBUMS[1:])
    st = asyncio.run(music.add_artist(10, "new"))
    assert not [a for a in st["albums"] if a["state"] == "wanted"]
    new = [{"deezer_id": 106, "title": "The Big Decider", "record_type": "album", "release_date": "2008-01-01", "explicit": False, "tracks": 2},
           {"deezer_id": 107, "title": "The Big Decider (Deluxe)", "record_type": "album", "release_date": "2008-01-01", "explicit": True, "tracks": 3},
           {"deezer_id": 108, "title": "Tired of Hanging Around (Remastered)", "record_type": "album", "release_date": "2020-01-01", "explicit": False, "tracks": 3}]
    _fake(monkeypatch, ALBUMS[1:] + new)
    art = db.one("SELECT id FROM music_artists")["id"]
    assert asyncio.run(music.refresh_artist(art)) == ["The Big Decider (Deluxe)"]  # one edition; a reissue isn't new
    db.update("music_artists", art, {"monitored": 0})
    _fake(monkeypatch, ALBUMS[1:] + new + [{**new[0], "deezer_id": 109, "title": "Another One"}])
    assert asyncio.run(music.refresh_artist(art)) == []  # not followed


def test_download_lands_in_letter_folder(env, monkeypatch):
    flac, _ = _roots(env)
    _fake(monkeypatch, ALBUMS)
    db.save_settings({"deezer_arl": "secret-arl"})
    monkeypatch.setattr(deemix, "available", lambda: None)
    calls = []

    async def fake_download(url, dest, quality, on_progress, key="", artist_folder=True):
        calls.append((url, dest, quality))
        d = os.path.join(dest, *(["The Zutons"] if artist_folder else []), "Tired of Hanging Around (Deluxe)")
        os.makedirs(d, exist_ok=True)
        for i in range(1, 4 if len(calls) > 1 else 3):  # first try: 2 of 3 tracks
            open(os.path.join(d, f"0{i}- Track.flac"), "wb").close()
        open(os.path.join(d, "errors.txt"), "w").close()
        on_progress(100)
        return [] if len(calls) > 1 else ["The Zutons - Track 3 :: ECONNRESET: socket hang up"]
    monkeypatch.setattr(deemix, "download", fake_download)
    asyncio.run(music.add_album(102))

    async def run():
        await music.process()
        await music._task
    asyncio.run(run())
    assert calls[0] == ("https://www.deezer.com/album/102", str(flac / "Z"), "flac")
    dl = db.one("SELECT * FROM downloads")
    assert dl["status"] == "failed" and dl["message"].startswith("2 of 3 tracks")
    assert music.next_album() is None  # waits RETRY_HOURS
    db.ex("UPDATE music_albums SET last_try=datetime('now','-7 hours')")
    asyncio.run(run())
    assert calls[1][1] == str(flac / "Z" / "The Zutons")  # the retry goes into the folder the first try made
    a = db.one("SELECT * FROM music_albums WHERE deezer_id=102")
    assert a["folder"] == str(flac / "Z" / "The Zutons" / "Tired of Hanging Around (Deluxe)")
    assert db.one("SELECT status FROM downloads ORDER BY id DESC")["status"] == "imported"
    assert not os.path.exists(os.path.join(a["folder"], "errors.txt"))


def test_restart_leftovers_retry(env):
    aid = db.insert("music_artists", {"deezer_id": 1, "name": "X"})
    alb = db.insert("music_albums", {"deezer_id": 2, "artist_id": aid, "title": "Y", "wanted": 1, "last_try": "2099-01-01"})
    db.insert("downloads", {"kind": "music", "music_album_id": alb, "release_title": "X - Y", "status": "downloading"})
    asyncio.run(music.process())  # no ARL: nothing starts, but the stale row is closed
    assert db.one("SELECT status FROM downloads")["status"] == "failed"
    assert db.one("SELECT last_try FROM music_albums")["last_try"] is None


def test_parse_lines():
    assert deemix.parse_line("[Super Sonic Rock Saga] Downloading: 52%") == (52.0, None)
    assert deemix.parse_line("[Album] A - B :: Track downloaded.") == (None, None)
    assert deemix.parse_line("[Album] A - B :: Track not yet encoded and no alternative found")[1] == \
        "A - B :: Track not yet encoded and no alternative found"
    assert "Deezer login failed" in deemix.parse_line("No valid ARL found. Log in through…")[1]


def test_config_written_with_private_login(env):
    db.save_settings({"deezer_arl": "secret-arl"})
    deemix._write_config("320")
    d = deemix.config_dir()
    cfg = json.load(open(os.path.join(d, "config.json")))
    assert cfg["maxBitrate"] == 3 and cfg["albumTracknameTemplate"] == "%tracknumber%- %title%" and cfg["createArtistFolder"]
    login = os.path.join(d, "login.json")
    assert json.load(open(login)) == {"arl": "secret-arl"}
    assert stat.S_IMODE(os.stat(login).st_mode) == 0o600


def test_api(client, monkeypatch):
    _fake(monkeypatch, ALBUMS)
    client.put("/api/settings", json={"deezer_arl": "secret-arl"})
    assert client.get("/api/settings").json()["deezer_arl"] == "********"
    client.put("/api/settings", json={"deezer_arl": "********"})  # the mask doesn't overwrite it
    assert db.setting("deezer_arl") == "secret-arl"
    r = client.post("/api/music/artists", json={"deezer_id": 10, "monitor": "none"})
    assert r.status_code == 200 and r.json()["artist"]["monitored"] == 0
    assert client.post("/api/music/artists", json={"deezer_id": 10, "monitor": "sometimes"}).status_code == 400
    lib = client.get("/api/music").json()
    assert lib["artists"][0]["name"] == "The Zutons"
    alb = client.post("/api/music/albums", json={"deezer_id": 104}).json()
    assert alb["state"] == "wanted"
    did = db.insert("downloads", {"kind": "music", "music_album_id": alb["id"], "release_title": "The Zutons - Who Killed",
                                  "status": "downloading"})
    q = client.get("/api/queue").json()
    assert q[0]["music_title"] == "Who Killed... The Zutons"
    assert client.delete(f"/api/queue/{did}").status_code == 200
    assert db.one("SELECT status FROM downloads WHERE id=?", (did,))["status"] == "removed"
    assert db.one("SELECT wanted FROM music_albums WHERE id=?", (alb["id"],))["wanted"] == 0


def test_want_missing_skips_titles_owned_in_any_edition(env, monkeypatch):
    flac, _ = _roots(env)
    owned = flac / "Z" / "The Zutons" / "Tired of Hanging Around"  # the clean edition is already here
    owned.mkdir(parents=True)
    (owned / "01- Why Won't You Give Me Your Love.flac").write_bytes(b"x")
    music.forget_index()
    _fake(monkeypatch, ALBUMS)
    st = asyncio.run(music.add_artist(10, "new"))
    by = {a["deezer_id"]: a for a in st["albums"]}
    assert by[103]["state"] == "have" and by[102]["state"] == "have"  # owning one edition covers the Deluxe too
    art = db.one("SELECT id FROM music_artists")["id"]
    assert [r["deezer_id"] for r in music.missing_editions(art)] == [101]
    assert music.want_missing(art) == 1 and music.missing_editions(art) == []


def test_download_goes_into_existing_artist_folder(env, monkeypatch):
    flac, _ = _roots(env)
    mine = flac / "Z" / "Zutons, The"  # spelled differently from Deezer's "The Zutons"
    (mine / "Big Black Hole").mkdir(parents=True)
    _fake(monkeypatch, ALBUMS)
    db.save_settings({"deezer_arl": "secret-arl"})
    monkeypatch.setattr(deemix, "available", lambda: None)
    calls = []

    async def fake_download(url, dest, quality, on_progress, key="", artist_folder=True):
        calls.append((dest, artist_folder))
        d = os.path.join(dest, "Tired of Hanging Around (Deluxe)")
        os.makedirs(d, exist_ok=True)
        for i in range(1, 4):
            open(os.path.join(d, f"0{i}- Track.flac"), "wb").close()
        return []
    monkeypatch.setattr(deemix, "download", fake_download)
    asyncio.run(music.add_album(102))

    async def run():
        await music.process()
        await music._task
    asyncio.run(run())
    assert calls == [(str(mine), False)]
    assert db.one("SELECT folder FROM music_albums WHERE deezer_id=102")["folder"] == str(mine / "Tired of Hanging Around (Deluxe)")
    assert sorted(os.listdir(flac / "Z")) == ["Zutons, The"]  # no second artist folder


def test_try_with_no_tracks_leaves_no_empty_album(env, monkeypatch):
    flac, _ = _roots(env)
    _fake(monkeypatch, ALBUMS)
    db.save_settings({"deezer_arl": "secret-arl"})
    monkeypatch.setattr(deemix, "available", lambda: None)

    async def fake_download(url, dest, quality, on_progress, key="", artist_folder=True):
        d = os.path.join(dest, "The Zutons", "Tired of Hanging Around (Deluxe)")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "errors.txt"), "w") as f:
            f.write("1 | x | ENOENT")
        return ["The Zutons - x :: ENOENT"]
    monkeypatch.setattr(deemix, "download", fake_download)
    asyncio.run(music.add_album(102))

    async def run():
        await music.process()
        await music._task
    asyncio.run(run())
    assert db.one("SELECT status FROM downloads")["status"] == "failed"
    assert not os.path.exists(flac / "Z" / "The Zutons" / "Tired of Hanging Around (Deluxe)")


def _run_album(monkeypatch, files, errors):
    db.save_settings({"deezer_arl": "secret-arl"})
    monkeypatch.setattr(deemix, "available", lambda: None)

    async def fake_download(url, dest, quality, on_progress, key="", artist_folder=True):
        d = os.path.join(dest, "The Zutons", "Tired of Hanging Around (Deluxe)")
        os.makedirs(d, exist_ok=True)
        for i in range(1, files + 1):
            open(os.path.join(d, f"0{i}- Track.flac"), "wb").close()
        return errors
    monkeypatch.setattr(deemix, "download", fake_download)
    asyncio.run(music.add_album(102))

    async def run():
        await music.process()
        await music._task
    asyncio.run(run())
    return db.one("SELECT * FROM music_albums WHERE deezer_id=102"), db.one("SELECT * FROM downloads ORDER BY id DESC")


def test_tracks_without_flac_dont_keep_the_album_wanted(env, monkeypatch):
    _roots(env)
    _fake(monkeypatch, ALBUMS)
    a, dl = _run_album(monkeypatch, 2, ["The Zutons - Track 3 :: Track not found at desired bitrate and no alternative found!"])
    assert dl["status"] == "imported" and a["folder"] and a["message"] == "1 track not in FLAC on Deezer"
    assert music.next_album() is None


def test_album_without_flac_is_not_retried(env, monkeypatch):
    flac, _ = _roots(env)
    _fake(monkeypatch, ALBUMS)
    errs = [f"The Zutons - Track {i} :: Track not found at desired bitrate." for i in (1, 2, 3)]
    a, dl = _run_album(monkeypatch, 0, errs)
    assert dl["status"] == "failed" and a["wanted"] == 0 and a["message"] == "Not in FLAC on Deezer"
    assert music.album_state(a["id"])["state"] == "unavailable"
    assert not os.path.exists(flac / "Z" / "The Zutons" / "Tired of Hanging Around (Deluxe)")
    art = db.one("SELECT id FROM music_artists")["id"]
    assert 102 not in [r["deezer_id"] for r in music.missing_editions(art)]  # "download all missing" leaves it
    assert deemix.unavailable("A - B :: Your account can't stream the track at the desired bitrate.")
    assert not deemix.unavailable("A - B :: ENOENT: no such file or directory")


def _flac(path, seconds, size):
    rate, samples = 44100, 44100 * seconds
    info = bytearray(18)
    info[10:13] = (rate << 4 | 1 << 1).to_bytes(3, "big")  # rate, 2 channels
    info[13:18] = ((15 << 36) | samples).to_bytes(5, "big")  # 16 bit, total samples
    data = b"fLaC" + bytes([0x80, 0, 0, 34]) + bytes(info) + bytes(16)
    with open(path, "wb") as f:
        f.write(data + bytes(max(0, size - len(data))))


def test_cut_off_flac_is_fetched_again(env, monkeypatch):
    flac, _ = _roots(env)
    _fake(monkeypatch, ALBUMS)
    asyncio.run(music.add_album(102))  # wanted; then a first try is cut off after two tracks
    album = flac / "Z" / "The Zutons" / "Tired of Hanging Around (Deluxe)"
    album.mkdir(parents=True)
    _flac(album / "01- Track.flac", 150, 20_000_000)  # ~1 Mbit/s: fine
    _flac(album / "02- Track.flac", 150, 2048 * 977)  # ~107 kbit/s, ends on a 2048-byte block: cut off
    assert not music.flac_cut_off(str(album / "01- Track.flac")) and music.flac_cut_off(str(album / "02- Track.flac"))
    _flac(album / "silent.flac", 300, 52_013)  # a hidden "silent track": tiny but not cut off
    assert not music.flac_cut_off(str(album / "silent.flac"))
    os.remove(album / "silent.flac")
    music.forget_index()
    seen = []

    async def fake_download(url, dest, quality, on_progress, key="", artist_folder=True):
        d = os.path.join(dest, "Tired of Hanging Around (Deluxe)")
        seen.append(sorted(os.listdir(d)))
        for i in (2, 3):
            _flac(os.path.join(d, f"0{i}- Track.flac"), 150, 20_000_000)
        return []
    db.save_settings({"deezer_arl": "secret-arl"})
    monkeypatch.setattr(deemix, "available", lambda: None)
    monkeypatch.setattr(deemix, "download", fake_download)

    async def run():
        await music.process()
        await music._task
    asyncio.run(run())
    assert seen == [["01- Track.flac"]]  # the cut-off track was removed before deemix ran
    assert db.one("SELECT status FROM downloads")["status"] == "imported"
