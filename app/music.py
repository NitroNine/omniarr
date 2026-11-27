"""Music: artists and albums from Deezer, downloaded by the built-in deemix (services/deemix.py) into the music folder
as <root>/<Letter>/<Artist>/<Album>/NN- Title.flac (the owner's layout: a letter folder per artist, "The" ignored,
"#" for anything not A-Z).

- Following an artist (`monitored`) grabs every new album / EP it releases (types in setting `music_types`).
  Adding an artist can also want its whole back catalogue ("all") or nothing ("none").
- Albums already in the music folder, or in a sibling format folder (FLAC next to Compressed, MP3...), count as owned.
- One album downloads at a time; a download row (kind 'music') shows it in Activity. An album that didn't fully come
  down is tried again 6 hours later (deemix skips the tracks it already has)."""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import time

from . import db
from .services import deemix, deezer

ROOT_GUESSES = ["/music", "/data/music/FLAC", "/data/media/music/FLAC", "/data/music", "/data/media/music", "/data/Music"]
_FORMAT_DIR = re.compile(r"^(flac|lossless|hi-?res|compressed|lossy|mp3|opus|aac|m4a|ogg|vorbis)$", re.I)
AUDIO = (".flac", ".mp3", ".m4a", ".opus", ".ogg", ".aac", ".wav", ".alac", ".wma")
RETRY_HOURS = 6
REFRESH_HOURS = 12
TYPES = ("album", "ep", "single", "compile")


def root() -> str:
    r = (db.setting("root_music") or "").strip()
    if r:
        return r.rstrip("/")
    return next((g for g in ROOT_GUESSES if os.path.isdir(g)), ROOT_GUESSES[0])


def wanted_types() -> set[str]:
    return {t.strip().lower() for t in (db.setting("music_types") or "album,ep").split(",") if t.strip()}


def letter(artist: str) -> str:
    name = re.sub(r"^the\s+", "", (artist or "").strip(), flags=re.I)
    first = next((c for c in name if c.isalnum()), "#")
    first = first.upper()
    return first if "A" <= first <= "Z" else "#"


def norm(s: str) -> str:
    s = (s or "").lower().replace("&", "and").replace("․", ".")
    s = re.sub(r"^the\s+|,\s*the$", "", s.strip())
    return re.sub(r"[\W_]+", "", s)


def base(s: str) -> str:
    """An album title without its edition: 'Album (Deluxe Edition)' / 'Album - 20th Anniversary Edition' → album."""
    s = re.sub(r"\s*[(\[][^)\]]*[)\]]", "", s or "")
    s = re.sub(r"\s+-\s+.*$", "", s)
    return norm(s) or norm(s or "")


# ------------------------------------------------------------- the folder --
_index: dict = {"at": 0.0, "key": None, "artists": {}}


def library_folders() -> list[str]:
    """The music folder, plus sibling format folders (e.g. root …/music/FLAC → also …/music/Compressed)."""
    r = root()
    out = [r]
    parent = os.path.dirname(r)
    if _FORMAT_DIR.match(os.path.basename(r)) and os.path.isdir(parent):
        out += sorted(os.path.join(parent, d) for d in os.listdir(parent)
                      if d != os.path.basename(r) and _FORMAT_DIR.match(d) and os.path.isdir(os.path.join(parent, d)))
    return out


def _scan() -> dict[str, list[tuple[str, str]]]:
    artists: dict[str, list[tuple[str, str]]] = {}

    def add_artist(path: str, name: str):
        try:
            albums = [(d, os.path.join(path, d)) for d in os.listdir(path) if os.path.isdir(os.path.join(path, d))]
        except OSError:
            return
        artists.setdefault(norm(name), []).extend(albums)
    for top in library_folders():
        try:
            entries = [e for e in os.listdir(top) if os.path.isdir(os.path.join(top, e))]
        except OSError:
            continue
        for e in entries:
            p = os.path.join(top, e)
            if len(e) == 1 or e in ("#", "0-9"):  # a letter folder
                try:
                    for a in os.listdir(p):
                        if os.path.isdir(os.path.join(p, a)):
                            add_artist(os.path.join(p, a), a)
                except OSError:
                    pass
            else:
                add_artist(p, e)
    return artists


def index(fresh: bool = False) -> dict[str, list[tuple[str, str]]]:
    key = tuple(library_folders())
    if fresh or _index["key"] != key or time.time() - _index["at"] > 600:
        _index.update(at=time.time(), key=key, artists=_scan())
    return _index["artists"]


