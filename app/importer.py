"""Tracks NZBGet downloads and imports finished ones into the library with
TRaSH naming. Failed downloads are blocklisted and a new search is started."""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil

from . import db, library, naming, trash
from .parser import parse
from .services import downloader, mediainfo, nzbget

VIDEO_EXT = {".mkv", ".mp4", ".avi", ".m4v", ".ts", ".m2ts", ".wmv", ".mov", ".webm"}
SUB_EXT = {".srt", ".ass", ".ssa", ".sub", ".idx", ".sup", ".vtt"}


def map_path(path: str) -> str:
    s = db.get_settings()
    src, dst = s.get("remote_path_from") or "", s.get("remote_path_to") or ""
    if src and path.startswith(src):
        return dst.rstrip("/") + "/" + path[len(src):].lstrip("/")
    return path


def video_files(root: str) -> list[str]:
    if os.path.isfile(root):
        return [root]
    out = []
    for dp, _dn, fns in os.walk(root):
        for f in fns:
            ext = os.path.splitext(f)[1].lower()
            if ext in VIDEO_EXT and not re.search(r"\bsample\b", f, re.I) and "/sample" not in dp.lower():
                out.append(os.path.join(dp, f))
    return out


def _transfer(src: str, dst: str) -> None:
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    mode = db.setting("import_mode")
    if os.path.exists(dst):
        os.remove(dst)
    if mode == "hardlink":
        try:
            os.link(src, dst)
            return
        except OSError:
            pass  # different filesystem -> fall back to copy
        shutil.copy2(src, dst)
    elif mode == "copy":
        shutil.copy2(src, dst)
    else:
        shutil.move(src, dst)


def subtitles_for(video: str) -> list[str]:
    base = os.path.splitext(os.path.basename(video))[0]
    d = os.path.dirname(video)
    out = []
    for dp, _dn, fns in os.walk(d):
        for f in fns:
            if os.path.splitext(f)[1].lower() in SUB_EXT and (f.startswith(base) or len(video_files(d)) == 1):
                out.append(os.path.join(dp, f))
    return out


def sub_suffix(sub: str, video: str) -> str:
    """Keep language/forced tags: 'movie.en.forced.srt' -> '.en.forced.srt'."""
    stem = os.path.splitext(os.path.basename(sub))[0]
    vbase = os.path.splitext(os.path.basename(video))[0]
    rest = stem[len(vbase):] if stem.startswith(vbase) else stem
    tags = [p for p in re.split(r"[._ -]", rest) if re.fullmatch(r"[a-z]{2,3}|forced|sdh|cc|default", p, re.I)]
    return ("." + ".".join(tags) if tags else "") + os.path.splitext(sub)[1].lower()


def _remove_old(path: str | None) -> None:
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError as e:
            db.log(f"Could not remove old file {path}: {e}", "warn")


def _rename_cfs(kind: str, cf_names: list[str]) -> list[str]:
    store = trash.ensure_loaded()
    app = "radarr" if kind == "movie" else "sonarr"
    keep = {cf.name for cf in store.cfs[app].values() if cf.include_when_renaming}
    return [n for n in cf_names if n in keep]


def _avoid_sibling(dest: str, taken: set[str], tier: str) -> str:
    """Both versions share a folder: never overwrite the other version's file. TRaSH names
    include the quality so this rarely triggers; when it does, tag the name Plex-style."""
    if dest not in taken:
        return dest
    base, ext = os.path.splitext(dest)
    return f"{base} - {library.TIER_NAMES[tier]}{ext}"


def _sibling_episode_files(series: dict) -> set[str]:
    sib = library.shares_folder("tv", series)
    if not sib:
        return set()
    return {r["file_path"] for r in db.q("SELECT file_path FROM episodes WHERE series_id=? AND file_path IS NOT NULL",
                                          (sib["id"],))}


