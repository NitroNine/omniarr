"""Searching indexers, RSS sync, and sending the chosen release to NZBGet."""
from __future__ import annotations

import json
from collections import defaultdict

from . import db, decision, library
from .parser import normalize_title, parse
from .services import newznab, nzbget

GB = 1024 ** 3
_last_space_log = 0.0


class NoSpace(Exception):
    """The download disk can't hold this release on top of what's already queued."""


# Reasons to stop grabbing for now and leave the rest for a later search, without marking anything as searched.
HOLD = (NoSpace, newznab.IndexerBusy)


def _queued_bytes() -> int:
    rows = db.q("SELECT size, progress FROM downloads WHERE status IN ('queued','downloading')")
    return int(sum((r["size"] or 0) * (1 - (r["progress"] or 0) / 100) for r in rows))


async def check_space(size: int) -> None:
    """Refuse a grab the download disk can't finish.

    A Usenet download needs roughly twice its size while it's unpacked (the RAR parts and
    the unpacked file), so we want room for 2x everything still queued, 2x this release,
    plus the reserve from settings (`download_min_free_gb`)."""
    try:
        st = await nzbget.status()
    except nzbget.NZBGetError:
        return  # the grab itself will report the downloader problem
    free_mb = st.get("FreeDiskSpaceMB") if isinstance(st, dict) else None
    if not free_mb:
        return
    free = int(free_mb) * 1024 ** 2
    reserve = int(float(db.setting("download_min_free_gb") or 0) * GB)
    need = 2 * (_queued_bytes() + (size or 0)) + reserve
    if free < need:
        global _last_space_log
        import time
        msg = (f"Not enough space on the download disk: {free / GB:.0f} GB free, "
               f"{need / GB:.0f} GB needed for this release plus the queue")
        if time.time() - _last_space_log > 600:
            db.log(msg + "; grabbing paused until downloads finish.", "warn")
            _last_space_log = time.time()
        raise NoSpace(msg)


async def grab(d: decision.Decision, kind: str, movie_id: int | None = None, series_id: int | None = None) -> int:
    rel = d.release
    await check_space(rel.size)
    nzb = await newznab.fetch_nzb(rel.link)
    s = db.get_settings()
    cat = s["nzbget_category_movies"] if kind == "movie" else s["nzbget_category_tv"]
    dupe_key = f"omniarr-movie-{movie_id}" if kind == "movie" else f"omniarr-tv-{series_id}-{'-'.join(map(str, d.episode_ids))}"
    nzb_id = await nzbget.add(nzb, rel.title, cat, dupe_key=dupe_key, dupe_score=d.score)
    dl_id = db.insert("downloads", {
        "kind": kind, "movie_id": movie_id, "series_id": series_id,
        "episode_ids": json.dumps(d.episode_ids), "release_title": rel.title, "indexer": rel.indexer,
        "guid": rel.guid, "size": rel.size, "quality": d.quality, "score": d.score,
        "custom_formats": json.dumps(d.custom_formats), "nzbget_id": nzb_id, "status": "queued",
    })
    db.log(f"Grabbed {rel.title} [{d.quality}, score {d.score}] from {rel.indexer}")
    return dl_id


# ----------------------------------------------------------------- movies --
async def search_movie(movie_id: int, auto_grab: bool = True) -> list[dict]:
    movie = db.one("SELECT * FROM movies WHERE id=?", (movie_id,))
    if not movie:
        raise ValueError("Movie not found")
    releases = await newznab.search_movie(movie)
    decisions = decision.decide_movie(movie, releases)
    if auto_grab and decisions and decisions[0].accepted and not decision.active_download_for("movie", movie_id):
        await grab(decisions[0], "movie", movie_id=movie_id)  # NoSpace leaves last_search alone
        db.update("movies", movie_id, {"last_search": db.one("SELECT datetime('now') t")["t"]})
    else:
        db.update("movies", movie_id, {"last_search": db.one("SELECT datetime('now') t")["t"]})
        if auto_grab:
            db.log(f"Search for {movie['title']}: {len(releases)} results, none acceptable")
    return [d.to_dict() for d in decisions]


# ----------------------------------------------------------------- series --
async def search_series(series_id: int, season: int | None = None, episode_id: int | None = None,
                        auto_grab: bool = True) -> list[dict]:
    series = db.one("SELECT * FROM series WHERE id=?", (series_id,))
    if not series:
        raise ValueError("Series not found")
    if episode_id:
        wanted = db.q("SELECT * FROM episodes WHERE id=?", (episode_id,))
    else:
        wanted = library.wanted_episodes(series_id)
        if season is not None:
            wanted = [e for e in wanted if e["season"] == season]
    wanted = [e for e in wanted if not decision.active_download_for("tv", e["id"])]
    if not wanted:
        return []
    by_season: dict[int, list[dict]] = defaultdict(list)
    for e in wanted:
        by_season[e["season"]].append(e)
    all_decisions: list[decision.Decision] = []
    for sn, eps in by_season.items():
        total = db.one("SELECT COUNT(*) c FROM episodes WHERE series_id=? AND season=?", (series_id, sn))["c"]
        if episode_id is None and len(eps) > 1 and len(eps) >= total / 2:
            releases = await newznab.search_episodes(series, sn)  # season search (packs + singles)
        else:
            releases = []
            for e in eps[:25]:
                releases += await newznab.search_episodes(series, sn, e["episode"])
        decisions = decision.decide_episodes(series, eps, releases)
        all_decisions += decisions
        if auto_grab:
            await _grab_best_for_episodes(series, decisions)
    now = db.one("SELECT datetime('now') t")["t"]
    for e in wanted:
        db.update("episodes", e["id"], {"last_search": now})
    all_decisions.sort(key=lambda x: (not x.accepted, x.sort_key()))
    return [d.to_dict() for d in all_decisions]


