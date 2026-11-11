"""Add an .nzb file by hand: send it to NZBGet as a movie or TV download for a library title, so it is
imported (renamed, moved, quality recorded) like a grab from an indexer."""
from __future__ import annotations

import hashlib
import os
import re

from . import db, decision, search
from .naming import strip_year
from .parser import normalize_title, parse
from .services import newznab

MAX_NZB = 50 * 1024 * 1024


def nzb_size(nzb: bytes) -> int:
    """Total of the article sizes listed in the NZB (what NZBGet will download)."""
    return sum(int(b) for b in re.findall(rb'<segment[^>]*\bbytes="(\d+)"', nzb))


def release_name(filename: str, nzb: bytes) -> str:
    """The release name: the NZB's own <meta type="name">, else the file name without .nzb."""
    m = re.search(rb'<meta\s+type="name"\s*>([^<]{3,300})</meta>', nzb)
    if m:
        return m.group(1).decode("utf-8", "replace").strip()
    return re.sub(r"\.nzb$", "", os.path.basename(filename or ""), flags=re.I).strip()


def guess(kind: str, name: str) -> dict | None:
    """The library title (and version: 4K for 2160p releases) this release name belongs to."""
    p = parse(name)
    want = normalize_title(p.clean_name)
    if not want:
        return None
    tier = "uhd" if p.resolution >= 2160 else "hd"
    table = "movies" if kind == "movie" else "series"
    hits = []
    for r in db.q(f"SELECT * FROM {table}"):
        names = {r["title"], strip_year(r["title"]), r.get("original_title")}
        if r.get("year"):
            names.add(f"{strip_year(r['title'])} {r['year']}")
        if want not in {normalize_title(n) for n in names if n}:
            continue
        year_ok = not p.year or not r.get("year") or abs(p.year - r["year"]) <= 1
        hits.append((not year_ok, r["tier"] != tier, r["id"], r))
    return sorted(hits, key=lambda h: h[:3])[0][3] if hits else None


async def add(filename: str, nzb: bytes, kind: str, item_id: int | None = None) -> dict:
    if kind not in ("movie", "tv"):
        raise ValueError("Choose Movie or TV")
    if not nzb or len(nzb) > MAX_NZB or b"<nzb" not in nzb[:4096].lower():
        raise ValueError("That isn't an .nzb file")
    name = release_name(filename, nzb)
    table = "movies" if kind == "movie" else "series"
    item = db.one(f"SELECT * FROM {table} WHERE id=?", (item_id,)) if item_id else guess(kind, name)
    if not item:
        what = "movie" if kind == "movie" else "show"
        raise ValueError(f"Couldn't tell which {what} '{name}' is. Pick it from the list "
                         f"(add it to the library first if it isn't there).")
    rel = newznab.Release(title=name, guid="manual-" + hashlib.sha1(nzb).hexdigest()[:16], link="",
                          size=nzb_size(nzb), indexer="Manual upload", indexer_id=0)
    if kind == "movie":
        d = decision.evaluate(rel, "movie", decision.profile_for("movie", item["tier"], item=item))
    else:
        all_eps = db.q("SELECT * FROM episodes WHERE series_id=?", (item["id"],))
        d = decision.decide_episodes(item, all_eps, [rel])[0]  # maps the release onto episodes
        if not d.episode_ids:
            raise ValueError(f"Couldn't tell which episodes '{name}' has (it needs S01E02 or S01 in the name)")
    dl_id = await search.grab(d, kind, movie_id=item["id"] if kind == "movie" else None,
                              series_id=item["id"] if kind == "tv" else None, nzb=nzb)
    return {"id": dl_id, "title": item["title"], "tier": item["tier"], "release": name,
            "quality": d.quality, "episodes": len(d.episode_ids)}