def forget_index() -> None:
    _index["at"] = 0.0


def find_album(artist: str, title: str, fresh: bool = False) -> str | None:
    """The folder of an album already in the music folders, matched by artist and title (edition ignored)."""
    albums = index(fresh).get(norm(artist)) or []
    n, b = norm(title), base(title)
    for name, path in albums:
        if norm(name) == n:
            return path
    for name, path in albums:
        if b and base(name) == b:
            return path
    return None


def artist_dir(artist: str) -> str | None:
    """The artist's existing folder in the music folder (root/Letter/Artist or root/Artist), matched loosely
    ("The", case, punctuation), so a new album goes next to the ones already there even if the spelling differs."""
    r, n = root(), norm(artist)
    if not n:
        return None
    for top in (os.path.join(r, letter(artist)), r):
        try:
            names = os.listdir(top)
        except OSError:
            continue
        for e in sorted(names):
            p = os.path.join(top, e)
            if len(e) > 1 and e != "0-9" and norm(e) == n and os.path.isdir(p):
                return p
    return None


def _tidy(folder: str, done: bool) -> None:
    """After a download: drop deemix's errors.txt once the album is complete; remove the album folder again when a
    try brought down no audio at all (only errors.txt / cover art), so the library has no empty albums."""
    try:
        if done:
            p = os.path.join(folder, "errors.txt")
            if os.path.isfile(p):
                os.remove(p)
        elif not count_audio(folder):
            shutil.rmtree(folder, ignore_errors=True)
    except OSError:
        pass


def count_audio(folder: str) -> int:
    n = 0
    for _dir, _sub, files in os.walk(folder):
        n += sum(1 for f in files if f.lower().endswith(AUDIO))
    return n


MIN_FLAC_BPS = 160_000  # real FLAC never gets this low; a file that does was cut off mid-download


def flac_cut_off(path: str) -> bool:
    """A FLAC whose size is far too small for the length its header promises: deemix was stopped while writing it
    (Omniarr restarted, download cancelled). Its header and tags are there, so it looks fine but plays a few seconds."""
    try:
        with open(path, "rb") as f:
            if f.read(4) != b"fLaC":
                return False
            head = f.read(4)
            if len(head) < 4 or head[0] & 0x7F != 0:  # STREAMINFO is always the first block
                return False
            info = f.read(18)
        rate = int.from_bytes(info[10:13], "big") >> 4
        samples = int.from_bytes(info[13:18], "big") & 0xFFFFFFFFF
        if not rate or not samples:
            return False
        return os.path.getsize(path) * 8 / (samples / rate) < MIN_FLAC_BPS
    except OSError:
        return False


def remove_cut_off(folder: str) -> list[str]:
    """Delete cut-off FLACs in an album folder so the next deemix run fetches them again (it skips existing files)."""
    gone = []
    for d, _sub, files in os.walk(folder):
        for f in files:
            p = os.path.join(d, f)
            if f.lower().endswith(".flac") and flac_cut_off(p):
                try:
                    os.remove(p)
                    gone.append(f)
                except OSError:
                    pass
    return gone


# ------------------------------------------------------------- the database --
def _artist_row(deezer_id: int) -> dict | None:
    return db.one("SELECT * FROM music_artists WHERE deezer_id=?", (deezer_id,))


def _save_album(a: dict, artist_row: dict, wanted: bool = False) -> dict:
    row = db.one("SELECT * FROM music_albums WHERE deezer_id=?", (a["deezer_id"],))
    folder = find_album(artist_row["name"], a["title"])
    if row:
        upd = {"title": a["title"], "release_date": a.get("release_date"), "record_type": a.get("record_type"),
               "cover": a.get("cover") or row["cover"], "explicit": int(bool(a.get("explicit")))}
        if a.get("tracks"):
            upd["tracks"] = a["tracks"]
        if folder and not row["folder"]:
            upd["folder"] = folder
        db.update("music_albums", row["id"], upd)
        return db.one("SELECT * FROM music_albums WHERE id=?", (row["id"],))
    aid = db.insert("music_albums", {
        "deezer_id": a["deezer_id"], "artist_id": artist_row["id"], "title": a["title"],
        "record_type": a.get("record_type") or "album", "release_date": a.get("release_date"),
        "explicit": int(bool(a.get("explicit"))), "cover": a.get("cover"), "tracks": a.get("tracks"),
        "wanted": int(wanted and not folder), "folder": folder})
    return db.one("SELECT * FROM music_albums WHERE id=?", (aid,))


