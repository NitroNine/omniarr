"""Torrents added by hand, linked to a movie or show in the library. When a linked torrent finishes, its
files are imported like a Usenet download (TRaSH naming, into the title's folder), but hardlinked (or
copied when the folders are on different disks), never moved, so the torrent keeps seeding.

A link is a row in `downloads` with `torrent_hash` set (and no `nzbget_id`); Activity, the title's
history and "is something already downloading this?" all work as for Usenet downloads."""
from __future__ import annotations

import json

from . import db, decision, importer, manual_nzb
from .parser import parse
from .services import newznab
from .services import qbittorrent as qbt

OPEN = ("queued", "downloading", "importing", "import_failed")


def link_of(h: str) -> dict | None:
    return db.one("SELECT * FROM downloads WHERE torrent_hash=? AND status NOT IN ('removed') ORDER BY id DESC", (h.lower(),))


def guess(kind: str, name: str) -> dict | None:
    return manual_nzb.guess(kind, name) if name else None


def link(h: str, name: str, size: int, kind: str, item_id: int | None) -> dict:
    """Tie a torrent to a library movie or show (item_id, or guessed from the torrent's name)."""
    h = h.lower()
    if kind not in ("movie", "tv"):
        raise ValueError("Choose Movie or TV")
    table = "movies" if kind == "movie" else "series"
    item = db.one(f"SELECT * FROM {table} WHERE id=?", (item_id,)) if item_id else guess(kind, name)
    if not item:
        what = "movie" if kind == "movie" else "show"
        raise ValueError(f"Couldn't tell which {what} '{name}' is. Pick it from the list "
                         f"(add it to the library first if it isn't there).")
    old = link_of(h)
    if old and old["status"] == "imported":
        raise ValueError("This torrent was already imported")
    if old:
        db.ex("UPDATE downloads SET status='removed', message='Linked to another title' WHERE id=?", (old["id"],))
    rel = newznab.Release(title=name, guid="torrent-" + h, link="", size=size or 0, indexer="Torrent", indexer_id=0)
    episode_ids: list[int] = []
    if kind == "movie":
        d = decision.evaluate(rel, "movie", decision.profile_for("movie", item["tier"], item=item))
    else:
        all_eps = db.q("SELECT * FROM episodes WHERE series_id=?", (item["id"],))
        d = decision.decide_episodes(item, all_eps, [rel])[0]
        episode_ids = d.episode_ids  # empty: the files' own SxxEyy names decide at import time
    quality = d.quality or parse(name).quality_name(kind)
    dl_id = db.insert("downloads", {
        "kind": kind, "movie_id": item["id"] if kind == "movie" else None,
        "series_id": item["id"] if kind == "tv" else None, "episode_ids": json.dumps(episode_ids),
        "release_title": name, "indexer": "Torrent", "guid": rel.guid, "size": size, "quality": quality,
        "score": d.score, "custom_formats": json.dumps(d.custom_formats), "status": "queued", "torrent_hash": h,
    })
    db.log(f"Torrent {name} linked to {item['title']} ({quality})")
    return {"id": dl_id, "title": item["title"], "tier": item["tier"], "quality": quality, "episodes": len(episode_ids)}


def unlink(h: str) -> None:
    db.ex(f"UPDATE downloads SET status='removed', message='Unlinked' WHERE torrent_hash=? AND status IN ({','.join('?' * len(OPEN))})",
          (h.lower(), *OPEN))


async def add(magnet: str | None = None, data: bytes | None = None, filename: str = "", kind: str | None = None,
              item_id: int | None = None) -> dict:
    """Add a magnet link or .torrent file; with a kind, link it (to item_id, or the title its name points to)."""
    if data:
        h, name = await qbt.add_file(data, filename or "file.torrent"), qbt.torrent_name(data)
    elif magnet:
        h, name = await qbt.add_magnet(magnet), qbt.magnet_name(magnet)
    else:
        raise ValueError("Paste a magnet link or choose a .torrent file")
    name = name or h
    out = {"hash": h, "name": name, "linked": None}
    if kind in ("movie", "tv"):
        try:
            out["linked"] = link(h, name, 0, kind, item_id)
        except ValueError as e:
            out["link_error"] = str(e)  # added anyway; link it from the list once the title is in the library
    db.log(f"Torrent added: {name}")
    return out


