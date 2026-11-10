"""Library import: bring media that already sits in the root folders into Omniarr.

Flow: scan root folders -> match each folder to TMDB (IDs in the
folder name win, then title + year search) -> import: create the movie/series
using the existing folder, then detect the files already there (quality,
TRaSH custom-format score), optionally renaming them to TRaSH naming.
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil

from . import db, decision, importer, library, trash
from .parser import normalize_title, parse
from .services import mediainfo, metadata

_ID = re.compile(r"[\[{(]\s*(tmdb|tvdb|imdb)(?:id)?[-=: ]\s*(tt\d+|\d+)\s*[\]})]", re.I)
_EDITION = re.compile(r"\{edition-[^}]*\}", re.I)
_YEAR_PAREN = re.compile(r"\((19\d{2}|20\d{2})\)")
_YEAR_END = re.compile(r"[ ._-](19\d{2}|20\d{2})$")
_NOISE = re.compile(r"[\[(][^\])]*[\])]")


def parse_folder(name: str) -> dict:
    """'Dune Part Two (2024) {tmdb-693134}' -> title/year/ids."""
    ids = {k.lower(): v for k, v in _ID.findall(name)}
    rest = _EDITION.sub("", _ID.sub("", name)).strip()
    year = None
    m = _YEAR_PAREN.search(rest)
    if m:
        year = int(m.group(1))
        title = rest[:m.start()]
    else:
        m = _YEAR_END.search(rest)
        if m:
            year, title = int(m.group(1)), rest[:m.start()]
        else:
            p = parse(rest)  # scene-style folder name "Movie.Name.2019.1080p.BluRay-GRP"
            if p.resolution or p.source != "unknown":
                title, year = (p.clean_name or rest), p.year
            else:
                title = rest
    title = re.sub(r"[._]", " ", title)
    title = re.sub(r"\s+", " ", title).strip(" -")
    if not title:
        title = re.sub(r"\s+", " ", _NOISE.sub("", rest)).strip()
    return {
        "title": title, "year": year,
        "tmdb_id": int(ids["tmdb"]) if ids.get("tmdb", "").isdigit() else None,
        "tvdb_id": int(ids["tvdb"]) if ids.get("tvdb", "").isdigit() else None,
        "imdb_id": ids.get("imdb") if str(ids.get("imdb", "")).startswith("tt") else None,
    }


def _dir_stats(path: str) -> tuple[int, int]:
    files = importer.video_files(path)
    return len(files), sum(os.path.getsize(f) for f in files if os.path.exists(f))


def scan(kind: str) -> list[dict]:
    """List folders (and loose video files) in the movies or TV folder that aren't in the
    library yet. Their tier is 'auto': the versions (1080p / 4K) come from the files at import."""
    table = "movies" if kind == "movie" else "series"
    known = {r["folder"] for r in db.q(f"SELECT folder FROM {table}")}
    root = library.root_for(kind)
    roots = {os.path.normpath(root): ["auto"]} if root and os.path.isdir(root) else {}
    out = []
    for root, tiers in roots.items():
        tier = tiers[0]
        for entry in sorted(os.listdir(root), key=str.lower):
            if entry.startswith((".", "@")) or entry in known:
                continue
            path = os.path.join(root, entry)
            loose = os.path.isfile(path)
            if loose and os.path.splitext(entry)[1].lower() not in importer.VIDEO_EXT:
                continue
            if not loose and not os.path.isdir(path):
                continue
            n, size = (1, os.path.getsize(path)) if loose else _dir_stats(path)
            info = parse_folder(os.path.splitext(entry)[0] if loose else entry)
            out.append({"kind": kind, "tier": tier, "folder": entry, "path": path, "loose": loose,
                        "files": n, "size": size, **info})
    return out


async def _best(results: list[dict], title: str, year: int | None) -> tuple[dict | None, str]:
    if not results:
        return None, "none"
    nt = normalize_title(title)
    for r in results:
        if normalize_title(r["title"]) == nt and (not year or not r.get("year") or abs(r["year"] - year) <= 1):
            return r, "exact"
    if year:
        for r in results:
            if r.get("year") and abs(r["year"] - year) <= 1:
                return r, "guess"
    return results[0], "guess"


async def match(c: dict) -> dict:
    """Attach {'match': {...}, 'confidence': id|exact|guess|none} to a scan candidate."""
    m, conf = None, "none"
    try:
        if c["kind"] == "movie":
            if c.get("tmdb_id"):
                m, conf = await metadata.movie_details(c["tmdb_id"]), "id"
            elif c.get("imdb_id"):
                m = await metadata.movie_by_imdb(c["imdb_id"])
                conf = "id" if m else "none"
            if not m:
                term = f"{c['title']} {c['year']}" if c.get("year") else c["title"]
                res = await metadata.search_movies(term)
                if not res and c.get("year"):
                    res = await metadata.search_movies(c["title"])
                m, conf = await _best(res, c["title"], c.get("year"))
            if m:
                m = {k: m.get(k) for k in ("tmdb_id", "title", "year", "poster")}
                m["in_library"] = bool(db.one("SELECT 1 FROM movies WHERE tmdb_id=?", (m["tmdb_id"],)))
        else:
            if c.get("tvdb_id") or c.get("tmdb_id"):
                m, conf = await metadata.series_details(tvdb_id=c.get("tvdb_id"), tmdb_id=c.get("tmdb_id")), "id"
            elif c.get("imdb_id"):
                m = await metadata.series_by_remote_id(c["imdb_id"])
                conf = "id" if m else "none"
            if not m:
                res = await metadata.search_series(c["title"])
                m, conf = await _best(res, c["title"], c.get("year"))
            if m:
                m = {k: m.get(k) for k in ("tvdb_id", "tmdb_id", "title", "year", "poster")}
                m["in_library"] = bool(library._find_series(m.get("tvdb_id"), m.get("tmdb_id")))
    except Exception as e:  # noqa: BLE001
        return {**c, "match": None, "confidence": "none", "error": str(e)}
    return {**c, "match": m, "confidence": conf if m else "none"}


async def match_many(items: list[dict]) -> list[dict]:
    sem = asyncio.Semaphore(6)

    async def one(c):
        async with sem:
            return await match(c)
    return await asyncio.gather(*(one(c) for c in items))


# ------------------------------------------------------------ file detection --
async def _parse_file(path: str):
    """Parse a file name like a release title, filling gaps from its folder name and ffprobe."""
    rel = parse(os.path.splitext(os.path.basename(path))[0])
    if not rel.resolution or rel.source == "unknown":
        parent = parse(os.path.basename(os.path.dirname(path)))
        if not rel.resolution and parent.resolution:
            rel.resolution = parent.resolution
        if rel.source == "unknown" and parent.source != "unknown":
            rel.source, rel.modifier = parent.source, parent.modifier
    if not rel.resolution:
        mi = await mediainfo.probe(path)
        rel.resolution = mi.get("resolution") or 0
    return rel


async def file_tier(path: str) -> str:
    """Which version a file on disk belongs to: 'uhd' for 2160p, otherwise 'hd'."""
    return "uhd" if ((await _parse_file(path)).resolution or 0) >= 2160 else "hd"


async def _own_files(kind: str, item: dict, files: list[str]) -> list[str]:
    """When both versions share a folder, each version only takes the files of its tier."""
    if not library.shares_folder(kind, item):
        return files
    return [f for f in files if await file_tier(f) == item["tier"]]


async def tiers_in(path: str) -> list[str]:
    """The versions found in a folder (or loose file): ['hd'], ['uhd'] or both."""
    files = importer.video_files(path) if os.path.exists(path) else []
    found = {await file_tier(f) for f in files}
    return [t for t in library.TIERS if t in found] or ["hd"]


async def detect(path: str, kind: str, profile: trash.Profile) -> tuple[str, int, list[str]]:
    """Work out quality + TRaSH score for a file already on disk.
    Uses the file name (and its folder name) like a release title; ffprobe
    fills in the resolution when the name doesn't say."""
    rel = await _parse_file(path)
    if rel.source == "unknown" and rel.resolution:
        rel.source = "dvd" if rel.resolution <= 576 else "webdl"
    if rel.source == "bluray" and not rel.resolution:
        rel.resolution = 1080
    quality = rel.quality_name(kind)
    score, cfs = trash.score_release(profile, rel)
    return quality, score, cfs


