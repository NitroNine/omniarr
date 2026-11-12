"""Requests: users ask for movies, shows or collections; an admin approves them,
or they're approved straight away for users with auto-approve."""
from __future__ import annotations

import asyncio
import json

from . import collections_, db, library
from .services import metadata

# tables are created in db.SCHEMA (users, sessions, requests)
KINDS = ("movie", "tv", "collection")


def ensure_schema() -> None:
    db.connect()


async def _describe(kind: str, target_id: int, source: str | None) -> tuple[int, str | None, dict]:
    """Validate the target with the metadata provider and get a title/poster to show the admin.
    Returns the (possibly translated) target id and its description."""
    if kind == "movie":
        m = await metadata.movie_details(target_id)
        return target_id, None, {"title": m["title"], "year": m.get("year"), "poster": m.get("poster")}
    if kind == "tv":
        # source says which id target_id is: 'tmdb' or 'tvdb'. Prefer TMDB ids (always available).
        s = await metadata.series_details(**({"tmdb_id": target_id} if source == "tmdb" else {"tvdb_id": target_id}))
        info = {"title": s["title"], "year": s.get("year"), "poster": s.get("poster")}
        return (s["tmdb_id"], "tmdb", info) if s.get("tmdb_id") else (s["tvdb_id"], "tvdb", info)
    c = await metadata.collection(source or "collection", target_id)
    return target_id, source, {"title": c["name"], "year": None, "poster": c.get("poster")}


def _in_library(kind: str, target_id: int, source: str | None = None, tier: str = "hd") -> bool:
    """True when every requested version ('both' = 1080p and 4K) is already in the library."""
    if kind not in ("movie", "tv"):
        return False
    table, col = ("movies", "tmdb_id") if kind == "movie" else ("series", "tmdb_id" if source == "tmdb" else "tvdb_id")
    have = {r["tier"] for r in db.q(f"SELECT tier FROM {table} WHERE {col}=?", (target_id,))}
    return all(t in have for t in library.tiers_of(tier))


async def create(user: dict, body: dict) -> dict:
    kind = body.get("kind")
    if kind not in KINDS:
        raise ValueError("kind must be movie, tv or collection")
    tier = body.get("tier") or "hd"
    library.tiers_of(tier)  # hd | uhd | both
    if tier != "hd" and not user["allow_uhd"]:
        raise PermissionError("Your account can't request 4K. Ask an admin to allow it.")
    if kind == "tv":
        source = "tmdb" if body.get("tmdb_id") else "tvdb"
        raw_id = body.get("tmdb_id") or body.get("tvdb_id")
    else:
        raw_id = body.get("tmdb_id")
        source = body.get("source") if kind == "collection" else None
        if kind == "collection" and source not in ("collection", "keyword"):
            source = "collection"
    if not raw_id:
        raise ValueError("Missing id")
    target_id, source, info = await _describe(kind, int(raw_id), source)
    if _in_library(kind, target_id, source, tier) or (
            kind == "tv" and body.get("tvdb_id") and _in_library("tv", int(body["tvdb_id"]), "tvdb", tier)):
        raise ValueError(f"{info['title']} is already in the library")
    dup = db.one("SELECT * FROM requests WHERE kind=? AND target_id=? AND IFNULL(source,'')=? AND status='pending'",
                 (kind, target_id, source or ""))
    if dup:
        return {**dup, "duplicate": True}
    options = {k: body[k] for k in ("monitor", "search", "tmdb_ids") if k in body}
    rid = db.insert("requests", {"user_id": user["id"], "kind": kind, "target_id": target_id, "source": source,
                                 "title": info["title"], "year": info["year"], "poster": info["poster"], "tier": tier,
                                 "options": json.dumps(options)})
    db.log(f"{user['username']} requested {info['title']} [{_tier_label(tier)}]")
    if user["auto_approve"] or user["role"] == "admin":
        return await approve(rid, user, auto=True)
    return get(rid)


def _tier_label(tier: str) -> str:
    return "1080p + 4K" if tier == "both" else library.TIER_NAMES.get(tier, tier)