def _pick_editions(albums: list[dict]) -> list[dict]:
    """One edition per title and type: explicit over clean, then the first Deezer lists."""
    best: dict[tuple, dict] = {}
    for a in albums:
        k = (a.get("record_type"), base(a["title"]))
        if k not in best or (a.get("explicit") and not best[k].get("explicit")):
            best[k] = a
    return list(best.values())


async def _ensure_artist(deezer_id: int, monitored: bool | None = None) -> dict:
    row = _artist_row(deezer_id)
    if row:
        if monitored is not None:
            db.update("music_artists", row["id"], {"monitored": int(monitored)})
        return _artist_row(deezer_id)
    a = await deezer.artist(deezer_id)
    db.insert("music_artists", {"deezer_id": deezer_id, "name": a["name"], "picture": a.get("picture"),
                                "monitored": int(bool(monitored))})
    return _artist_row(deezer_id)


def missing_editions(artist_id: int) -> list[dict]:
    """The albums "download all missing" would want: one edition per title of the wanted types, none of whose
    editions is in the library or already wanted."""
    rows = db.q("SELECT * FROM music_albums WHERE artist_id=?", (artist_id,))
    types = wanted_types()
    covered = {(r["record_type"], base(r["title"])) for r in rows if r["folder"] or r["wanted"] or r["message"]}
    todo = [{**r, "explicit": bool(r["explicit"])} for r in rows
            if r["record_type"] in types and (r["record_type"], base(r["title"])) not in covered]
    return _pick_editions(todo)


def want_missing(artist_id: int) -> int:
    rows = missing_editions(artist_id)
    for r in rows:
        db.update("music_albums", r["id"], {"wanted": 1, "last_try": None, "message": None})
    return len(rows)


async def add_artist(deezer_id: int, monitor: str = "new") -> dict:
    """Track an artist. monitor: 'new' = grab future releases, 'all' = also every missing album now, 'none' = neither."""
    if monitor not in ("new", "all", "none"):
        raise ValueError("monitor must be new, all or none")
    art = await _ensure_artist(deezer_id, monitored=monitor != "none")
    albums = await deezer.artist_albums(deezer_id)
    index(fresh=True)
    for a in albums:
        _save_album(a, art)
    db.update("music_artists", art["id"], {"last_refresh": db.one("SELECT datetime('now') t")["t"]})
    n = want_missing(art["id"]) if monitor == "all" else 0
    have = db.one("SELECT COUNT(*) c FROM music_albums WHERE artist_id=? AND folder IS NOT NULL", (art["id"],))["c"]
    db.log(f"Music: added {art['name']}" + (f", {n} album(s) wanted" if n else "") + (f", {have} already in the music folder" if have else ""))
    return artist_state(art["id"])


async def refresh_artist(artist_id: int) -> list[str]:
    """Pick up an artist's new releases; on a followed artist the wanted types are grabbed. Returns new titles."""
    art = db.one("SELECT * FROM music_artists WHERE id=?", (artist_id,))
    if not art:
        return []
    albums = await deezer.artist_albums(art["deezer_id"])
    known = {r["deezer_id"] for r in db.q("SELECT deezer_id FROM music_albums WHERE artist_id=?", (artist_id,))}
    have_titles = {(r["record_type"], base(r["title"])) for r in
                   db.q("SELECT record_type, title FROM music_albums WHERE artist_id=?", (artist_id,))}
    types = wanted_types()
    new_titles = []
    fresh = [a for a in albums if a["deezer_id"] not in known]
    for a in _pick_editions(fresh) if fresh else []:
        dup = (a.get("record_type"), base(a["title"])) in have_titles  # another edition of something known
        row = _save_album(a, art)
        if art["monitored"] and not dup and a.get("record_type") in types and not row["folder"]:
            db.update("music_albums", row["id"], {"wanted": 1})
            new_titles.append(a["title"])
    for a in fresh:  # the other editions, not wanted
        if not db.one("SELECT 1 FROM music_albums WHERE deezer_id=?", (a["deezer_id"],)):
            _save_album(a, art)
    for r in db.q("SELECT * FROM music_albums WHERE artist_id=? AND folder IS NULL", (artist_id,)):
        f = find_album(art["name"], r["title"])
        if f:
            db.update("music_albums", r["id"], {"folder": f})
    db.update("music_artists", artist_id, {"last_refresh": db.one("SELECT datetime('now') t")["t"]})
    if new_titles:
        db.log(f"Music: new from {art['name']}: {', '.join(new_titles)}")
    return new_titles