async def _rename_into_place(src: str, dest: str) -> str:
    if os.path.abspath(src) == os.path.abspath(dest):
        return src
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if os.path.exists(dest):
        return src  # never overwrite another file
    subs = importer.subtitles_for(src)
    shutil.move(src, dest)
    base = os.path.splitext(dest)[0]
    for s in subs:
        if os.path.exists(s):
            target = base + importer.sub_suffix(s, src)
            if not os.path.exists(target):
                shutil.move(s, target)
    return dest


async def rescan_movie(movie_id: int, rename: bool = False) -> dict:
    movie = db.one("SELECT * FROM movies WHERE id=?", (movie_id,))
    folder = library.movie_dir(movie)
    files = importer.video_files(folder) if os.path.isdir(folder) else []
    files = await _own_files("movie", movie, files)
    if not files:
        db.update("movies", movie_id, {"file_path": None, "file_quality": None, "file_score": None, "file_size": None})
        return {"files": 0}
    video = max(files, key=os.path.getsize)
    profile = decision.profile_for("movie", movie["tier"])
    quality, score, cfs = await detect(video, "movie", profile)
    if rename:
        rel = parse(os.path.splitext(os.path.basename(video))[0])
        mi = await mediainfo.probe(video)
        name = importer.movie_file_name(movie, rel, quality, cfs, mi)
        video = await _rename_into_place(video, os.path.join(folder, name + os.path.splitext(video)[1].lower()))
    db.update("movies", movie_id, {"file_path": video, "file_quality": quality, "file_score": score,
                                   "file_size": os.path.getsize(video)})
    return {"files": 1, "quality": quality, "score": score}


