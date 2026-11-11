"""SQLite storage. One small file in /config, no external database needed."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any

CONFIG_DIR = os.environ.get("OMNIARR_CONFIG", "/config")
DB_PATH = os.path.join(CONFIG_DIR, "omniarr.db")

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS usenet_servers (
  id INTEGER PRIMARY KEY, name TEXT, host TEXT NOT NULL, port INTEGER DEFAULT 563, ssl INTEGER DEFAULT 1,
  username TEXT, password TEXT, connections INTEGER DEFAULT 8, level INTEGER DEFAULT 0,
  retention INTEGER DEFAULT 0, active INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS indexers (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, url TEXT NOT NULL, apikey TEXT NOT NULL,
  movie_categories TEXT DEFAULT '2000', tv_categories TEXT DEFAULT '5000',
  enabled INTEGER DEFAULT 1, priority INTEGER DEFAULT 25
);

-- one row per version: a title can have a 1080p row ('hd') and a 4K row ('uhd')
CREATE TABLE IF NOT EXISTS movies (
  id INTEGER PRIMARY KEY, tmdb_id INTEGER NOT NULL, imdb_id TEXT, title TEXT NOT NULL,
  original_title TEXT, year INTEGER, overview TEXT, poster TEXT, runtime INTEGER,
  release_date TEXT, tier TEXT NOT NULL DEFAULT 'hd', monitored INTEGER DEFAULT 1,
  folder TEXT, file_path TEXT, file_quality TEXT, file_score INTEGER, file_size INTEGER,
  added TEXT DEFAULT CURRENT_TIMESTAMP, last_search TEXT, collection_id INTEGER, collection_name TEXT,
  digital_release TEXT, physical_release TEXT, last_refresh TEXT,
  UNIQUE(tmdb_id, tier)
);

CREATE TABLE IF NOT EXISTS series (
  id INTEGER PRIMARY KEY, tvdb_id INTEGER, imdb_id TEXT, tmdb_id INTEGER,
  title TEXT NOT NULL, year INTEGER, overview TEXT, poster TEXT, runtime INTEGER,
  status TEXT, series_type TEXT DEFAULT 'standard', tier TEXT NOT NULL DEFAULT 'hd',
  monitored INTEGER DEFAULT 1, folder TEXT, added TEXT DEFAULT CURRENT_TIMESTAMP,
  last_refresh TEXT, UNIQUE(tvdb_id, tier), UNIQUE(tmdb_id, tier)
);

CREATE TABLE IF NOT EXISTS episodes (
  id INTEGER PRIMARY KEY, series_id INTEGER NOT NULL REFERENCES series(id) ON DELETE CASCADE,
  tvdb_id INTEGER, season INTEGER NOT NULL, episode INTEGER NOT NULL, absolute INTEGER,
  title TEXT, air_date TEXT, overview TEXT, monitored INTEGER DEFAULT 1,
  file_path TEXT, file_quality TEXT, file_score INTEGER, file_size INTEGER, last_search TEXT,
  UNIQUE(series_id, season, episode)
);

CREATE TABLE IF NOT EXISTS downloads (
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL, movie_id INTEGER, series_id INTEGER,
  episode_ids TEXT, release_title TEXT NOT NULL, indexer TEXT, guid TEXT, size INTEGER,
  quality TEXT, score INTEGER, custom_formats TEXT, nzbget_id INTEGER,
  status TEXT NOT NULL DEFAULT 'queued', progress REAL DEFAULT 0, message TEXT,
  dest_dir TEXT, added TEXT DEFAULT CURRENT_TIMESTAMP, updated TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS blocklist (
  id INTEGER PRIMARY KEY, release_title TEXT NOT NULL, guid TEXT, reason TEXT,
  added TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS collections (
  id INTEGER PRIMARY KEY, source TEXT NOT NULL DEFAULT 'collection', tmdb_id INTEGER NOT NULL,
  name TEXT NOT NULL, overview TEXT, poster TEXT, tier TEXT NOT NULL DEFAULT 'hd',
  monitored INTEGER DEFAULT 1, search_on_add INTEGER DEFAULT 1, excluded TEXT DEFAULT '[]',
  members TEXT DEFAULT '[]',
  added TEXT DEFAULT CURRENT_TIMESTAMP, last_refresh TEXT, UNIQUE(source, tmdb_id)
);

CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL COLLATE NOCASE, password_hash TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'user', auto_approve INTEGER DEFAULT 0, allow_uhd INTEGER DEFAULT 0,
  enabled INTEGER DEFAULT 1, created TEXT DEFAULT CURRENT_TIMESTAMP, last_login TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  expires REAL NOT NULL, created TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS requests (
  id INTEGER PRIMARY KEY, user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
  kind TEXT NOT NULL, target_id INTEGER NOT NULL, source TEXT, title TEXT, year INTEGER, poster TEXT,
  tier TEXT NOT NULL DEFAULT 'hd', options TEXT DEFAULT '{}', status TEXT NOT NULL DEFAULT 'pending',
  note TEXT, item_id INTEGER, decided_by INTEGER, decided_at TEXT,
  created TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY, level TEXT, message TEXT, ts TEXT DEFAULT CURRENT_TIMESTAMP
);
"""