async def refresh_due() -> None:
    index(fresh=True)
    for a in db.q("SELECT id FROM music_artists WHERE last_refresh IS NULL OR last_refresh < datetime('now', ?)",
                  (f"-{REFRESH_HOURS} hours",)):
        try:
            await refresh_artist(a["id"])
        except Exception as e:  # noqa: BLE001
            db.log(f"Music: couldn't refresh artist {a['id']}: {e}", "warn")


async def add_album(deezer_id: int, download: bool = True) -> dict:
    a = await deezer.album(deezer_id)
    art = await _ensure_artist(a["artist_id"])
    row = _save_album(a, art)
    if download and not row["folder"]:
        db.update("music_albums", row["id"], {"wanted": 1, "last_try": None, "message": None})
    return album_state(row["id"])


def set_album(album_id: int, wanted: bool) -> dict:
    db.update("music_albums", album_id, {"wanted": int(wanted), "last_try": None, "message": None})
    return album_state(album_id)


def remove_artist(artist_id: int) -> None:
    for d in db.q("SELECT d.id FROM downloads d JOIN music_albums a ON a.id=d.music_album_id "
                  "WHERE a.artist_id=? AND d.status IN ('queued','downloading')", (artist_id,)):
        cancel(d["id"])
    db.ex("DELETE FROM music_albums WHERE artist_id=?", (artist_id,))
    db.ex("DELETE FROM music_artists WHERE id=?", (artist_id,))


# ------------------------------------------------------------- state for the UI --
def _active() -> dict[int, dict]:
    return {d["music_album_id"]: d for d in db.q(
        "SELECT id, music_album_id, status, progress, message FROM downloads WHERE music_album_id IS NOT NULL "
        "AND status IN ('queued','downloading')")}


def _state(row: dict | None, active: dict[int, dict], artist: str, title: str) -> dict:
    if row:
        d = active.get(row["id"])
        if d:
            return {"state": "downloading", "progress": d["progress"], "album_id": row["id"]}
        if row["folder"]:
            return {"state": "have", "folder": row["folder"], "album_id": row["id"], "message": row.get("message")}
        if row["wanted"]:
            return {"state": "wanted", "album_id": row["id"], "message": row.get("message"),
                    "upcoming": bool(row.get("release_date") and row["release_date"] > time.strftime("%Y-%m-%d"))}
        if row.get("message"):  # e.g. "Not in FLAC on Deezer": not retried until asked again
            return {"state": "unavailable", "album_id": row["id"], "message": row["message"]}
        return {"state": "", "album_id": row["id"]}
    f = find_album(artist, title)
    return {"state": "have", "folder": f} if f else {"state": ""}


def annotate(albums: list[dict]) -> list[dict]:
    """Deezer albums with what Omniarr has of each (have / wanted / downloading)."""
    ids = [a["deezer_id"] for a in albums]
    rows = {r["deezer_id"]: r for r in db.q(
        f"SELECT * FROM music_albums WHERE deezer_id IN ({','.join('?' * len(ids))})", tuple(ids))} if ids else {}
    active = _active()
    for a in albums:
        a.update(_state(rows.get(a["deezer_id"]), active, a.get("artist") or "", a["title"]))
    return albums


def recent_releases(days: int = 90) -> list[dict]:
    """Albums and EPs of your artists released in the last `days` days or coming out, newest first."""
    rows = db.q("""SELECT a.*, ar.name artist, ar.deezer_id artist_deezer_id FROM music_albums a
        JOIN music_artists ar ON ar.id=a.artist_id
        WHERE a.release_date >= date('now', ?) AND a.record_type IN ('album','ep')
        ORDER BY a.release_date DESC LIMIT 60""", (f"-{days} days",))
    active = _active()
    return [{"deezer_id": r["deezer_id"], "title": r["title"], "cover": r["cover"], "release_date": r["release_date"],
             "record_type": r["record_type"], "artist": r["artist"], "artist_id": r["artist_deezer_id"],
             **_state(r, active, r["artist"], r["title"])} for r in rows]