async def rescan_series(series_id: int, rename: bool = False) -> dict:
    series = db.one("SELECT * FROM series WHERE id=?", (series_id,))
    root = library.series_dir(series)
    eps = db.q("SELECT * FROM episodes WHERE series_id=?", (series_id,))
    by_se = {(e["season"], e["episode"]): e for e in eps}
    found: dict[int, str] = {}  # episode id -> file
    groups: dict[str, list[dict]] = {}
    for f in await _own_files("tv", series, importer.video_files(root) if os.path.isdir(root) else []):
        matched = importer.match_episode_file(f, by_se)
        if not matched:
            continue
        for e in matched:
            prev = found.get(e["id"])
            if prev is None or os.path.getsize(f) > os.path.getsize(prev):
                found[e["id"]] = f
        groups[f] = matched
    profile = decision.profile_for("tv", series["tier"])
    with_files = 0
    for f, episodes in groups.items():
        episodes = [e for e in episodes if found.get(e["id"]) == f]
        if not episodes:
            continue
        quality, score, cfs = await detect(f, "tv", profile)
        path = f
        if rename:
            rel = parse(os.path.splitext(os.path.basename(f))[0])
            mi = await mediainfo.probe(f)
            name = importer.episode_file_name(series, episodes, rel, quality, cfs, mi)
            dest_dir = os.path.join(root, library.season_folder_name(episodes[0]["season"]))
            path = await _rename_into_place(f, os.path.join(dest_dir, name + os.path.splitext(f)[1].lower()))
        size = os.path.getsize(path)
        for e in episodes:
            db.update("episodes", e["id"], {"file_path": path, "file_quality": quality, "file_score": score,
                                            "file_size": size})
            with_files += 1
    # episodes whose files disappeared
    for e in eps:
        if e["file_path"] and e["id"] not in found and not os.path.exists(e["file_path"]):
            db.update("episodes", e["id"], {"file_path": None, "file_quality": None, "file_score": None, "file_size": None})
    if rename and os.path.isdir(root):
        _remove_empty_dirs(root)
    return {"files": len(groups), "episodes": with_files}


