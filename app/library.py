"""Library management: adding movies/series, refreshing episode lists (TMDB),
root-folder resolution by quality tier, and folder naming."""
from __future__ import annotations

import os
import shutil
from datetime import date, timedelta

from . import db, naming, trash
from .services import metadata

TIERS = ("hd", "uhd")
TIER_NAMES = {"hd": "1080p", "uhd": "4K"}


def tiers_of(tier: str) -> list[str]:
    """'hd' | 'uhd' | 'both' -> the versions to add."""
    if tier == "both":
        return list(TIERS)
    if tier not in TIERS:
        raise ValueError("tier must be 'hd', 'uhd' or 'both'")
    return [tier]


def tier_of_quality(quality: str | None) -> str:
    return "uhd" if quality and "2160" in quality else "hd"


def versions(kind: str, item: dict) -> list[dict]:
    """All rows (versions) of the same title, including `item`."""
    if kind == "movie":
        return db.q("SELECT * FROM movies WHERE tmdb_id=? ORDER BY tier", (item["tmdb_id"],))
    rows = []
    if item.get("tmdb_id"):
        rows = db.q("SELECT * FROM series WHERE tmdb_id=? ORDER BY tier", (item["tmdb_id"],))
    if item.get("tvdb_id"):
        seen = {r["id"] for r in rows}
        rows += [r for r in db.q("SELECT * FROM series WHERE tvdb_id=? ORDER BY tier", (item["tvdb_id"],))
                 if r["id"] not in seen]
    return rows or [item]


def sibling(kind: str, item: dict) -> dict | None:
    """The other version of this title, if there is one."""
    return next((v for v in versions(kind, item) if v["id"] != item["id"]), None)


def shares_folder(kind: str, item: dict) -> dict | None:
    """The other version when both versions live in the same folder (shared root folder)."""
    sib = sibling(kind, item)
    if not sib:
        return None
    d = movie_dir if kind == "movie" else series_dir
    return sib if os.path.normpath(d(sib)) == os.path.normpath(d(item)) else None


def root_for(kind: str, tier: str | None = None) -> str:
    """The root folder for movies or TV. Both versions (tiers) of a title live in it, side by side."""
    return db.setting("root_movies" if kind == "movie" else "root_tv")


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


# The Christmas folder (setting christmas_folder) is either a full path, e.g. /data/Christmas Movies: its own root
# next to the movies folder (movies.folder is then just the title's folder name), or a name, e.g. "Christmas Movies":
# a folder inside the movies folder (movies.folder is then "Christmas Movies/<title folder>"). Empty = no separation.
def _cf(value: str | None = None) -> str:
    v = (db.setting("christmas_folder") if value is None else value) or ""
    v = v.strip()
    return v.rstrip("/") if v.startswith("/") else v.strip("/")


def christmas_root(value: str | None = None) -> str:
    """The separate Christmas root folder, or '' when Christmas movies sit inside the movies folder."""
    cf = _cf(value)
    return cf if cf.startswith("/") else ""


def christmas_folder(value: str | None = None) -> str:
    """The Christmas subfolder inside the movies folder, or '' when it is a separate root (or off)."""
    cf = _cf(value)
    return "" if cf.startswith("/") else cf


def _in_christmas(folder: str, value: str | None = None) -> bool:
    cf = christmas_folder(value)
    return bool(cf) and (folder or "").startswith(cf + "/")


def _base_folder(folder: str, value: str | None = None) -> str:
    return folder[len(christmas_folder(value)) + 1:] if _in_christmas(folder, value) else folder


def _folder_for(base: str, on: bool, value: str | None = None) -> str:
    cf = christmas_folder(value)
    return f"{cf}/{base}" if on and cf else base


def _dir_for(folder: str, on: bool, value: str | None = None) -> str:
    root = christmas_root(value) if on and christmas_root(value) else root_for("movie")
    return os.path.join(root, folder)


def movie_dir(movie: dict) -> str:
    return _dir_for(movie["folder"], bool(movie.get("christmas")))


def series_dir(series: dict) -> str:
    return os.path.join(root_for("tv", series["tier"]), series["folder"])


