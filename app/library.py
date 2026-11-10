"""Library management: adding movies/series, refreshing TVDB episode lists,
root-folder resolution by quality tier, and folder naming."""
from __future__ import annotations

import os
import shutil
from datetime import date

from . import db, naming, trash
from .services import metadata

TIERS = ("hd", "uhd")


def root_for(kind: str, tier: str) -> str:
    key = f"root_{'movies' if kind == 'movie' else 'tv'}_{tier}"
    return db.setting(key)


def movie_folder_name(movie: dict) -> str:
    fmt = trash.ensure_loaded().naming_format("movie", "folder", db.setting("naming_movie_folder")) \
        or "{Movie CleanTitle} ({Release Year})"
    return naming.safe(naming.render(fmt, naming.movie_values(movie)))


def series_folder_name(series: dict) -> str:
    fmt = trash.ensure_loaded().naming_format("tv", "series", db.setting("naming_series_folder")) \
        or "{Series TitleYear}"
    return naming.safe(naming.render(fmt, naming.series_values(series)))


def season_folder_name(season: int) -> str:
    fmt = trash.ensure_loaded().naming_format("tv", "season", db.setting("naming_season_folder")) \
        or "Season {season:00}"
    if season == 0:
        return "Specials"
    return naming.safe(naming.render(fmt, {"season": season}))


def movie_dir(movie: dict) -> str:
    return os.path.join(root_for("movie", movie["tier"]), movie["folder"])


def series_dir(series: dict) -> str:
    return os.path.join(root_for("tv", series["tier"]), series["folder"])


# ----------------------------------------------------------------- movies --
async def add_movie(tmdb_id: int, tier: str, monitored: bool = True) -> dict:
    if tier not in TIERS:
        raise ValueError("tier must be 'hd' or 'uhd'")
    existing = db.one("SELECT * FROM movies WHERE tmdb_id=?", (tmdb_id,))
    if existing:
        raise ValueError(f"{existing['title']} is already in your library")
    m = await metadata.movie_details(tmdb_id)
    m.update(tier=tier, monitored=int(monitored))
    m["folder"] = movie_folder_name(m)
    m["id"] = db.insert("movies", m)
    db.log(f"Added movie {m['title']} ({m.get('year')}) [{tier.upper()}]")
    return m


async def refresh_movie(movie_id: int) -> None:
    m = db.one("SELECT * FROM movies WHERE id=?", (movie_id,))
    if not m:
        return
    d = await metadata.movie_details(m["tmdb_id"])
    db.update("movies", movie_id, {k: d[k] for k in ("imdb_id", "title", "original_title", "year", "overview",
                                                       "poster", "runtime", "release_date")})


def movie_available(m: dict) -> bool:
    rd = m.get("release_date")
    if not rd:
        return True
    try:
        # digital/physical usually ~ 3 months after theatrical; search from release on
        return date.fromisoformat(rd) <= date.today()
    except ValueError:
        return True


# ----------------------------------------------------------------- series --
async def add_series(tvdb_id: int, tier: str, monitor: str = "all") -> dict:
    """monitor: all | future | missing | none | latest"""
    if tier not in TIERS:
        raise ValueError("tier must be 'hd' or 'uhd'")
    existing = db.one("SELECT * FROM series WHERE tvdb_id=?", (tvdb_id,))
    if existing:
        raise ValueError(f"{existing['title']} is already in your library")
    s = await metadata.series_details(tvdb_id)
    s.update(tier=tier, monitored=int(monitor != "none"))
    s["folder"] = series_folder_name(s)
    s["id"] = db.insert("series", s)
    await refresh_series(s["id"], initial_monitor=monitor)
    db.log(f"Added series {s['title']} [{tier.upper()}]")
    return s