DEFAULT_SETTINGS: dict[str, Any] = {
    # metadata
    "tmdb_api_key": "",
    "metadata_language": "en",
    "streaming_region": "US",  # country for "where to watch" (Discover)
    "streaming_hide_ads": True,  # leave "... with Ads" plans of paid services off the Streaming tab
    # Downloads. "builtin" = the NZBGet that ships inside Omniarr (servers set in Omniarr);
    # "external" = a separate NZBGet reached at nzbget_url.
    "download_client": "builtin",
    # built-in: completed downloads land in <dir>/completed/<category>. Empty = the Downloads path
    # from the Unraid template (/downloads) when it's mapped, else /data/usenet.
    "download_dir": "",
    "download_speed_limit": 0,  # built-in: KB/s, 0 = unlimited
    "download_min_free_gb": 20,
    "english_only": True,
    # Hallmark Christmas tab: auto-add new Hallmark Christmas movies (hallmark.py)
    "hallmark_auto": False,
    "hallmark_tier": "hd",
    "hallmark_seen": "[]",  # TMDB ids already added (or skipped because they were in the library): never re-added
    "hallmark_last_sync": "",  # skip releases naming another language, MULTI or dual audio
    "failed_retry_limit": 3,  # immediate re-searches after failed downloads, per item per day (0 = no limit)  # don't grab when the download disk would drop below this
    "_downloader_password": "",  # built-in: generated, localhost-only control password
    # external NZBGet
    "nzbget_url": "http://nzbget:6789",
    "nzbget_username": "nzbget",
    "nzbget_password": "",
    "nzbget_category_movies": "movies",
    "nzbget_category_tv": "tv",
    # root folders: one for movies, one for TV. 1080p and 4K versions of a title share its folder.
    "root_movies": "/data/media/movies",
    "root_tv": "/data/media/tv",
    # path the download client uses vs. what this container sees
    "remote_path_from": "",
    "remote_path_to": "",
    # TRaSH profile choices (file stems in docs/json/<app>/quality-profiles)
    "profile_movies_hd": "hd-bluray-web",
    "profile_movies_uhd": "uhd-bluray-web",
    "profile_tv_hd": "web-1080p",
    "profile_tv_uhd": "web-2160p",
    "trash_score_set": "default",
    "trash_branch": "master",
    "trash_auto_sync_hours": 24,
    # naming (keys into TRaSH naming json)
    "naming_movie_folder": "plex-tmdb",
    "naming_movie_file": "plex-tmdb",
    "naming_series_folder": "plex-tvdb",
    "naming_season_folder": "default",
    "naming_episode_file": "default",
    "import_mode": "move",  # move | hardlink | copy
    # scheduling
    "search_interval_minutes": 60,
    "rss_interval_minutes": 15,
    "series_refresh_hours": 12,
    "upgrades_enabled": True,
    # Per-library overrides of the TRaSH profile, keyed movies_hd / movies_uhd / tv_hd / tv_uhd:
    # {"allowed": [quality item names] | null, "cutoff": item name | "", "upgrades": bool,
    #  "cutoff_score": int | null}
    "quality_overrides": {},
}


def connect() -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            _conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
            _conn.row_factory = sqlite3.Row
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.execute("PRAGMA foreign_keys=ON")
            _conn.executescript(SCHEMA)
            _migrate(_conn)
        return _conn


# columns added after the first release: (table, column, definition)
MIGRATIONS = [
    ("movies", "collection_id", "INTEGER"),
    ("movies", "collection_name", "TEXT"),
    ("movies", "digital_release", "TEXT"),
    ("movies", "physical_release", "TEXT"),
    ("movies", "last_refresh", "TEXT"),
]


def _migrate(c: sqlite3.Connection) -> None:
    # Older versions logged indexer URLs with the API key in them
    import re
    for eid, msg in c.execute("SELECT id, message FROM events WHERE message LIKE '%apikey=%' OR message LIKE '%&r=%'").fetchall():
        c.execute("UPDATE events SET message=? WHERE id=?",
                  (re.sub(r"([?&](?:api_?key|apitoken|token|r)=)[^&\s'\"<>]+", r"\1***", msg, flags=re.I), eid))
    # Root folders used to be split into 1080p and 4K; now there's one per media type
    # (versions share the title folder). Keep the 1080p folder the user had set.
    for kind in ("movies", "tv"):
        if not c.execute("SELECT 1 FROM settings WHERE key=?", (f"root_{kind}",)).fetchone():
            c.execute("UPDATE settings SET key=? WHERE key=?", (f"root_{kind}", f"root_{kind}_hd"))
        c.execute("DELETE FROM settings WHERE key IN (?, ?)", (f"root_{kind}_hd", f"root_{kind}_uhd"))
    # Installs from before the built-in downloader keep using the NZBGet they set up
    if not c.execute("SELECT 1 FROM settings WHERE key='download_client'").fetchone():
        row = c.execute("SELECT value FROM settings WHERE key='nzbget_password'").fetchone()
        if row and row["value"] not in ('""', "", "null"):
            c.execute("INSERT INTO settings(key, value) VALUES('download_client', '\"external\"')")
    # TheTVDB support was removed: TV comes from TMDB only
    c.execute("DELETE FROM settings WHERE key IN ('tvdb_api_key', 'tvdb_pin', 'tv_metadata_source')")
    # Older databases allowed one row per title (tmdb_id / tvdb_id UNIQUE). Rows are now
    # per version (1080p / 4K), so rebuild those tables with UNIQUE(id, tier).
    for table in ("movies", "series"):
        _rebuild_if_old(c, table)
    for table, col, decl in MIGRATIONS:
        cols = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def _create_sql(table: str) -> str:
    start = SCHEMA.index(f"CREATE TABLE IF NOT EXISTS {table} (")
    return SCHEMA[start:SCHEMA.index(");", start) + 2]