def match_episode_file(path: str, all_by_se: dict) -> list[dict]:
    """Map a video file to episodes using SxxEyy / 1x01 / air date in its name,
    falling back to a 'Season NN' parent folder plus an 'E01'/'Episode 1' file name."""
    p = parse(os.path.basename(path))
    if p.season is not None and p.episodes:
        return [all_by_se[(p.season, n)] for n in p.episodes if (p.season, n) in all_by_se]
    if p.air_date:
        return [e for e in all_by_se.values() if e["air_date"] == p.air_date]
    m = re.search(r"(?:season|series|s)[ ._-]?(\d{1,3})$", os.path.basename(os.path.dirname(path)), re.I)
    e = re.search(r"(?:^|[ ._-])(?:e|ep|episode)[ ._-]?(\d{1,4})(?:[ ._-]|$)", os.path.splitext(os.path.basename(path))[0], re.I)
    if m and e and (int(m.group(1)), int(e.group(1))) in all_by_se:
        return [all_by_se[(int(m.group(1)), int(e.group(1)))]]
    return []


def movie_file_name(movie: dict, rel, quality: str, cf_names: list[str], mi: dict | None) -> str:
    """File name (no extension) per the TRaSH movie naming scheme selected in settings."""
    fmt = trash.ensure_loaded().naming_format("movie", "file", db.setting("naming_movie_file")) \
        or "{Movie CleanTitle} {(Release Year)} {[Quality Full]}{-Release Group}"
    values = {**naming.movie_values(movie), **naming.media_values(rel, quality, _rename_cfs("movie", cf_names), mi)}
    return naming.safe(naming.render(fmt, values))


def episode_file_name(series: dict, episodes: list[dict], rel, quality: str, cf_names: list[str], mi: dict | None) -> str:
    style = "anime" if series["series_type"] == "anime" else "daily" if rel.air_date else "standard"
    fmt = trash.ensure_loaded().naming_format("tv", style, db.setting("naming_episode_file")) \
        or "{Series TitleYear} - S{season:00}E{episode:00} - {Episode CleanTitle} {[Quality Full]}{-Release Group}"
    values = {**naming.series_values(series), **naming.episode_values(episodes),
              **naming.media_values(rel, quality, _rename_cfs("tv", cf_names), mi)}
    return naming.safe(naming.render_episode(fmt, values))


async def import_movie(dl: dict, source_dir: str) -> None:
    movie = db.one("SELECT * FROM movies WHERE id=?", (dl["movie_id"],))
    files = video_files(source_dir)
    if not files:
        raise RuntimeError(f"No video files found in {source_dir}")
    video = max(files, key=os.path.getsize)
    rel = parse(dl["release_title"])
    mi = await mediainfo.probe(video)
    name = movie_file_name(movie, rel, dl["quality"], json.loads(dl["custom_formats"] or "[]"), mi)
    dest_dir = library.movie_dir(movie)
    dest = os.path.join(dest_dir, name + os.path.splitext(video)[1].lower())
    sib = library.shares_folder("movie", movie)
    dest = _avoid_sibling(dest, {sib["file_path"]} if sib and sib["file_path"] else set(), movie["tier"])
    name = os.path.splitext(os.path.basename(dest))[0]
    subs = subtitles_for(video)
    if movie["file_path"] and movie["file_path"] != dest:
        _remove_old(movie["file_path"])
    await asyncio.to_thread(_transfer, video, dest)
    for sub in subs:
        await asyncio.to_thread(_transfer, sub, os.path.join(dest_dir, name + sub_suffix(sub, video)))
    db.update("movies", movie["id"], {"file_path": dest, "file_quality": dl["quality"], "file_score": dl["score"],
                                      "file_size": os.path.getsize(dest)})
    db.log(f"Imported {movie['title']} → {dest}")