async def listing() -> list[dict]:
    rows = await qbt.torrents()
    links = {r["torrent_hash"]: r for r in db.q(
        """SELECT d.*, m.title AS movie_title, m.tier AS movie_tier, s.title AS series_title, s.tier AS series_tier
           FROM downloads d LEFT JOIN movies m ON m.id=d.movie_id LEFT JOIN series s ON s.id=d.series_id
           WHERE d.torrent_hash IS NOT NULL AND d.status != 'removed' ORDER BY d.id""")}
    out = []
    for t in sorted(rows, key=lambda t: -(t.get("added_on") or 0)):
        ln = links.get(t["hash"])
        out.append({
            "hash": t["hash"], "name": t.get("name"), "size": t.get("total_size") or t.get("size"),
            "progress": round((t.get("progress") or 0) * 100, 1), "state": t.get("state"),
            "dlspeed": t.get("dlspeed"), "upspeed": t.get("upspeed"), "eta": t.get("eta"),
            "seeds": t.get("num_seeds"), "peers": t.get("num_leechs"), "ratio": round(t.get("ratio") or 0, 2),
            "added_on": t.get("added_on"), "path": t.get("content_path"),
            "link": {"id": ln["id"], "kind": ln["kind"], "status": ln["status"], "message": ln["message"],
                     "title": ln["movie_title"] or ln["series_title"], "tier": ln["movie_tier"] or ln["series_tier"],
                     "item_id": ln["movie_id"] or ln["series_id"], "quality": ln["quality"]} if ln else None,
        })
    return out


async def process() -> None:
    """Scheduler (with the Usenet downloads): progress of linked torrents; import the finished ones."""
    rows = db.q("SELECT * FROM downloads WHERE torrent_hash IS NOT NULL AND status IN ('queued','downloading','importing')")
    if not rows:
        return
    try:
        live = {t["hash"]: t for t in await qbt.torrents([r["torrent_hash"] for r in rows])}
    except qbt.TorrentError:
        return  # not running yet; try again next round
    for dl in rows:
        t = live.get(dl["torrent_hash"])
        if not t:
            db.ex("UPDATE downloads SET status='failed', message='The torrent was removed from the torrent client', "
                  "updated=datetime('now') WHERE id=?", (dl["id"],))
            continue
        size = t.get("total_size") or t.get("size") or dl["size"]
        if not qbt.finished(t):
            db.ex("UPDATE downloads SET status=?, progress=?, size=?, message=?, updated=datetime('now') WHERE id=?",
                  ("downloading" if (t.get("progress") or 0) > 0 else "queued", round((t.get("progress") or 0) * 100, 1),
                   size, t.get("state"), dl["id"]))
            continue
        src = t.get("content_path") or ""
        db.ex("UPDATE downloads SET status='importing', progress=100, size=?, dest_dir=? WHERE id=?", (size, src, dl["id"]))
        try:
            await import_torrent(dl, src)
            db.ex("UPDATE downloads SET status='imported', message=NULL, updated=datetime('now') WHERE id=?", (dl["id"],))
        except Exception as e:  # noqa: BLE001
            db.ex("UPDATE downloads SET status='import_failed', message=?, updated=datetime('now') WHERE id=?", (str(e), dl["id"]))
            db.log(f"Import failed for torrent {dl['release_title']}: {e}", "error")


async def import_torrent(dl: dict, src: str) -> None:
    """Import without moving anything out of the torrent's folder: hardlink (copy across disks)."""
    token = importer.TRANSFER_MODE.set("hardlink")
    try:
        if dl["kind"] == "movie":
            await importer.import_movie(dl, src)
        else:
            await importer.import_episodes(dl, src)
    finally:
        importer.TRANSFER_MODE.reset(token)