async def _grab_best_for_episodes(series: dict, decisions: list[decision.Decision]) -> None:
    taken: set[int] = set()
    # prefer season packs when they're the best option, then fill in singles
    for d in sorted([d for d in decisions if d.accepted], key=lambda x: (x.sort_key(), -len(x.episode_ids))):
        if taken.intersection(d.episode_ids):
            continue
        if any(decision.active_download_for("tv", eid) for eid in d.episode_ids):
            continue
        try:
            await grab(d, "tv", series_id=series["id"])
            taken.update(d.episode_ids)
        except HOLD:
            raise
        except Exception as e:  # noqa: BLE001
            db.log(f"Grab failed for {d.release.title}: {e}", "warn")


# ------------------------------------------------------------------- RSS --
async def rss_sync() -> int:
    """Check the newest releases on every indexer against what we want."""
    try:
        return await _rss_sync()
    except HOLD:
        return 0


async def _rss_sync() -> int:
    grabbed = 0
    movies = [m for m in db.q("SELECT * FROM movies WHERE monitored=1") if library.movie_available(m)]
    wanted_movies = {}
    for m in movies:
        if m["file_path"]:
            from .decision import profile_for
            if library.cutoff_met(profile_for("movie", m["tier"]), m["file_quality"], m["file_score"]):
                continue
        for t in filter(None, (m["title"], m.get("original_title"))):
            wanted_movies.setdefault(normalize_title(t), []).append(m)
    if wanted_movies:
        rels = await newznab.rss("movie")
        buckets: dict[int, list] = defaultdict(list)
        for r in rels:
            for m in wanted_movies.get(normalize_title(parse(r.title).clean_name), []):
                buckets[m["id"]].append(r)
        for mid, rs in buckets.items():
            if decision.active_download_for("movie", mid):
                continue
            m = db.one("SELECT * FROM movies WHERE id=?", (mid,))
            ds = decision.decide_movie(m, rs)
            if ds and ds[0].accepted:
                await grab(ds[0], "movie", movie_id=mid)
                grabbed += 1

    wanted_eps = library.wanted_episodes()
    if wanted_eps:
        from .naming import strip_year
        series_ids = {e["series_id"] for e in wanted_eps}
        series = {s["id"]: s for s in db.q(f"SELECT * FROM series WHERE id IN ({','.join('?' * len(series_ids))})",
                                          tuple(series_ids))}
        names: dict[str, list[dict]] = defaultdict(list)  # a title can have a 1080p and a 4K version
        for s in series.values():
            for t in {s["title"], strip_year(s["title"]), f"{strip_year(s['title'])} {s.get('year') or ''}"}:
                names[normalize_title(t)].append(s)
        rels = await newznab.rss("tv")
        buckets = defaultdict(list)
        for r in rels:
            matches = names.get(normalize_title(parse(r.title).clean_name)) or []
            if not matches and r.tvdb_id:
                matches = [x for x in series.values() if x["tvdb_id"] == r.tvdb_id]
            for s in matches:
                buckets[s["id"]].append(r)
        for sid, rs in buckets.items():
            eps = [e for e in wanted_eps if e["series_id"] == sid and not decision.active_download_for("tv", e["id"])]
            if eps:
                ds = decision.decide_episodes(series[sid], eps, rs)
                before = db.one("SELECT COUNT(*) c FROM downloads")["c"]
                await _grab_best_for_episodes(series[sid], ds)
                grabbed += db.one("SELECT COUNT(*) c FROM downloads")["c"] - before
    return grabbed


async def missing_search(limit: int = 10) -> None:
    """Periodic backlog search, a few items at a time so indexer API limits are respected."""
    for m in db.q("SELECT * FROM movies WHERE monitored=1 AND (last_search IS NULL OR last_search < datetime('now','-1 day')) "
                  "ORDER BY last_search IS NOT NULL, last_search LIMIT ?", (limit * 3,)):
        if limit <= 0:
            break
        if not library.movie_available(m) or decision.active_download_for("movie", m["id"]):
            continue
        if m["file_path"]:
            if library.cutoff_met(decision.profile_for("movie", m["tier"]), m["file_quality"], m["file_score"]):
                continue
        try:
            await search_movie(m["id"])
        except HOLD:
            return
        except Exception as e:  # noqa: BLE001
            db.log(f"Search for {m['title']} failed: {e}", "warn")
        limit -= 1
    eps = [e for e in library.wanted_episodes()
           if not e.get("last_search") or e["last_search"] < db.one("SELECT datetime('now','-1 day') t")["t"]]
    seen: set = set()
    for e in eps:
        if limit <= 0:
            break
        key = (e["series_id"], e["season"])
        if key in seen:
            continue
        seen.add(key)
        try:
            await search_series(e["series_id"], season=e["season"])
        except HOLD:
            return
        except Exception as ex:  # noqa: BLE001
            db.log(f"Episode search failed: {ex}", "warn")
        limit -= 1