async def import_episodes(dl: dict, source_dir: str) -> None:
    series = db.one("SELECT * FROM series WHERE id=?", (dl["series_id"],))
    ep_ids = json.loads(dl["episode_ids"] or "[]")
    eps = db.q(f"SELECT * FROM episodes WHERE id IN ({','.join('?' * len(ep_ids))})", tuple(ep_ids)) if ep_ids else []
    files = video_files(source_dir)
    if not files:
        raise RuntimeError(f"No video files found in {source_dir}")
    all_by_se = {(e["season"], e["episode"]): e
                 for e in db.q("SELECT * FROM episodes WHERE series_id=?", (series["id"],))}
    plan: list[tuple[str, list[dict]]] = []
    if len(files) == 1 and len(eps) >= 1 and not parse(dl["release_title"]).full_season:
        plan.append((files[0], eps))
    else:
        for f in files:
            matched = match_episode_file(f, all_by_se)
            if matched:
                plan.append((f, matched))
            else:
                db.log(f"Skipped unrecognised file {os.path.basename(f)}", "warn")
    if not plan:
        raise RuntimeError("Could not match any downloaded file to an episode")
    rel = parse(dl["release_title"])
    cfs = json.loads(dl["custom_formats"] or "[]")
    sibling_files = _sibling_episode_files(series)
    imported = 0
    for video, episodes in plan:
        mi = await mediainfo.probe(video)
        name = episode_file_name(series, episodes, rel, dl["quality"], cfs, mi)
        dest_dir = os.path.join(library.series_dir(series), library.season_folder_name(episodes[0]["season"]))
        dest = os.path.join(dest_dir, name + os.path.splitext(video)[1].lower())
        dest = _avoid_sibling(dest, sibling_files, series["tier"])
        name = os.path.splitext(os.path.basename(dest))[0]
        subs = subtitles_for(video) if len(plan) == 1 else []
        for e in episodes:
            if e["file_path"] and e["file_path"] != dest:
                _remove_old(e["file_path"])
        await asyncio.to_thread(_transfer, video, dest)
        for sub in subs:
            await asyncio.to_thread(_transfer, sub, os.path.join(dest_dir, name + sub_suffix(sub, video)))
        size = os.path.getsize(dest)
        for e in episodes:
            db.update("episodes", e["id"], {"file_path": dest, "file_quality": dl["quality"],
                                            "file_score": dl["score"], "file_size": size})
        imported += 1
    db.log(f"Imported {imported} file(s) for {series['title']}")


async def _cleanup_source(path: str) -> None:
    if db.setting("import_mode") == "move" and os.path.isdir(path):
        if not video_files(path):
            shutil.rmtree(path, ignore_errors=True)


async def process_downloads() -> None:
    active = db.q("SELECT * FROM downloads WHERE status IN ('queued','downloading','importing')")
    if not active:
        return
    try:
        queue = await nzbget.queue()
        hist = await nzbget.history()
    except nzbget.NZBGetError as e:
        db.log(str(e), "warn")
        return
    free_mb: float | None = None
    for dl in active:
        nid = dl["nzbget_id"]
        if nid in queue:
            g = queue[nid]
            total = g.get("FileSizeMB") or 0
            remaining = g.get("RemainingSizeMB") or 0
            prog = 100 * (1 - remaining / total) if total else 0
            status = "downloading" if g.get("Status", "").startswith(("DOWNLOADING", "FETCHING")) or prog > 0 else "queued"
            db.ex("UPDATE downloads SET status=?, progress=?, message=?, updated=datetime('now') WHERE id=?",
                  (status, round(prog, 1), g.get("Status"), dl["id"]))
            continue
        h = hist.get(nid)
        if not h:
            continue
        st = h.get("Status", "")
        if st.startswith("SUCCESS"):
            src = map_path(h.get("FinalDir") or h.get("DestDir") or "")
            db.ex("UPDATE downloads SET status='importing', progress=100, dest_dir=? WHERE id=?", (src, dl["id"]))
            try:
                if dl["kind"] == "movie":
                    await import_movie(dl, src)
                else:
                    await import_episodes(dl, src)
                await _cleanup_source(src)
                db.ex("UPDATE downloads SET status='imported', message=NULL, updated=datetime('now') WHERE id=?", (dl["id"],))
            except Exception as e:  # noqa: BLE001
                db.ex("UPDATE downloads SET status='import_failed', message=?, updated=datetime('now') WHERE id=?",
                      (str(e), dl["id"]))
                db.log(f"Import failed for {dl['release_title']}: {e}", "error")
        elif st.startswith(("FAILURE", "WARNING", "DELETED")):
            if free_mb is None:
                free_mb = await _free_mb()
            if _out_of_space(st, free_mb):
                await space_failure(dl, st, h)
            else:
                await fail_download(dl, st, research=not st.startswith("DELETED/MANUAL"))


