"""Newznab indexer client (works with NZBGeek, DrunkenSlug, NZBPlanet, NZBFinder, ...)."""
from __future__ import annotations

import asyncio
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from email.utils import parsedate_to_datetime

import httpx

from .. import db

NS = {"newznab": "http://www.newznab.com/DTD/2010/feeds/attributes/"}


@dataclass
class Release:
    title: str
    guid: str
    link: str
    size: int
    indexer: str
    indexer_id: int
    pub_date: str | None = None
    age_days: float | None = None
    grabs: int | None = None
    tvdb_id: int | None = None
    imdb_id: str | None = None


def _api_url(base: str) -> str:
    base = base.rstrip("/")
    return base if base.endswith("/api") else base + "/api"


def parse_feed(xml_text: str, indexer: dict) -> list[Release]:
    root = ET.fromstring(xml_text)
    err = root if root.tag == "error" else root.find("error")
    if err is not None:
        raise RuntimeError(f"{indexer['name']}: {err.get('description') or err.get('code')}")
    out = []
    for item in root.iter("item"):
        attrs = {a.get("name"): a.get("value") for a in item.findall("newznab:attr", NS)}
        enc = item.find("enclosure")
        link = (enc.get("url") if enc is not None else None) or item.findtext("link") or ""
        size = int(attrs.get("size") or (enc.get("length") if enc is not None else 0) or 0)
        pub = item.findtext("pubDate")
        age = None
        if pub:
            try:
                from datetime import datetime, timezone
                age = (datetime.now(timezone.utc) - parsedate_to_datetime(pub)).total_seconds() / 86400
            except (TypeError, ValueError):
                pass
        out.append(Release(
            title=(item.findtext("title") or "").strip(), guid=item.findtext("guid") or link, link=link,
            size=size, indexer=indexer["name"], indexer_id=indexer["id"], pub_date=pub, age_days=age,
            grabs=int(attrs["grabs"]) if attrs.get("grabs", "").isdigit() else None,
            tvdb_id=int(attrs["tvdbid"]) if attrs.get("tvdbid", "").isdigit() else None,
            imdb_id=attrs.get("imdb"),
        ))
    return out


async def _query(indexer: dict, params: dict) -> list[Release]:
    params = {**params, "apikey": indexer["apikey"], "extended": 1, "limit": 100}
    async with httpx.AsyncClient(timeout=45, follow_redirects=True, headers={"User-Agent": "omniarr"}) as c:
        r = await c.get(_api_url(indexer["url"]), params=params)
    r.raise_for_status()
    return parse_feed(r.text, indexer)


def enabled_indexers() -> list[dict]:
    return db.q("SELECT * FROM indexers WHERE enabled=1 ORDER BY priority, id")


async def _fan_out(build) -> list[Release]:
    idx = enabled_indexers()
    if not idx:
        db.log("No enabled indexers — add one under Settings → Indexers.", "warn")
        return []
    results = await asyncio.gather(*(build(i) for i in idx), return_exceptions=True)
    out: list[Release] = []
    for i, res in zip(idx, results):
        if isinstance(res, Exception):
            db.log(f"Indexer {i['name']} failed: {res}", "warn")
        else:
            out.extend(res)
    seen, uniq = set(), []
    for r in out:
        k = (r.title.lower(), r.size // (1024 * 1024))
        if k not in seen:
            seen.add(k)
            uniq.append(r)
    return uniq


async def search_movie(movie: dict) -> list[Release]:
    async def one(ix):
        cat = ix["movie_categories"]
        res: list[Release] = []
        if movie.get("imdb_id"):
            try:
                res = await _query(ix, {"t": "movie", "imdbid": movie["imdb_id"].removeprefix("tt"), "cat": cat})
            except (httpx.HTTPError, RuntimeError):
                res = []
        if not res:
            res = await _query(ix, {"t": "search", "q": f"{movie['title']} {movie.get('year') or ''}".strip(), "cat": cat})
        return res
    return await _fan_out(one)


async def search_episodes(series: dict, season: int, episode: int | None = None) -> list[Release]:
    async def one(ix):
        cat = ix["tv_categories"]
        p = {"t": "tvsearch", "cat": cat, "season": season}
        if episode is not None:
            p["ep"] = episode
        res: list[Release] = []
        # best id first: TheTVDB (all newznab indexers), then IMDb, then plain title search
        for key, val in (("tvdbid", series.get("tvdb_id")),
                         ("imdbid", (series.get("imdb_id") or "").removeprefix("tt") or None)):
            if res or not val:
                continue
            try:
                res = await _query(ix, {**p, key: val})
            except (httpx.HTTPError, RuntimeError):
                res = []
        if not res:
            res = await _query(ix, {**p, "q": re.sub(r"\s*\((19|20)\d{2}\)$", "", series["title"])})
        return res
    return await _fan_out(one)


async def rss(kind: str) -> list[Release]:
    async def one(ix):
        cat = ix["movie_categories"] if kind == "movie" else ix["tv_categories"]
        return await _query(ix, {"t": "movie" if kind == "movie" else "tvsearch", "cat": cat})
    return await _fan_out(one)


async def fetch_nzb(link: str) -> bytes:
    async with httpx.AsyncClient(timeout=60, follow_redirects=True, headers={"User-Agent": "omniarr"}) as c:
        r = await c.get(link)
    r.raise_for_status()
    if b"<nzb" not in r.content[:2000] and b"<?xml" not in r.content[:100]:
        raise RuntimeError("Indexer did not return an NZB file (check API limits).")
    return r.content


async def test(indexer: dict) -> str:
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as c:
        r = await c.get(_api_url(indexer["url"]), params={"t": "caps", "apikey": indexer["apikey"]})
    r.raise_for_status()
    root = ET.fromstring(r.text)
    if root.tag == "error":
        raise RuntimeError(root.get("description"))
    srv = root.find("server")
    return srv.get("title") if srv is not None and srv.get("title") else "OK"