def artists() -> list[dict]:
    return db.q("""SELECT ar.*, (SELECT COUNT(*) FROM music_albums a WHERE a.artist_id=ar.id AND a.folder IS NOT NULL) have,
        (SELECT COUNT(*) FROM music_albums a WHERE a.artist_id=ar.id AND a.folder IS NULL AND a.wanted=1) wanted
        FROM music_artists ar ORDER BY ar.name COLLATE NOCASE""")


def artist_state(artist_id: int) -> dict:
    art = db.one("SELECT * FROM music_artists WHERE id=?", (artist_id,))
    if not art:
        raise ValueError("Artist not found")
    active = _active()
    albums = db.q("SELECT * FROM music_albums WHERE artist_id=? ORDER BY release_date DESC", (artist_id,))
    out = []
    for r in albums:
        out.append({"deezer_id": r["deezer_id"], "title": r["title"], "cover": r["cover"], "release_date": r["release_date"],
                    "record_type": r["record_type"], "explicit": bool(r["explicit"]), "tracks": r["tracks"],
                    "artist": art["name"], "artist_id": art["deezer_id"], **_state(r, active, art["name"], r["title"])})
    return {"artist": art, "albums": out}


def album_state(album_id: int) -> dict:
    r = db.one("SELECT a.*, ar.name artist, ar.deezer_id artist_deezer_id FROM music_albums a "
               "JOIN music_artists ar ON ar.id=a.artist_id WHERE a.id=?", (album_id,))
    if not r:
        raise ValueError("Album not found")
    return {**r, **_state(r, _active(), r["artist"], r["title"])}


async def artist_page(deezer_id: int) -> dict:
    """An artist as Deezer has it, with what Omniarr / the music folder has of each album."""
    row = _artist_row(deezer_id)
    if row:
        if not row["last_refresh"] or not db.one("SELECT 1 FROM music_albums WHERE artist_id=?", (row["id"],)):
            await refresh_artist(row["id"])
        st = artist_state(row["id"])
        return {"artist": {**(await deezer.artist(deezer_id)), **{k: row[k] for k in ("id", "monitored")}},
                "tracked": True, "albums": st["albums"], "missing": len(missing_editions(row["id"]))}
    art = await deezer.artist(deezer_id)
    albums = annotate(await deezer.artist_albums(deezer_id))
    albums.sort(key=lambda a: a.get("release_date") or "", reverse=True)
    return {"artist": art, "tracked": False, "albums": albums}


# ------------------------------------------------------------- downloading --
_task: asyncio.Task | None = None


def cancel(download_id: int) -> None:
    deemix.cancel(str(download_id))
    db.ex("UPDATE downloads SET status='removed', message='Cancelled', updated=datetime('now') WHERE id=?", (download_id,))


def next_album() -> dict | None:
    return db.one("""SELECT a.*, ar.name artist FROM music_albums a JOIN music_artists ar ON ar.id=a.artist_id
        WHERE a.wanted=1 AND a.folder IS NULL AND (a.release_date IS NULL OR a.release_date <= date('now', 'localtime'))
          AND (a.last_try IS NULL OR a.last_try < datetime('now', ?))
        ORDER BY a.last_try IS NOT NULL, a.added, a.id LIMIT 1""", (f"-{RETRY_HOURS} hours",))


async def process() -> None:
    """Scheduler job: start the next wanted album when nothing is downloading."""
    global _task
    if _task and not _task.done():
        return
    # left 'downloading' by a restart: try again
    for d in db.q("SELECT id, music_album_id FROM downloads WHERE music_album_id IS NOT NULL AND status IN ('queued','downloading')"):
        db.ex("UPDATE downloads SET status='failed', message='Interrupted (Omniarr restarted)' WHERE id=?", (d["id"],))
        db.ex("UPDATE music_albums SET last_try=NULL WHERE id=?", (d["music_album_id"],))
    if not (db.setting("deezer_arl") or "").strip() or deemix.available():
        return
    a = next_album()
    if not a:
        return
    q = db.setting("music_quality") or "flac"
    dl_id = db.insert("downloads", {"kind": "music", "music_album_id": a["id"], "release_title": f"{a['artist']} - {a['title']}",
                                    "indexer": "Deezer", "quality": {"flac": "FLAC", "320": "MP3 320", "128": "MP3 128"}.get(q, q),
                                    "status": "downloading", "progress": 0})
    db.update("music_albums", a["id"], {"last_try": db.one("SELECT datetime('now') t")["t"]})
    _task = asyncio.create_task(_run(dl_id, a, q))