SPACE_STATUSES = ("FAILURE/MOVE", "WARNING/SPACE", "FAILURE/SPACE")


async def _free_mb() -> float:
    try:
        st = await nzbget.status()
        return float(st.get("FreeDiskSpaceMB")) if isinstance(st, dict) and st.get("FreeDiskSpaceMB") is not None else -1
    except (nzbget.NZBGetError, TypeError, ValueError):
        return -1


def _out_of_space(status: str, free_mb: float) -> bool:
    """A failure caused by a full download disk says nothing about the release, so it mustn't be blocklisted."""
    if status.startswith(SPACE_STATUSES):
        return True
    if status.startswith("DELETED/MANUAL") or free_mb < 0:
        return False
    reserve_mb = float(db.setting("download_min_free_gb") or 0) * 1024
    return free_mb < max(2048.0, reserve_mb / 2)


async def space_failure(dl: dict, reason: str, hist: dict | None = None) -> None:
    """Disk-full failure: drop the half-finished files, don't blocklist, and let the backlog search try again."""
    db.ex("UPDATE downloads SET status='failed', message=?, updated=datetime('now') WHERE id=?",
          (f"Download disk ran out of space ({reason}); not blocklisted, will be searched again", dl["id"]))
    db.log(f"Download failed because the disk was full ({reason}): {dl['release_title']}. Not blocklisted.", "warn")
    path = map_path((hist or {}).get("DestDir") or "")
    inter = os.path.realpath(os.path.join(downloader.download_dir(), "intermediate"))
    if path and os.path.realpath(path).startswith(inter + os.sep) and os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)
    try:
        await nzbget.delete(dl["nzbget_id"], from_history=True)
    except nzbget.NZBGetError:
        pass
    if dl["kind"] == "movie" and dl["movie_id"]:
        db.ex("UPDATE movies SET last_search=NULL WHERE id=?", (dl["movie_id"],))
    for eid in json.loads(dl["episode_ids"] or "[]"):
        db.ex("UPDATE episodes SET last_search=NULL WHERE id=?", (eid,))


async def fail_download(dl: dict, reason: str, research: bool = True) -> None:
    db.ex("UPDATE downloads SET status='failed', message=?, updated=datetime('now') WHERE id=?", (reason, dl["id"]))
    db.insert("blocklist", {"release_title": dl["release_title"], "guid": dl["guid"], "reason": reason})
    db.log(f"Download failed ({reason}): {dl['release_title']} — blocklisted", "warn")
    if not research:
        return
    from . import search
    try:
        if dl["kind"] == "movie":
            await search.search_movie(dl["movie_id"])
        else:
            await search.search_series(dl["series_id"])
    except Exception as e:  # noqa: BLE001
        db.log(f"Re-search after failure errored: {e}", "warn")


async def retry_import(dl_id: int) -> None:
    dl = db.one("SELECT * FROM downloads WHERE id=?", (dl_id,))
    if not dl or not dl["dest_dir"]:
        raise ValueError("Nothing to retry")
    if dl["kind"] == "movie":
        await import_movie(dl, dl["dest_dir"])
    else:
        await import_episodes(dl, dl["dest_dir"])
    db.ex("UPDATE downloads SET status='imported', message=NULL WHERE id=?", (dl_id,))