# ----------------------------------------------------------------- movies --
async def add_movie(tmdb_id: int, tier: str, monitored: bool = True, folder: str | None = None,
                    christmas: bool = False) -> dict:
    """Add one version (tier) of a movie. `folder` keeps an existing folder name (library
    import) instead of TRaSH naming; a second version reuses the first one's folder.
    `christmas` puts it in the Christmas folder (setting christmas_folder) inside the movies folder."""
    if tier not in TIERS:
        raise ValueError("tier must be 'hd' or 'uhd'")
    existing = db.one("SELECT * FROM movies WHERE tmdb_id=? AND tier=?", (tmdb_id, tier))
    if existing:
        raise ValueError(f"{existing['title']} is already in your library in {TIER_NAMES[tier]}")
    other = db.one("SELECT * FROM movies WHERE tmdb_id=?", (tmdb_id,))
    m = await metadata.movie_details(tmdb_id)
    m.update(tier=tier, monitored=int(monitored))
    if other:
        christmas = bool(other.get("christmas"))
    if folder:
        christmas = christmas or _in_christmas(folder)
    m["folder"] = folder or (other["folder"] if other else None) or movie_folder_name(m)
    if christmas and not other:
        m["folder"] = _folder_for(_base_folder(m["folder"]), True)
    m["christmas"] = int(christmas)
    m["id"] = db.insert("movies", m)
    db.log(f"Added movie {m['title']} ({m.get('year')}) [{TIER_NAMES[tier]}]")
    return m


async def add_movie_tiers(tmdb_id: int, tier: str, monitored: bool = True, christmas: bool = False) -> list[dict]:
    """Add every requested version that isn't in the library yet ('both' = 1080p and 4K)."""
    wanted = tiers_of(tier)
    have = {r["tier"] for r in db.q("SELECT tier FROM movies WHERE tmdb_id=?", (tmdb_id,))}
    todo = [t for t in wanted if t not in have]
    if not todo:
        title = db.one("SELECT title FROM movies WHERE tmdb_id=?", (tmdb_id,))["title"]
        raise ValueError(f"{title} is already in your library in {' and '.join(TIER_NAMES[t] for t in wanted)}")
    return [await add_movie(tmdb_id, t, monitored, christmas=christmas) for t in todo]


async def refresh_movie(movie_id: int) -> None:
    m = db.one("SELECT * FROM movies WHERE id=?", (movie_id,))
    if not m:
        return
    d = await metadata.movie_details(m["tmdb_id"])
    upd = {k: d[k] for k in ("imdb_id", "title", "original_title", "year", "overview", "poster", "runtime",
                             "release_date", "collection_id", "collection_name", "digital_release", "physical_release")}
    for v in versions("movie", m):  # both versions share the title's metadata
        db.update("movies", v["id"], {**upd, "last_refresh": db.one("SELECT datetime('now') t")["t"]})


async def refresh_due_movies(limit: int = 40) -> None:
    """Keep release dates current for movies still waiting for a file (digital / disc dates get announced late)."""
    rows = db.q("SELECT MIN(id) id FROM movies WHERE file_path IS NULL AND monitored=1 AND "
                "(last_refresh IS NULL OR last_refresh < datetime('now','-1 day')) GROUP BY tmdb_id LIMIT ?", (limit,))
    for r in rows:
        try:
            await refresh_movie(r["id"])
        except Exception as e:  # noqa: BLE001
            db.log(f"Refreshing movie {r['id']} failed: {e}", "warn")


THEATRICAL_TO_HOME_DAYS = 90


def available_date(m: dict) -> str | None:
    """When a movie can be expected on Usenet: its digital or disc release, whichever is first.
    Without either, 90 days after the cinema release (like Radarr). None = no dates at all."""
    home = [d for d in (m.get("digital_release"), m.get("physical_release")) if d]
    if home:
        return min(home)[:10]
    rd = m.get("release_date")
    try:
        return (date.fromisoformat(rd[:10]) + timedelta(days=THEATRICAL_TO_HOME_DAYS)).isoformat() if rd else None
    except ValueError:
        return None


def movie_available(m: dict) -> bool:
    when = available_date(m)
    return when is None or when <= date.today().isoformat()