def _remove_empty_dirs(root: str) -> None:
    for dp, dn, fn in sorted(os.walk(root), key=lambda x: -len(x[0])):
        if dp != root and not dn and not fn:
            try:
                os.rmdir(dp)
            except OSError:
                pass


# --------------------------------------------------------------- import job --
JOB: dict = {"running": False, "total": 0, "done": 0, "imported": 0, "errors": [], "current": None}


async def import_one(item: dict) -> dict:
    """item: kind, tier (hd | uhd | both | auto), folder, loose, id (movie: tmdb) /
    tvdb_id + tmdb_id (tv), monitor, search, rename. 'auto' adds a version for each
    quality found in the folder (shared 1080p/4K root folder)."""
    kind, tier, folder = item["kind"], item["tier"], item["folder"]
    rename = bool(item.get("rename"))
    src_root = library.root_for(kind)
    tiers = await tiers_in(os.path.join(src_root, folder)) if tier == "auto" else library.tiers_of(tier)
    from . import search
    if kind == "movie":
        loose = bool(item.get("loose"))
        tmdb_id = int(item["id"])
        have = {r["tier"] for r in db.q("SELECT tier FROM movies WHERE tmdb_id=?", (tmdb_id,))}
        first = None
        for t in [x for x in tiers if x not in have]:
            m = await library.add_movie(tmdb_id, t, item.get("monitor", "all") != "none",
                                        folder=None if loose else folder)
            if loose and first is None:  # a video file sitting directly in the root: give it a folder
                src = os.path.join(src_root, folder)
                dest_dir = library.movie_dir(m)
                os.makedirs(dest_dir, exist_ok=True)
                shutil.move(src, os.path.join(dest_dir, os.path.basename(src)))
            first = first or m
        if first is None:
            raise ValueError("Already in your library")
        for m in db.q("SELECT * FROM movies WHERE tmdb_id=?", (tmdb_id,)):
            res = await rescan_movie(m["id"], rename=rename or loose)
            movie = db.one("SELECT * FROM movies WHERE id=?", (m["id"],))
            if item.get("search") and movie["monitored"] and library.movie_available(movie) and not library.cutoff_met(
                    decision.profile_for("movie", movie["tier"]), movie["file_quality"], movie["file_score"]):
                asyncio.create_task(search.search_movie(m["id"]))
            db.log(f"Library import: {m['title']} [{library.TIER_NAMES[m['tier']]}] ({res.get('quality') or 'no file'})")
        return first
    monitor = item.get("monitor", "all")
    tvdb_id = item.get("tvdb_id") or (None if item.get("tmdb_id") else item.get("id"))
    tvdb_id = int(tvdb_id) if tvdb_id else None
    added = []
    for t in tiers:
        if library._find_series(tvdb_id, item.get("tmdb_id"), t):
            continue
        added.append(await library.add_series(tvdb_id, t, "all" if monitor == "existing" else monitor,
                                              folder=folder, tmdb_id=item.get("tmdb_id")))
    if not added:
        raise ValueError("Already in your library")
    for s in added:  # rescan after every version exists, so each one only takes its own files
        res = await rescan_series(s["id"], rename=rename)
        if monitor == "existing":
            db.ex("UPDATE episodes SET monitored = (file_path IS NOT NULL) WHERE series_id=?", (s["id"],))
        if item.get("search") and monitor != "none":
            asyncio.create_task(search.search_series(s["id"]))
        db.log(f"Library import: {s['title']} [{library.TIER_NAMES[s['tier']]}] ({res['episodes']} episodes with files)")
    return added[0]


async def run_import(items: list[dict]) -> None:
    JOB.update(running=True, total=len(items), done=0, imported=0, errors=[], current=None)
    try:
        for it in items:
            JOB["current"] = it.get("folder")
            try:
                await import_one(it)
                JOB["imported"] += 1
            except Exception as e:  # noqa: BLE001
                JOB["errors"].append(f"{it.get('folder')}: {e}")
                db.log(f"Library import failed for {it.get('folder')}: {e}", "warn")
            JOB["done"] += 1
    finally:
        JOB.update(running=False, current=None)
