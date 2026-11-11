"""Backup and restore of everything Omniarr knows, for moving to another server.

A backup is a .zip with:
  omniarr.db      the database: library (movies, shows, episodes and where their files are), settings, indexers,
                  Usenet servers, users, requests, download history, blocklist, collections
  nzbget/queue/   the built-in NZBGet's queue, so unfinished downloads carry on
  manifest.json   what's inside, plus the folders it expects (to check the new server's container mappings)
Media files themselves are not in it: they stay in the media share, which moves separately. Library paths are
container paths (/data/…), so they work again as long as the new container maps the same folders.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sqlite3
import tempfile
import time
import zipfile

from . import db

FORMAT = 1
KEEP = 7  # automatic backups kept
restoring = False  # the scheduler skips its jobs while a restore swaps the database


def backup_dir() -> str:
    return os.path.join(db.CONFIG_DIR, "backups")


def _queue_dir() -> str:
    return os.path.join(db.CONFIG_DIR, "nzbget", "queue")


def _counts(conn: sqlite3.Connection) -> dict:
    out = {}
    for t in ("movies", "series", "episodes", "indexers", "usenet_servers", "users", "requests", "downloads"):
        try:
            out[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except sqlite3.Error:
            out[t] = None
    return out


def _snapshot(path: str) -> None:
    """A consistent copy of the live database (SQLite's online backup; safe while Omniarr runs)."""
    src = db.connect()
    dst = sqlite3.connect(path)
    try:
        with db._lock:
            src.backup(dst)
    finally:
        dst.close()


def make_backup() -> tuple[str, bytes]:
    """Returns (file name, zip bytes)."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    with tempfile.TemporaryDirectory() as tmp:
        snap = os.path.join(tmp, "omniarr.db")
        _snapshot(snap)
        conn = sqlite3.connect(snap)
        counts = _counts(conn)
        conn.close()
        s = db.get_settings()
        manifest = {"format": FORMAT, "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "counts": counts,
                    "paths": {k: s.get(k) for k in ("root_movies", "root_tv", "download_dir")}}
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("manifest.json", json.dumps(manifest, indent=2))
            z.write(snap, "omniarr.db")
            q = _queue_dir()
            if os.path.isdir(q):
                for dp, _dn, fns in os.walk(q):
                    for f in fns:
                        full = os.path.join(dp, f)
                        z.write(full, os.path.join("nzbget", "queue", os.path.relpath(full, q)))
    return f"omniarr-backup-{stamp}.zip", buf.getvalue()


def auto_backup(force: bool = False) -> str:
    """Daily backup into /config/backups, keeping the newest KEEP. Skipped when the last one is under 20 h old
    (the scheduler also runs it at every start)."""
    d = backup_dir()
    os.makedirs(d, exist_ok=True)
    autos = sorted(f for f in os.listdir(d) if f.startswith("omniarr-backup-") and f.endswith(".zip"))
    if autos and not force and time.time() - os.path.getmtime(os.path.join(d, autos[-1])) < 20 * 3600:
        return ""
    name, data = make_backup()
    path = os.path.join(d, name)
    with open(path + ".part", "wb") as f:
        f.write(data)
    os.replace(path + ".part", path)
    autos = sorted(f for f in os.listdir(d) if f.startswith("omniarr-backup-") and f.endswith(".zip"))
    for old in autos[:-KEEP]:  # before-restore-*.zip copies are kept until removed by hand
        os.remove(os.path.join(d, old))
    return path


def list_backups() -> list[dict]:
    d = backup_dir()
    if not os.path.isdir(d):
        return []
    return [{"name": f, "size": os.path.getsize(os.path.join(d, f)),
             "created": time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(os.path.join(d, f))))}
            for f in sorted(os.listdir(d), reverse=True) if f.endswith(".zip")]


def stored(name: str) -> bytes:
    if os.path.basename(name) != name or not name.endswith(".zip"):
        raise ValueError("No such backup")
    path = os.path.join(backup_dir(), name)
    if not os.path.isfile(path):
        raise ValueError("No such backup")
    with open(path, "rb") as f:
        return f.read()


def inspect(data: bytes) -> tuple[zipfile.ZipFile, dict]:
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
        names = set(z.namelist())
    except zipfile.BadZipFile:
        raise ValueError("That isn't an Omniarr backup (.zip)") from None
    if "omniarr.db" not in names or "manifest.json" not in names:
        raise ValueError("That .zip isn't an Omniarr backup (no omniarr.db / manifest.json in it)")
    manifest = json.loads(z.read("manifest.json"))
    if manifest.get("format", 0) > FORMAT:
        raise ValueError("That backup is from a newer Omniarr. Update this one first.")
    for n in names:  # no absolute paths or ../ tricks
        if n.startswith("/") or ".." in n.split("/"):
            raise ValueError("The backup contains an unsafe path")
    return z, manifest


async def restore(data: bytes) -> dict:
    """Replace this Omniarr's database (and NZBGet queue) with the backup's. The current one is saved first
    as backups/before-restore-<time>.zip, so a restore can be undone by restoring that."""
    global restoring
    from .services import downloader
    z, manifest = inspect(data)
    with tempfile.TemporaryDirectory(dir=db.CONFIG_DIR) as tmp:
        new_db = os.path.join(tmp, "omniarr.db")
        with open(new_db, "wb") as f:
            f.write(z.read("omniarr.db"))
        conn = sqlite3.connect(new_db)
        try:
            ok = conn.execute("PRAGMA integrity_check").fetchone()[0]
            counts = _counts(conn)
        except sqlite3.DatabaseError:
            ok = "not a database"
        finally:
            conn.close()
        if ok != "ok":
            raise ValueError(f"The backup's database is damaged ({ok})")

        os.makedirs(backup_dir(), exist_ok=True)
        undo_name, undo = make_backup()
        undo_path = os.path.join(backup_dir(), undo_name.replace("omniarr-backup-", "before-restore-"))
        with open(undo_path, "wb") as f:
            f.write(undo)

        restoring = True
        try:
            import asyncio
            await asyncio.to_thread(downloader.stop)
            with db._lock:
                if db._conn:
                    db._conn.close()
                db._conn = None
                for ext in ("-wal", "-shm"):
                    if os.path.exists(db.DB_PATH + ext):
                        os.remove(db.DB_PATH + ext)
                os.replace(new_db, db.DB_PATH)
            q = _queue_dir()
            if any(n.startswith("nzbget/queue/") for n in z.namelist()):
                shutil.rmtree(q, ignore_errors=True)
                for n in z.namelist():
                    if n.startswith("nzbget/queue/") and not n.endswith("/"):
                        dest = os.path.join(q, n[len("nzbget/queue/"):])
                        os.makedirs(os.path.dirname(dest), exist_ok=True)
                        with open(dest, "wb") as f:
                            f.write(z.read(n))
            db.connect()  # runs migrations, so an older backup is brought up to date
            db.log(f"Restored the backup from {manifest.get('created', '?')}; the previous state is saved as "
                   f"{os.path.basename(undo_path)}")
        finally:
            restoring = False
        try:
            await downloader.ensure()
        except Exception as e:  # noqa: BLE001
            db.log(f"Built-in downloader didn't start after the restore: {e}", "warn")
    s = db.get_settings()
    missing = [p for p in (s.get("root_movies"), s.get("root_tv")) if p and not os.path.isdir(p)]
    return {"created": manifest.get("created"), "counts": counts, "undo": os.path.basename(undo_path),
            "missing_folders": missing}