# ----------------------------------------------------------------- series --
async def add_series(tvdb_id: int | None, tier: str, monitor: str = "all", folder: str | None = None,
                     tmdb_id: int | None = None) -> dict:
    """Add one version (tier) of a show. monitor: all | future | missing | none | latest |
    same (copy episode monitoring from the other version). Give a TheTVDB id, a TMDB id, or both."""
    if tier not in TIERS:
        raise ValueError("tier must be 'hd' or 'uhd'")
    if not tvdb_id and not tmdb_id:
        raise ValueError("A TheTVDB or TMDB id is needed")
    existing = _find_series(tvdb_id, tmdb_id, tier)
    if existing:
        raise ValueError(f"{existing['title']} is already in your library in {TIER_NAMES[tier]}")
    s = await metadata.series_details(tvdb_id=tvdb_id, tmdb_id=tmdb_id)
    existing = _find_series(s.get("tvdb_id"), s.get("tmdb_id"), tier)
    if existing:
        raise ValueError(f"{existing['title']} is already in your library in {TIER_NAMES[tier]}")
    other = _find_series(s.get("tvdb_id") or tvdb_id, s.get("tmdb_id") or tmdb_id)
    s = {k: s.get(k) for k in ("tvdb_id", "tmdb_id", "imdb_id", "title", "year", "overview", "poster", "runtime",
                               "status", "series_type")}
    if other:  # keep both versions on the same ids and settings
        for k in ("tvdb_id", "tmdb_id"):
            s[k] = s[k] or other[k]
        s["series_type"] = other["series_type"]
    if monitor == "same" and not other:
        monitor = "all"
    s.update(tier=tier, monitored=int(monitor != "none"), series_type=s.get("series_type") or "standard")
    s["folder"] = folder or (other["folder"] if other else None) or series_folder_name(s)
    s["id"] = db.insert("series", s)
    await refresh_series(s["id"], initial_monitor="all" if monitor == "same" else monitor)
    if monitor == "same":
        db.ex("UPDATE episodes SET monitored = COALESCE((SELECT o.monitored FROM episodes o WHERE o.series_id=? "
              "AND o.season=episodes.season AND o.episode=episodes.episode), monitored) WHERE series_id=?",
              (other["id"], s["id"]))
        db.update("series", s["id"], {"monitored": other["monitored"]})
    db.log(f"Added series {s['title']} [{TIER_NAMES[tier]}]")
    return s


async def add_series_tiers(tvdb_id: int | None, tier: str, monitor: str = "all",
                           tmdb_id: int | None = None) -> list[dict]:
    """Add every requested version that isn't in the library yet ('both' = 1080p and 4K)."""
    wanted = tiers_of(tier)
    todo = [t for t in wanted if not _find_series(tvdb_id, tmdb_id, t)]
    if not todo:
        title = _find_series(tvdb_id, tmdb_id)["title"]
        raise ValueError(f"{title} is already in your library in {' and '.join(TIER_NAMES[t] for t in wanted)}")
    return [await add_series(tvdb_id, t, monitor, tmdb_id=tmdb_id) for t in todo]


def _find_series(tvdb_id: int | None, tmdb_id: int | None, tier: str | None = None) -> dict | None:
    """A show by either id; with `tier`, only that version."""
    extra, params = (" AND tier=?", (tier,)) if tier else ("", ())
    if tvdb_id:
        row = db.one("SELECT * FROM series WHERE tvdb_id=?" + extra, (tvdb_id, *params))
        if row:
            return row
    if tmdb_id:
        return db.one("SELECT * FROM series WHERE tmdb_id=?" + extra, (tmdb_id, *params))
    return None


async def refresh_series(series_id: int, initial_monitor: str | None = None) -> None:
    s = db.one("SELECT * FROM series WHERE id=?", (series_id,))
    if not s:
        return
    try:
        d = await metadata.series_details(tvdb_id=s["tvdb_id"], tmdb_id=s["tmdb_id"])
        upd = {k: d.get(k) for k in ("title", "year", "overview", "poster", "imdb_id", "runtime", "status")}
        for k in ("tvdb_id", "tmdb_id"):  # fill in ids we learn, never blank out known ones
            if d.get(k) and not s[k] and not _find_series(d[k] if k == "tvdb_id" else None,
                                                           d[k] if k == "tmdb_id" else None, s["tier"]):
                upd[k] = d[k]
        db.update("series", series_id, upd)
    except Exception as e:  # noqa: BLE001
        db.log(f"Refreshing {s['title']} failed: {e}", "warn")
        return
    eps = await metadata.series_episodes({**s, **d})
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
            s = db.one("SELECT tier, profile FROM series WHERE id=?", (e["series_id"],))
            from .decision import profile_for
            prof = profiles.setdefault((s["tier"], s["profile"]), profile_for("tv", s["tier"], item=s))
            if not cutoff_met(prof, e["file_quality"], e["file_score"]):
                out.append(e)
    return out


def cutoff_met(profile, quality: str | None, score: int | None) -> bool:
    """Nothing left to search for. A file of a quality the profile doesn't allow counts when it is at least as good
    overall as the profile's cutoff (a 4K Remux in a "UHD Bluray + WEB" library is done)."""
    if not quality:
        return False
    if profile.quality_rank(quality) is None:
        from .decision import quality_order
        g_cur, g_cut = quality_order(quality), quality_order(profile.cutoff, profile)
        if g_cur is not None and g_cut is not None and g_cur <= g_cut:
            return True
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
    if any(v["tier"] == tier for v in versions(kind, item)):
        raise ValueError(f"{item['title']} already has a {TIER_NAMES[tier]} version")
    old = os.path.join(root_for(kind, item["tier"]), item["folder"])
    new = os.path.join(root_for(kind, tier), item["folder"])
    if os.path.normpath(old) != os.path.normpath(new) and os.path.isdir(old):  # shared root: nothing to move
        os.makedirs(os.path.dirname(new), exist_ok=True)
        shutil.move(old, new)
        if kind == "movie" and item["file_path"]:
            db.update("movies", item_id, {"file_path": item["file_path"].replace(old, new, 1)})
        else:
            for e in db.q("SELECT id, file_path FROM episodes WHERE series_id=? AND file_path IS NOT NULL", (item_id,)):
                db.update("episodes", e["id"], {"file_path": e["file_path"].replace(old, new, 1)})
    db.update(table, item_id, {"tier": tier})
    db.log(f"Moved {item['title']} to the {TIER_NAMES[tier]} library")


