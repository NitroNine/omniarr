"""Backup to a .zip and restore it (moving to a new server)."""
import asyncio
import io
import json
import os
import time
import zipfile

import pytest
from fastapi.testclient import TestClient

from app import backup, db


def _seed():
    db.ex("INSERT INTO movies (tmdb_id, title, year, file_path) VALUES (949, 'Heat', 1995, '/data/movies/Heat (1995)/Heat.mkv')")
    db.ex("INSERT INTO indexers (name, url, apikey, priority) VALUES ('altHUB', 'https://alt.test', 'KEY1', 0)")
    q = os.path.join(db.CONFIG_DIR, "nzbget", "queue")
    os.makedirs(q, exist_ok=True)
    with open(os.path.join(q, "queue"), "w") as f:
        f.write("nzbget queue state")


def test_backup_and_restore_round_trip(env):
    _seed()
    name, data = backup.make_backup()
    assert name.startswith("omniarr-backup-") and name.endswith(".zip")
    z = zipfile.ZipFile(io.BytesIO(data))
    assert {"manifest.json", "omniarr.db", "nzbget/queue/queue"} <= set(z.namelist())
    man = json.loads(z.read("manifest.json"))
    assert man["counts"]["movies"] == 1 and man["counts"]["indexers"] >= 1

    # "new server": a different library, queue gone
    db.ex("DELETE FROM movies")
    db.ex("INSERT INTO movies (tmdb_id, title) VALUES (1, 'Other')")
    os.remove(os.path.join(db.CONFIG_DIR, "nzbget", "queue", "queue"))
    res = asyncio.run(backup.restore(data))
    assert [r["title"] for r in db.q("SELECT title FROM movies")] == ["Heat"]
    assert db.one("SELECT apikey FROM indexers WHERE name='altHUB'")["apikey"] == "KEY1"
    assert open(os.path.join(db.CONFIG_DIR, "nzbget", "queue", "queue")).read() == "nzbget queue state"
    assert res["counts"]["movies"] == 1 and res["undo"].startswith("before-restore-")
    # what was replaced can be restored again
    undo = backup.stored(res["undo"])
    asyncio.run(backup.restore(undo))
    assert [r["title"] for r in db.q("SELECT title FROM movies")] == ["Other"]


def test_restore_rejects_bad_files(env):
    _seed()
    with pytest.raises(ValueError, match="isn't an Omniarr backup"):
        asyncio.run(backup.restore(b"not a zip"))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("manifest.json", "{}")
        z.writestr("omniarr.db", b"garbage" * 100)
    with pytest.raises(ValueError, match="damaged"):
        asyncio.run(backup.restore(buf.getvalue()))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("manifest.json", "{}")
        z.writestr("omniarr.db", b"")
        z.writestr("../evil", b"x")
    with pytest.raises(ValueError, match="unsafe"):
        asyncio.run(backup.restore(buf.getvalue()))
    assert db.one("SELECT title FROM movies")["title"] == "Heat"  # untouched


def test_auto_backup_daily_and_keeps_seven(env):
    d = backup.backup_dir()
    os.makedirs(d, exist_ok=True)
    for i in range(9):
        p = os.path.join(d, f"omniarr-backup-2026010{i}-000000.zip")
        open(p, "wb").write(b"x")
        os.utime(p, (time.time() - 3 * 86400, time.time() - 3 * 86400))
    assert backup.auto_backup()  # newest is 3 days old -> makes one
    assert len([f for f in os.listdir(d) if f.startswith("omniarr-backup-")]) == 7
    assert backup.auto_backup() == ""  # one made just now -> skip
    assert backup.auto_backup(force=True)


def test_api_admin_only(env):
    from app import auth
    from app.main import app
    _seed()
    auth._failures.clear()
    auth.create_user("boss", "bosspassword", role="admin")
    auth.create_user("kid", "kidpassword")
    kid = TestClient(app)
    kid.post("/api/auth/login", json={"username": "kid", "password": "kidpassword"})
    assert kid.get("/api/backup").status_code == 403
    assert kid.post("/api/restore", content=b"x").status_code == 403
    c = TestClient(app)
    c.post("/api/auth/login", json={"username": "boss", "password": "bosspassword"})
    r = c.get("/api/backup")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    assert "omniarr-backup-" in r.headers["content-disposition"]
    assert c.post("/api/restore", content=b"nope").status_code == 400
    assert c.get("/api/backups/omniarr.db").status_code == 404  # only .zip files in backups/
    r = c.get("/api/backups/..%2Fomniarr.db")  # never the database itself
    assert "zip" not in r.headers.get("content-type", "") and b"SQLite format" not in r.content
