"""Hallmark Christmas: every new Hallmark Christmas movie (this year on) found on TMDB's Hallmark services is
added to the library by itself, and downloaded once it has aired (the normal backlog search, which waits for
library.available_date: a Hallmark movie's TV premiere).

Movies this added are remembered (setting hallmark_seen), so one removed from the library isn't added again.
"""
from __future__ import annotations

import asyncio
import json
import time

from . import db, library
from .services import metadata

SYNC_HOURS = 6


async def providers() -> list[dict]:
    """The Hallmark services TMDB lists for the streaming region (Hallmark+, its Amazon / Apple TV channels...)."""
    return [p for p in await metadata._all_watch_providers()
            if "hallmark" in p["name"].lower() and "movie" in p["kinds"]]


async def candidates() -> list[dict]:
    """Hallmark Christmas movies from this year on: on Hallmark's streaming services, made by Hallmark, or airing on a
    Hallmark channel (upcoming premieres included), newest first."""
    found: dict[int, dict] = {}
    for p in await providers():
        try:
            data = await metadata.discover_christmas(p["id"], p["name"], "popular", False)  # incl. not aired yet
        except Exception as e:  # noqa: BLE001
            db.log(f"Hallmark Christmas: couldn't list {p['name']}: {e}", "warn")
            continue
        for r in data["results"]:
            found.setdefault(r["tmdb_id"], r)
    # not on Hallmark+ until they've aired: also Hallmark's own productions and anything airing on a Hallmark channel
    unmatched: list[dict] = []
    for what, fn in (("Hallmark's schedule on Wikipedia", metadata.hallmark_schedule),
                     ("Hallmark's productions", metadata.hallmark_productions),
                     ("Hallmark channel premieres", metadata.hallmark_premieres)):
        try:
            for r in await fn():
                if r["tmdb_id"] is None:  # announced, not on TMDB yet: shown, added once TMDB has it
                    unmatched.append(r)
                elif r["tmdb_id"] in found and fn is metadata.hallmark_schedule:
                    found[r["tmdb_id"]]["date"] = r["date"]  # Hallmark's premiere date wins
                else:
                    found.setdefault(r["tmdb_id"], r)
        except Exception as e:  # noqa: BLE001
            db.log(f"Hallmark Christmas: couldn't list {what}: {e}", "warn")
    return sorted([*found.values(), *unmatched], key=lambda r: r.get("date") or "", reverse=True)


def _seen() -> set[int]:
    try:
        return set(json.loads(db.setting("hallmark_seen") or "[]"))
    except (TypeError, ValueError):
        return set()


_lock = asyncio.Lock()


async def sync(force: bool = False) -> dict:
    """Add the Hallmark Christmas movies not seen before; search the ones that have already aired.
    One at a time: "Check now" and the scheduled job mustn't add the same movie twice."""
    if not force and not db.setting("hallmark_auto"):
        return {"added": []}
    async with _lock:
        return await _sync()


async def _sync() -> dict:
    tier = db.setting("hallmark_tier") or "hd"
    seen = _seen()
    have = {r["tmdb_id"] for r in db.q("SELECT tmdb_id FROM movies")}
    added: list[dict] = []
    for r in await candidates():
        tid = r["tmdb_id"]
        if tid is None or tid in seen:
            continue
        if tid not in have:
            try:
                added += await library.add_movie_tiers(tid, tier, christmas=True)
            except Exception as e:  # noqa: BLE001
                db.log(f"Hallmark Christmas: couldn't add {r.get('title')}: {e}", "warn")
                continue
        seen.add(tid)
    db.save_settings({"hallmark_seen": json.dumps(sorted(seen)), "hallmark_last_sync": time.strftime("%Y-%m-%d %H:%M")})
    if added:
        db.log(f"Hallmark Christmas: added {', '.join(m['title'] for m in added)}")
        from .collections_ import _search_added
        asyncio.create_task(_search_added(added))  # only the ones already aired; the rest wait for their premiere
    return {"added": [m["title"] for m in added]}


_last = 0.0


async def sync_due() -> None:
    """Scheduler job (runs hourly): sync every SYNC_HOURS while auto-add is on."""
    global _last
    if db.setting("hallmark_auto") and time.time() - _last >= SYNC_HOURS * 3600:
        _last = time.time()
        await sync()


async def page() -> dict:
    """The Hallmark tab: the movies, with what the library has of each."""
    rows = await candidates()
    lib: dict[int, list[dict]] = {}
    for m in db.q("SELECT id, tmdb_id, tier, file_path, monitored, release_date, digital_release, physical_release "
                  "FROM movies"):
        lib.setdefault(m["tmdb_id"], []).append(m)
    seen = _seen()
    for r in rows:
        vs = lib.get(r["tmdb_id"], [])
        r["removed"] = not vs and r["tmdb_id"] in seen  # added once, then removed: auto-add leaves it out
        r["library_id"] = vs[0]["id"] if vs else None
        r["has_file"] = any(v["file_path"] for v in vs)
        r["tiers"] = [v["tier"] for v in vs]
        r["airs"] = library.available_date(vs[0]) if vs else r.get("date")
    s = db.get_settings()
    return {"auto": bool(s.get("hallmark_auto")), "tier": s.get("hallmark_tier") or "hd",
            "last_sync": s.get("hallmark_last_sync"), "services": [p["name"] for p in await providers()],
            "movies": rows}
