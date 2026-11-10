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


async def _describe(kind: str, target_id: int, source: str | None, tmdb_tv: bool) -> tuple[int, dict]:
    """Validate the target with the metadata provider and get a title/poster to show the admin.
    Returns the (possibly translated) target id and its description."""
    if kind == "movie":
        m = await metadata.movie_details(target_id)
        return target_id, {"title": m["title"], "year": m.get("year"), "poster": m.get("poster")}
    if kind == "tv":
        if tmdb_tv:
            tvdb = await metadata.tvdb_id_for_tmdb_tv(target_id)
            if not tvdb:
                raise ValueError("TheTVDB doesn't know this show yet, so it can't be requested.")
            target_id = tvdb
        s = await metadata.series_details(target_id)
        return target_id, {"title": s["title"], "year": s.get("year"), "poster": s.get("poster")}
    c = await metadata.collection(source or "collection", target_id)
    return target_id, {"title": c["name"], "year": None, "poster": c.get("poster")}


def _in_library(kind: str, target_id: int) -> dict | None:
    if kind == "movie":
        return db.one("SELECT id FROM movies WHERE tmdb_id=?", (target_id,))
    if kind == "tv":
        return db.one("SELECT id FROM series WHERE tvdb_id=?", (target_id,))
    return None


async def create(user: dict, body: dict) -> dict:
    kind = body.get("kind")
    if kind not in KINDS:
        raise ValueError("kind must be movie, tv or collection")
    tier = body.get("tier") or "hd"
    if tier not in library.TIERS:
        raise ValueError("tier must be 'hd' or 'uhd'")
    if tier == "uhd" and not user["allow_uhd"]:
        raise PermissionError("Your account can't request 4K. Ask an admin to allow it.")
    tmdb_tv = kind == "tv" and not body.get("tvdb_id") and bool(body.get("tmdb_id"))
    raw_id = body.get("tvdb_id") if kind == "tv" and body.get("tvdb_id") else body.get("tmdb_id")
    if not raw_id:
        raise ValueError("Missing id")
    source = body.get("source") if kind == "collection" else None
    if kind == "collection" and source not in ("collection", "keyword"):
        source = "collection"
    target_id, info = await _describe(kind, int(raw_id), source, tmdb_tv)
    if _in_library(kind, target_id):
        raise ValueError(f"{info['title']} is already in the library")
    dup = db.one("SELECT * FROM requests WHERE kind=? AND target_id=? AND IFNULL(source,'')=? AND status='pending'",
                 (kind, target_id, source or ""))
    if dup:
        return {**dup, "duplicate": True}
    options = {k: body[k] for k in ("monitor", "search", "tmdb_ids") if k in body}
    rid = db.insert("requests", {"user_id": user["id"], "kind": kind, "target_id": target_id, "source": source,
                                 "title": info["title"], "year": info["year"], "poster": info["poster"], "tier": tier,
                                 "options": json.dumps(options)})
    db.log(f"{user['username']} requested {info['title']} [{'4K' if tier == 'uhd' else '1080p'}]")
    if user["auto_approve"] or user["role"] == "admin":
        return await approve(rid, user, auto=True)
    return get(rid)


async def fulfill(req: dict) -> int | None:
    """Add the requested item to the library; returns the movie/series/collection id."""
    opts = req["options"] if isinstance(req["options"], dict) else json.loads(req["options"] or "{}")
    search = bool(opts.get("search", True))
    if req["kind"] == "movie":
        m = await library.add_movie(req["target_id"], req["tier"])
        if search and library.movie_available(m):
            from . import search as search_mod
            asyncio.create_task(search_mod.search_movie(m["id"]))
        return m["id"]
    if req["kind"] == "tv":
        monitor = opts.get("monitor") if opts.get("monitor") in ("all", "future", "latest") else "all"
        s = await library.add_series(req["target_id"], req["tier"], monitor)
        if search:
            from . import search as search_mod
            asyncio.create_task(search_mod.search_series(s["id"]))
        return s["id"]
    r = await collections_.add(req["source"] or "collection", req["target_id"], req["tier"],
                               opts.get("tmdb_ids"), monitor=bool(opts.get("monitor", True)), search=search)
    return r["id"]


async def approve(rid: int, admin: dict, tier: str | None = None, auto: bool = False) -> dict:
    req = get(rid)
    if not req:
        raise ValueError("Request not found")
    if req["status"] != "pending":
        raise ValueError(f"Request is already {req['status']}")
    if tier in library.TIERS:
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