async def _run(dl_id: int, a: dict, quality: str) -> None:
    last = {"p": -10.0}

    def progress(p: float):
        if p - last["p"] >= 2 or p >= 100:
            last["p"] = p
            db.ex("UPDATE downloads SET progress=?, updated=datetime('now') WHERE id=? AND status='downloading'",
                  (round(p, 1), dl_id))
    existing = await asyncio.to_thread(artist_dir, a["artist"])
    dest = existing or os.path.join(root(), letter(a["artist"]))
    before = await asyncio.to_thread(find_album, a["artist"], a["title"], True)
    if before and before.startswith(root()):  # a try that was cut off left half-written files
        gone = await asyncio.to_thread(remove_cut_off, before)
        if gone:
            db.log(f"Music: {a['artist']} - {a['title']}: fetching {len(gone)} cut-off track(s) again")
    try:
        errors = await deemix.download(f"https://www.deezer.com/album/{a['deezer_id']}", dest, quality, progress,
                                       key=str(dl_id), artist_folder=not existing)
        if (db.one("SELECT status FROM downloads WHERE id=?", (dl_id,)) or {}).get("status") == "removed":
            return  # cancelled
        expected = a.get("tracks")
        if not expected:
            try:
                expected = (await deezer.album(a["deezer_id"])).get("tracks")
                if expected:
                    db.update("music_albums", a["id"], {"tracks": expected})
            except Exception:  # noqa: BLE001
                expected = None
        forget_index()
        folder = await asyncio.to_thread(find_album, a["artist"], a["title"], True)
        if folder and folder.startswith(root()):
            await asyncio.to_thread(remove_cut_off, folder)  # never count (or keep) a half-written track
        got = await asyncio.to_thread(count_audio, folder) if folder and folder.startswith(root()) else 0
        missing = sum(1 for e in errors if deemix.unavailable(e))  # tracks Deezer doesn't have at this quality
        others = [e for e in errors if not deemix.unavailable(e)]
        label = deemix.LABEL.get(quality, quality)
        complete = (got + missing >= expected) if expected else not others
        if folder and folder.startswith(root()) and got and complete:
            await asyncio.to_thread(_tidy, folder, True)
            note = f"{missing} track{'s' if missing != 1 else ''} not in {label} on Deezer" if missing else None
            db.update("music_albums", a["id"], {"folder": folder, "message": note})
            db.ex("UPDATE downloads SET status='imported', progress=100, dest_dir=?, message=?, updated=datetime('now') WHERE id=?",
                  (folder, f"{got} tracks" + (f" ({note})" if note else ""), dl_id))
            db.log(f"Music: downloaded {a['artist']} - {a['title']} ({got} tracks{', ' + note if note else ''})")
            from .services import mediaserver
            mediaserver.scan_soon([folder])
            return
        if folder and folder.startswith(root()) and not got:
            await asyncio.to_thread(_tidy, folder, False)
            folder = None
        if not got and missing and not others:  # nothing at this quality: a retry won't help
            msg = f"Not in {label} on Deezer"
            db.update("music_albums", a["id"], {"wanted": 0, "message": msg})
            db.ex("UPDATE downloads SET status='failed', message=?, updated=datetime('now') WHERE id=?", (msg, dl_id))
            db.log(f"Music: {a['artist']} - {a['title']}: {msg} (not retried)", "warn")
            return
        why = (others or errors)[0] if errors else "no files came down"
        msg = f"{got} of {expected or '?'} tracks" + (f": {why}" if why else "")
        db.update("music_albums", a["id"], {"message": msg})
        db.ex("UPDATE downloads SET status='failed', message=?, dest_dir=?, updated=datetime('now') WHERE id=?",
              (msg, folder, dl_id))
        db.log(f"Music: {a['artist']} - {a['title']}: {msg} (tries again in {RETRY_HOURS} h)", "warn")
    except Exception as e:  # noqa: BLE001
        db.update("music_albums", a["id"], {"message": str(e)})
        db.ex("UPDATE downloads SET status='failed', message=?, updated=datetime('now') WHERE id=?", (str(e), dl_id))
        db.log(f"Music: {a['artist']} - {a['title']} failed: {e}", "warn")


async def retry(album_id: int) -> None:
    db.update("music_albums", album_id, {"wanted": 1, "last_try": None, "message": None})
    await process()
