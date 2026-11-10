"""Movie collections and universes.

A collection is either a TMDB collection (e.g. "The Lord of the Rings
Collection") or a TMDB keyword used as a universe (e.g. "marvel cinematic
universe (mcu)"). Adding one adds its movies to a chosen library (1080p, 4K or both).
A monitored collection is re-checked daily, and new entries (sequels announced
later) are added automatically. Movies you untick when adding are remembered as
excluded and never added later.
"""
from __future__ import annotations

import asyncio
import json

from . import db, library
from .services import metadata


def _with_library_state(info: dict) -> dict:
    have: dict[int, list[dict]] = {}
    for r in db.q("SELECT id, tmdb_id, tier, file_path FROM movies ORDER BY tier"):
        have.setdefault(r["tmdb_id"], []).append(r)
    for p in info["parts"]:
        vs = have.get(p["tmdb_id"], [])
        p["in_library"] = bool(vs)
        p["movie_id"] = vs[0]["id"] if vs else None
        p["tier"] = vs[0]["tier"] if vs else None
        p["tiers"] = [v["tier"] for v in vs]
        p["has_file"] = any(v["file_path"] for v in vs)
    row = db.one("SELECT * FROM collections WHERE source=? AND tmdb_id=?", (info["source"], info["id"]))
    info["saved"] = row
    return info


async def details(source: str, cid: int) -> dict:
    return _with_library_state(await metadata.collection(source, cid))


async def _add_movies(tmdb_ids: list[int], tier: str) -> list[dict]:
    """Add movies in the collection's version(s) ('both' = 1080p and 4K), skipping versions
    already in the library; a few at a time."""
    sem = asyncio.Semaphore(4)
    added: list[dict] = []

    async def one(tid: int):
        async with sem:
            for t in library.tiers_of(tier):
                if db.one("SELECT 1 FROM movies WHERE tmdb_id=? AND tier=?", (tid, t)):
                    continue
                try:
                    added.append(await library.add_movie(tid, t))
                except Exception as e:  # noqa: BLE001
                    db.log(f"Could not add movie {tid} from collection: {e}", "warn")
    await asyncio.gather(*(one(t) for t in tmdb_ids))
    return added


async def _search_added(movies: list[dict]) -> None:
    """Search one movie at a time so indexer API limits aren't hammered."""
    from . import search
    for m in sorted(movies, key=lambda x: x.get("release_date") or ""):
        if not library.movie_available(m):
            continue
        try:
            await search.search_movie(m["id"])
        except Exception as e:  # noqa: BLE001
            db.log(f"Search for {m['title']} failed: {e}", "warn")


async def add(source: str, cid: int, tier: str, tmdb_ids: list[int] | None = None,
              monitor: bool = True, search: bool = True) -> dict:
    """Add a collection. `tmdb_ids` = the movies picked; None = all of them."""
    library.tiers_of(tier)  # validates hd | uhd | both
    info = await metadata.collection(source, cid)
    all_ids = [p["tmdb_id"] for p in info["parts"]]
    chosen = all_ids if tmdb_ids is None else [t for t in all_ids if t in set(tmdb_ids)]
    excluded = [t for t in all_ids if t not in set(chosen)]
    row = db.one("SELECT * FROM collections WHERE source=? AND tmdb_id=?", (source, cid))
    data = {"name": info["name"], "overview": info.get("overview"), "poster": info.get("poster"), "tier": tier,
            "monitored": int(monitor), "search_on_add": int(search), "excluded": json.dumps(excluded),
            "members": json.dumps(all_ids)}
    if row:
        db.update("collections", row["id"], data)
        coll_id = row["id"]
    else:
        coll_id = db.insert("collections", {"source": source, "tmdb_id": cid, **data})
    added = await _add_movies(chosen, tier)
    db.ex("UPDATE collections SET last_refresh=datetime('now') WHERE id=?", (coll_id,))
    db.log(f"Added collection {info['name']}: {len(added)} new movie version(s) [{tier.upper()}]")
    if search and added:
        asyncio.create_task(_search_added(added))
    return {"id": coll_id, "name": info["name"], "added": len(added), "total": len(all_ids)}


async def refresh(coll_id: int | None = None) -> int:
    """Add newly announced movies to monitored collections."""
    rows = db.q("SELECT * FROM collections WHERE monitored=1" + (" AND id=?" if coll_id else ""),
                (coll_id,) if coll_id else ())
    total = 0
    for c in rows:
        try:
            info = await metadata.collection(c["source"], c["tmdb_id"])
        except Exception as e:  # noqa: BLE001
            db.log(f"Refreshing collection {c['name']} failed: {e}", "warn")
            continue
        excluded = set(json.loads(c["excluded"] or "[]"))
        new = [p["tmdb_id"] for p in info["parts"] if p["tmdb_id"] not in excluded]
        added = await _add_movies(new, c["tier"])
        if added:
            db.log(f"Collection {c['name']}: added {', '.join(m['title'] for m in added)}")
            if c["search_on_add"]:
                asyncio.create_task(_search_added(added))
        db.ex("UPDATE collections SET last_refresh=datetime('now'), name=?, poster=COALESCE(?, poster), members=? WHERE id=?",
              (info["name"], info.get("poster"), json.dumps([p["tmdb_id"] for p in info["parts"]]), c["id"]))
        total += len(added)
    return total


async def refresh_due() -> None:
    for c in db.q("SELECT id FROM collections WHERE monitored=1 AND "
                  "(last_refresh IS NULL OR last_refresh < datetime('now','-1 day'))"):
        await refresh(c["id"])


def list_all() -> list[dict]:
    rows = db.q("SELECT * FROM collections ORDER BY name")
    for r in rows:
        members = json.loads(r["members"] or "[]")
        r["excluded"] = json.loads(r["excluded"] or "[]")
        r["total"] = len([m for m in members if m not in set(r["excluded"])])
        r["movies"] = db.q(
            f"SELECT id, tmdb_id, title, year, tier, file_path FROM movies WHERE tmdb_id IN ({','.join('?' * len(members))}) "
            "ORDER BY release_date", tuple(members)) if members else []
    return rows