async def refresh_series(series_id: int, initial_monitor: str | None = None) -> None:
    s = db.one("SELECT * FROM series WHERE id=?", (series_id,))
    if not s:
        return
    try:
        d = await metadata.series_details(s["tvdb_id"])
        db.update("series", series_id, {k: d[k] for k in ("title", "year", "overview", "poster", "imdb_id",
                                                          "tmdb_id", "runtime", "status")})
    except Exception as e:  # noqa: BLE001
        db.log(f"Refreshing {s['title']} failed: {e}", "warn")
        return
    eps = await metadata.series_episodes(s["tvdb_id"])
    today = date.today().isoformat()
    latest_season = max((e["season"] for e in eps), default=0)
    with db.tx() as c:
        for e in eps:
            row = c.execute("SELECT id FROM episodes WHERE series_id=? AND season=? AND episode=?",
                            (series_id, e["season"], e["episode"])).fetchone()
            if row:
                c.execute("UPDATE episodes SET tvdb_id=?, absolute=?, title=?, air_date=?, overview=? WHERE id=?",
                          (e["tvdb_id"], e["absolute"], e["title"], e["air_date"], e["overview"], row["id"]))
            else:
                mon = e["season"] > 0
                if initial_monitor == "future":
                    mon = mon and (not e["air_date"] or e["air_date"] > today)
                elif initial_monitor == "latest":
                    mon = mon and e["season"] == latest_season
                elif initial_monitor == "none":
                    mon = False
                elif initial_monitor is None:  # new episode found on a later refresh
                    mon = mon and bool(s["monitored"])
                c.execute("INSERT INTO episodes(series_id,tvdb_id,season,episode,absolute,title,air_date,overview,monitored)"
                          " VALUES(?,?,?,?,?,?,?,?,?)",
                          (series_id, e["tvdb_id"], e["season"], e["episode"], e["absolute"], e["title"],
                           e["air_date"], e["overview"], int(mon)))
        c.execute("UPDATE series SET last_refresh=datetime('now') WHERE id=?", (series_id,))


def wanted_episodes(series_id: int | None = None) -> list[dict]:
    sql = ("SELECT e.* FROM episodes e JOIN series s ON s.id=e.series_id WHERE s.monitored=1 AND e.monitored=1 "
           "AND e.air_date IS NOT NULL AND e.air_date <= date('now')")
    params: tuple = ()
    if series_id:
        sql += " AND e.series_id=?"
        params = (series_id,)
    rows = db.q(sql + " ORDER BY e.air_date DESC", params)
    out = []
    upgrades = db.setting("upgrades_enabled")
    profiles: dict = {}
    for e in rows:
        if not e["file_path"]:
            out.append(e)
        elif upgrades:
            s = db.one("SELECT tier FROM series WHERE id=?", (e["series_id"],))
            from .decision import profile_for
            prof = profiles.setdefault(s["tier"], profile_for("tv", s["tier"]))
            if not cutoff_met(prof, e["file_quality"], e["file_score"]):
                out.append(e)
    return out


def cutoff_met(profile, quality: str | None, score: int | None) -> bool:
    if not quality:
        return False
    r = profile.quality_rank(quality)
    r = 999 if r is None else r
    return r <= profile.cutoff_rank() and (score or 0) >= profile.cutoff_format_score


# --------------------------------------------------------------- tier move --
def change_tier(kind: str, item_id: int, tier: str) -> None:
    """Switch an item between the 1080p and 4K libraries, moving existing files."""
    if tier not in TIERS:
        raise ValueError("tier must be 'hd' or 'uhd'")
    table = "movies" if kind == "movie" else "series"
    item = db.one(f"SELECT * FROM {table} WHERE id=?", (item_id,))
    if not item or item["tier"] == tier:
        return
    old = os.path.join(root_for(kind, item["tier"]), item["folder"])
    new = os.path.join(root_for(kind, tier), item["folder"])
    if os.path.isdir(old):
        os.makedirs(os.path.dirname(new), exist_ok=True)
        shutil.move(old, new)
        if kind == "movie" and item["file_path"]:
            db.update("movies", item_id, {"file_path": item["file_path"].replace(old, new, 1)})
        else:
            for e in db.q("SELECT id, file_path FROM episodes WHERE series_id=? AND file_path IS NOT NULL", (item_id,)):
                db.update("episodes", e["id"], {"file_path": e["file_path"].replace(old, new, 1)})
    db.update(table, item_id, {"tier": tier})
    db.log(f"Moved {item['title']} to the {'4K' if tier == 'uhd' else '1080p'} library")