def _rebuild_if_old(c: sqlite3.Connection, table: str) -> None:
    row = c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    if not row or "UNIQUE(tmdb_id,tier)" in row["sql"].replace(" ", ""):
        return
    old_cols = [r["name"] for r in c.execute(f"PRAGMA table_info({table})")]
    c.execute("PRAGMA foreign_keys=OFF")
    c.execute("BEGIN")
    try:
        c.execute(_create_sql(table).replace(f"CREATE TABLE IF NOT EXISTS {table} (", f"CREATE TABLE {table}_new (", 1))
        new_cols = {r["name"] for r in c.execute(f"PRAGMA table_info({table}_new)")}
        cols = ",".join(x for x in old_cols if x in new_cols)
        c.execute(f"INSERT INTO {table}_new ({cols}) SELECT {cols} FROM {table}")
        c.execute(f"DROP TABLE {table}")
        c.execute(f"ALTER TABLE {table}_new RENAME TO {table}")
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise
    finally:
        c.execute("PRAGMA foreign_keys=ON")


def reset_for_tests(path: str) -> None:
    global _conn, DB_PATH, CONFIG_DIR
    with _lock:
        if _conn:
            _conn.close()
        _conn = None
        CONFIG_DIR = os.path.dirname(path)
        DB_PATH = path


@contextmanager
def tx():
    c = connect()
    with _lock:
        c.execute("BEGIN")
        try:
            yield c
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise


def q(sql: str, params: tuple | dict = ()) -> list[dict]:
    with _lock:
        return [dict(r) for r in connect().execute(sql, params).fetchall()]


def one(sql: str, params: tuple | dict = ()) -> dict | None:
    rows = q(sql, params)
    return rows[0] if rows else None


def ex(sql: str, params: tuple | dict = ()) -> int:
    with _lock:
        cur = connect().execute(sql, params)
        return cur.lastrowid


def insert(table: str, data: dict) -> int:
    cols = ",".join(data)
    marks = ",".join("?" for _ in data)
    return ex(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(data.values()))


def update(table: str, row_id: int, data: dict) -> None:
    if not data:
        return
    sets = ",".join(f"{k}=?" for k in data)
    ex(f"UPDATE {table} SET {sets} WHERE id=?", (*data.values(), row_id))


# ---- settings -----------------------------------------------------------
def get_settings() -> dict[str, Any]:
    s = dict(DEFAULT_SETTINGS)
    for r in q("SELECT key, value FROM settings"):
        try:
            s[r["key"]] = json.loads(r["value"])
        except json.JSONDecodeError:
            s[r["key"]] = r["value"]
    # environment variables override (handy for Unraid templates)
    for k in DEFAULT_SETTINGS:
        env = os.environ.get("OMNIARR_" + k.upper())
        if env is not None and env != "":
            d = DEFAULT_SETTINGS[k]
            if isinstance(d, dict):
                try:
                    s[k] = json.loads(env)
                except json.JSONDecodeError:
                    pass
                continue
            s[k] = int(env) if isinstance(d, int) and not isinstance(d, bool) else (
                env.lower() in ("1", "true", "yes") if isinstance(d, bool) else env)
    return s


def setting(key: str) -> Any:
    return get_settings().get(key)


def save_settings(values: dict[str, Any]) -> None:
    with tx() as c:
        for k, v in values.items():
            if k in DEFAULT_SETTINGS:
                c.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                          (k, json.dumps(v)))


def log(message: str, level: str = "info") -> None:
    import logging
    import re
    message = re.sub(r"([?&](?:api_?key|apitoken|token|r)=)[^&\s'\"<>]+", r"\1***", str(message), flags=re.I)
    logging.getLogger("omniarr").log(logging.WARNING if level == "warn" else logging.ERROR if level == "error" else logging.INFO, message)
    ex("INSERT INTO events(level, message) VALUES(?,?)", (level, message))
    ex("DELETE FROM events WHERE id < (SELECT MAX(id) - 2000 FROM events)")