async def fulfill(req: dict) -> int | None:
    """Add the requested item to the library; returns the movie/series/collection id."""
    opts = req["options"] if isinstance(req["options"], dict) else json.loads(req["options"] or "{}")
    search = bool(opts.get("search", True))
    if req["kind"] == "movie":
        added = await library.add_movie_tiers(req["target_id"], req["tier"])
        from . import search as search_mod
        for m in added:
            if search:
                asyncio.create_task(search_mod.search_movie(m["id"]))
        return added[0]["id"]
    if req["kind"] == "tv":
        monitor = opts.get("monitor") if opts.get("monitor") in ("all", "future", "latest") else "all"
        if req["source"] == "tmdb":
            added = await library.add_series_tiers(None, req["tier"], monitor, tmdb_id=req["target_id"])
        else:  # TheTVDB id (also requests made before TMDB TV support)
            added = await library.add_series_tiers(req["target_id"], req["tier"], monitor)
        from . import search as search_mod
        for s in added:
            if search:
                asyncio.create_task(search_mod.search_series(s["id"]))
        return added[0]["id"]
    r = await collections_.add(req["source"] or "collection", req["target_id"], req["tier"],
                               opts.get("tmdb_ids"), monitor=bool(opts.get("monitor", True)), search=search)
    return r["id"]


async def approve(rid: int, admin: dict, tier: str | None = None, auto: bool = False) -> dict:
    req = get(rid)
    if not req:
        raise ValueError("Request not found")
    if req["status"] != "pending":
        raise ValueError(f"Request is already {req['status']}")
    if tier in (*library.TIERS, "both"):
        db.update("requests", rid, {"tier": tier})
        req["tier"] = tier
    try:
        item_id = await fulfill(req)
        db.ex("UPDATE requests SET status='approved', item_id=?, decided_by=?, decided_at=datetime('now'), note=? WHERE id=?",
              (item_id, admin["id"], "Auto-approved" if auto else None, rid))
        db.log(f"Request for {req['title']} {'auto-' if auto else ''}approved by {admin['username']}")
    except ValueError as e:  # e.g. someone added it in the meantime
        db.ex("UPDATE requests SET status='failed', note=?, decided_by=?, decided_at=datetime('now') WHERE id=?",
              (str(e), admin["id"], rid))
    return get(rid)


def decline(rid: int, admin: dict, note: str | None) -> dict:
    req = get(rid)
    if not req or req["status"] != "pending":
        raise ValueError("Only pending requests can be declined")
    db.ex("UPDATE requests SET status='declined', note=?, decided_by=?, decided_at=datetime('now') WHERE id=?",
          (note or None, admin["id"], rid))
    return get(rid)


def cancel(rid: int, user: dict) -> None:
    req = get(rid)
    if not req:
        return
    if user["role"] != "admin" and (req["user_id"] != user["id"] or req["status"] != "pending"):
        raise PermissionError("You can only cancel your own pending requests")
    db.ex("DELETE FROM requests WHERE id=?", (rid,))


def get(rid: int) -> dict | None:
    rows = _decorate(db.q(_SELECT + " WHERE r.id=?", (rid,)))
    return rows[0] if rows else None


_SELECT = ("SELECT r.*, u.username, d.username AS decided_by_name FROM requests r "
           "LEFT JOIN users u ON u.id=r.user_id LEFT JOIN users d ON d.id=r.decided_by")


def _decorate(rows: list[dict]) -> list[dict]:
    """Add download progress for approved requests ('available' once files exist)."""
    for r in rows:
        r["options"] = json.loads(r["options"] or "{}")
        r["progress"] = None
        if r["status"] != "approved" or not r["item_id"]:
            continue
        if r["kind"] == "movie":
            m = db.one("SELECT file_path FROM movies WHERE id=?", (r["item_id"],))
            r["progress"] = {"have": int(bool(m and m["file_path"])), "total": 1}
        elif r["kind"] == "tv":
            c = db.one("SELECT SUM(file_path IS NOT NULL) have, COUNT(*) total FROM episodes WHERE series_id=? AND monitored=1 "
                       "AND air_date <= date('now')", (r["item_id"],))
            r["progress"] = {"have": c["have"] or 0, "total": c["total"] or 0}
        else:
            c = next((x for x in collections_.list_all() if x["id"] == r["item_id"]), None)
            if c:
                r["progress"] = {"have": sum(1 for m in c["movies"] if m["file_path"]), "total": c["total"]}
        p = r["progress"]
        if p and p["total"] and p["have"] >= p["total"]:
            r["status"] = "available"
    return rows


def list_for(user: dict, status: str | None = None) -> list[dict]:
    sql, params = _SELECT, []
    where = []
    if user["role"] != "admin":
        where.append("r.user_id=?")
        params.append(user["id"])
    if status in ("pending", "declined", "failed"):
        where.append("r.status=?")
        params.append(status)
    if where:
        sql += " WHERE " + " AND ".join(where)
    rows = _decorate(db.q(sql + " ORDER BY r.status='pending' DESC, r.id DESC LIMIT 500", tuple(params)))
    if status in ("approved", "available"):
        rows = [r for r in rows if r["status"] == status]
    return rows


def pending_count() -> int:
    return db.one("SELECT COUNT(*) c FROM requests WHERE status='pending'")["c"]
