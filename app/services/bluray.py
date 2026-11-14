"""4K Ultra HD Blu-ray release dates from Blu-ray.com's "New Releases" RSS feed.

Blu-ray.com doesn't allow crawling its pages, but its legal notice allows fetching its RSS feeds automatically, so
Omniarr only reads the feed. It holds the latest ~40 releases (about a week), so every 4K disc seen is kept in the
table `bluray_releases` and the list grows over time. Titles are matched to TMDB (title + year) once."""
from __future__ import annotations

import html
import re
import time
import xml.etree.ElementTree as ET

import httpx

from .. import db

FEED = "https://www.blu-ray.com/rss/newreleasesfeed.xml"
_UHD = re.compile(r"\s+4K\s*\(Blu-ray\)\s*$", re.I)
_TAGS = re.compile(r"<[^>]+>")


def parse(xml_text: str) -> list[dict]:
    """The 4K items of the feed: title (without " 4K (Blu-ray)"), film year, studio, release date, Blu-ray.com link."""
    out = []
    for it in ET.fromstring(xml_text).iter("item"):
        raw = (it.findtext("title") or "").strip()
        if not _UHD.search(raw):
            continue
        # description starts "<b><font>Studio | 2006 | 123 mins | Rated PG-13 | Nov 10, 2026</font></b>…"
        head = html.unescape(_TAGS.sub("\n", html.unescape(it.findtext("description") or ""))).strip().split("\n")[0]
        parts = [p.strip() for p in head.split("|")]
        year = next((int(p) for p in parts[1:] if re.fullmatch(r"(19|20)\d\d", p)), None)
        date = None
        for p in reversed(parts):
            try:
                date = time.strftime("%Y-%m-%d", time.strptime(p, "%b %d, %Y"))
                break
            except ValueError:
                continue
        if not date:
            continue
        out.append({"url": (it.findtext("link") or "").strip(), "title": _UHD.sub("", raw).strip(), "year": year,
                    "studio": parts[0] if len(parts) > 1 else None, "release_date": date})
    return [r for r in out if r["url"]]


async def _match(r: dict) -> int | None:
    from . import metadata
    res = await metadata.search_movies(f"{r['title']} {r['year']}" if r["year"] else r["title"])
    if not res and r["year"]:  # TMDB's year is sometimes a year off the disc's
        res = await metadata.search_movies(r["title"])
    norm = lambda t: re.sub(r"[^a-z0-9]", "", (t or "").lower())  # noqa: E731
    for m in res:
        year_ok = not r["year"] or not m.get("year") or abs(m["year"] - r["year"]) <= 1
        if year_ok and norm(r["title"]) in (norm(m["title"]), norm(m.get("original_title"))):
            return m["tmdb_id"]
    return res[0]["tmdb_id"] if len(res) == 1 and r["year"] and res[0].get("year") == r["year"] else None


async def sync() -> int:
    """Read the feed, keep new 4K discs, match unmatched ones to TMDB. Returns how many were new."""
    async with httpx.AsyncClient(timeout=20, headers={"User-Agent": "Omniarr (RSS reader)"}) as c:
        r = await c.get(FEED)
    r.raise_for_status()
    new = 0
    for it in parse(r.text):
        if db.one("SELECT 1 FROM bluray_releases WHERE url=?", (it["url"],)):
            db.ex("UPDATE bluray_releases SET release_date=? WHERE url=?", (it["release_date"], it["url"]))  # date moved
            continue
        db.ex("INSERT INTO bluray_releases(url, title, year, studio, release_date) VALUES(?,?,?,?,?)",
              (it["url"], it["title"], it["year"], it["studio"], it["release_date"]))
        new += 1
    for row in db.q("SELECT * FROM bluray_releases WHERE matched=0 LIMIT 60"):
        try:
            tid = await _match(row)
        except Exception:  # noqa: BLE001 - TMDB down or no key: try again next time
            break
        db.ex("UPDATE bluray_releases SET tmdb_id=?, matched=1 WHERE url=?", (tid, row["url"]))
    if new:
        db.log(f"Blu-ray.com: {new} new 4K disc release(s)")
    return new


def releases(since: str, until: str) -> list[dict]:
    """Matched 4K discs released between since and until (newest first)."""
    return db.q("SELECT tmdb_id, MIN(release_date) release_date, MAX(url) url FROM bluray_releases "
                "WHERE tmdb_id IS NOT NULL AND release_date BETWEEN ? AND ? GROUP BY tmdb_id ORDER BY release_date DESC",
                (since, until))