def set_christmas(movie_id: int, on: bool) -> dict:
    """Mark a movie (both versions) as a Christmas movie or not, moving its folder into or out of the
    Christmas folder."""
    m = db.one("SELECT * FROM movies WHERE id=?", (movie_id,))
    if not m:
        raise ValueError("No such movie")
    _move_movie(m, on, None, None)
    return db.one("SELECT * FROM movies WHERE id=?", (movie_id,))


def _move_movie(m: dict, on: bool, old_value: str | None, new_value: str | None) -> bool:
    """Move a movie's folder (shared by both versions) to where `on` and the Christmas setting put it, and update
    folder / file_path. old_value / new_value: the christmas_folder setting before / after (None = current)."""
    old_rel = m["folder"]
    old = _dir_for(old_rel, bool(m.get("christmas")), old_value)
    new_rel = _folder_for(_base_folder(old_rel, old_value), on, new_value)
    new = _dir_for(new_rel, on, new_value)
    moved = os.path.normpath(old) != os.path.normpath(new)
    if moved and os.path.exists(old):
        if os.path.exists(new):
            raise ValueError(f"{new} already exists")
        os.makedirs(os.path.dirname(new), exist_ok=True)
        shutil.move(old, new)
    for v in versions("movie", m):
        fp = v["file_path"]
        if fp and os.path.normpath(fp).startswith(os.path.normpath(old) + os.sep):
            fp = os.path.join(new, os.path.relpath(fp, old))
        db.update("movies", v["id"], {"christmas": int(on), "folder": new_rel, "file_path": fp})
    if moved:
        db.log(f"Moved {m['title']} to {os.path.dirname(new)}")
    return moved


def relocate_christmas(old_value: str, new_value: str) -> dict:
    """The Christmas folder setting changed: move every Christmas movie from the old place to the new one."""
    moved, errors, seen = 0, [], set()
    for m in db.q("SELECT * FROM movies WHERE christmas=1 ORDER BY id"):
        if m["tmdb_id"] in seen:
            continue
        seen.add(m["tmdb_id"])
        try:
            moved += _move_movie(m, True, old_value, new_value)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{m['title']}: {e}")
            db.log(f"Couldn't move {m['title']} to the new Christmas folder: {e}", "warn")
    if moved:
        db.log(f"Moved {moved} Christmas movie(s) to {christmas_root(new_value) or christmas_folder(new_value) or 'the movies folder'}")
    return {"moved": moved, "errors": errors}


def delete_version(kind: str, item_id: int, delete_files: bool = False) -> None:
    """Remove one version of a title. With `delete_files`, its files go too; when the other
    version shares the folder, only this version's own files are deleted."""
    table = "movies" if kind == "movie" else "series"
    item = db.one(f"SELECT * FROM {table} WHERE id=?", (item_id,))
    if not item:
        return
    if delete_files:
        folder = movie_dir(item) if kind == "movie" else series_dir(item)
        if shares_folder(kind, item):
            paths = [item["file_path"]] if kind == "movie" else [
                r["file_path"] for r in db.q("SELECT DISTINCT file_path FROM episodes WHERE series_id=? "
                                             "AND file_path IS NOT NULL", (item_id,))]
            for p in filter(None, paths):
                _delete_with_subs(p)
        elif os.path.isdir(folder):
            shutil.rmtree(folder, ignore_errors=True)
    if kind == "tv":
        db.ex("DELETE FROM episodes WHERE series_id=?", (item_id,))
    db.ex(f"DELETE FROM {table} WHERE id=?", (item_id,))
    db.log(f"Removed {item['title']} [{TIER_NAMES.get(item['tier'], item['tier'])}]")


def _delete_with_subs(video: str) -> None:
    base = os.path.splitext(video)[0]
    d = os.path.dirname(video)
    if not os.path.isdir(d):
        return
    for f in os.listdir(d):
        p = os.path.join(d, f)
        if p == video or (f.startswith(os.path.basename(base) + ".") and os.path.isfile(p)):
            try:
                os.remove(p)
            except OSError:
                pass
