"""Newznab indexer client (works with NZBGeek, DrunkenSlug, NZBPlanet, NZBFinder, ...)."""
from __future__ import annotations

import asyncio
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import httpx

from .. import db

_SECRET = re.compile(r"([?&](?:api_?key|apitoken|token|r)=)[^&\s'\"<>]+", re.I)


def redact(text: str) -> str:
    """Hide API keys and RSS tokens in URLs before they reach an error message or the log."""
    return _SECRET.sub(r"\1***", str(text))


class IndexerError(RuntimeError):
    pass


class IndexerBusy(IndexerError):
    """The indexer said "too many requests"; it's paused until `until` (epoch seconds)."""

    def __init__(self, host: str, until: float):
        self.host, self.until = host, until
        super().__init__(f"{host} asked Omniarr to slow down; trying again in {max(1, round(until - time.time()))} s")


class _Gate:
    """One request at a time per indexer host, spaced out, with back-off after a 429."""

    def __init__(self):
        self.lock = asyncio.Lock()
        self.last = 0.0
        self.interval = MIN_INTERVAL
        self.until = 0.0
        self.backoff = 0.0


MIN_INTERVAL = 1.0  # seconds between requests to the same indexer
MAX_INTERVAL = 5.0
MAX_BACKOFF = 900.0
_gates: dict[str, _Gate] = {}


def _gate(url: str) -> tuple[str, _Gate]:
    host = urlsplit(url).netloc.lower()
    return host, _gates.setdefault(host, _Gate())


def paused_until(url: str) -> float | None:
    g = _gates.get(urlsplit(url).netloc.lower())
    return g.until if g and g.until > time.time() else None


def _retry_after(r: httpx.Response) -> float | None:
    v = r.headers.get("retry-after")
    if not v:
        return None
    if v.strip().isdigit():
        return float(v)
    try:
        return max(0.0, parsedate_to_datetime(v).timestamp() - time.time())
    except (TypeError, ValueError):
        return None


async def _request(url: str, params: dict | None = None, timeout: float = 45) -> httpx.Response:
    host, g = _gate(url)
    if g.until > time.time():
        raise IndexerBusy(host, g.until)
    async with g.lock:
        if g.until > time.time():
            raise IndexerBusy(host, g.until)
        wait = g.last + g.interval - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers={"User-Agent": "omniarr"}) as c:
                r = await c.get(url, params=params)
        except httpx.HTTPError as e:
            raise IndexerError(f"{host}: {redact(e) or type(e).__name__}") from None
        finally:
            g.last = time.monotonic()
        if r.status_code == 429:
            g.backoff = min(MAX_BACKOFF, max(60.0, g.backoff * 2))
            pause = _retry_after(r) or g.backoff
            g.until = time.time() + pause
            g.interval = min(MAX_INTERVAL, g.interval * 1.5)  # and go a bit slower afterwards
            db.log(f"Indexer {host} said \"too many requests\" (429). Pausing it for {round(pause)} s, "
                   f"then one request every {g.interval:.1f} s.", "warn")
            raise IndexerBusy(host, g.until)
        g.backoff = 0.0
        if r.status_code >= 400:
            raise IndexerError(f"{host} answered {r.status_code} {r.reason_phrase}")
        return r

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
    r = await _request(_api_url(indexer["url"]), params)
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
    busy = []
    for i, res in zip(idx, results):
        if isinstance(res, IndexerBusy):
            busy.append(res)  # already logged when the pause started
        elif isinstance(res, Exception):
            db.log(f"Indexer {i['name']} failed: {redact(res)}", "warn")
        else:
            out.extend(res)
    if busy and len(busy) == len(idx):
        raise busy[0]  # every indexer is paused: try this search again later rather than calling it "nothing found"
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
            except IndexerBusy:
                raise
            except RuntimeError:
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
            except IndexerBusy:
                raise
            except RuntimeError:
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
    r = await _request(link, timeout=60)
    if b"<nzb" not in r.content[:2000] and b"<?xml" not in r.content[:100]:
        raise RuntimeError("Indexer did not return an NZB file (check API limits).")
    return r.content


def parse_caps(xml_text: str) -> dict:
    """An indexer's capabilities: its name and its category tree
    [{id, name, subcats: [{id, name}]}], including any non-standard categories it uses."""
    root = ET.fromstring(xml_text)
    if root.tag == "error":
        raise RuntimeError(root.get("description") or "The indexer returned an error")
    srv = root.find("server")
    cats = []
    for c in root.findall("./categories/category"):
        if not (c.get("id") or "").isdigit():
            continue
        cats.append({"id": int(c.get("id")), "name": c.get("name") or c.get("id"),
                     "subcats": [{"id": int(s.get("id")), "name": s.get("name") or s.get("id")}
                                 for s in c.findall("subcat") if (s.get("id") or "").isdigit()]})
    cats.sort(key=lambda c: c["id"])
    return {"title": srv.get("title") if srv is not None else None, "categories": cats}


async def caps(url: str, apikey: str) -> dict:
    try:
        r = await _request(_api_url(url), {"t": "caps", "apikey": apikey}, timeout=20)
    except IndexerError as e:
        if " 401 " in str(e) or " 403 " in str(e):
            raise RuntimeError("The indexer rejected the API key") from None
        raise
    return parse_caps(r.text)


async def test(indexer: dict) -> str:
    r = await _request(_api_url(indexer["url"]), {"t": "caps", "apikey": indexer["apikey"]}, timeout=20)
    root = ET.fromstring(r.text)
    if root.tag == "error":
        raise RuntimeError(root.get("description"))
    srv = root.find("server")
    return srv.get("title") if srv is not None and srv.get("title") else "OK"
