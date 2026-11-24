"""Audible's public catalog (no account needed): search, book details, popular and new books.

Ported from Spokarr (https://github.com/BlugDeg/Spokarr, MIT licence, src/audible.mjs)."""
from __future__ import annotations

import html
import re
import time

import httpx

from .. import db

REGIONS = {  # store → (API host, language of its books)
    "us": ("api.audible.com", "english"), "ca": ("api.audible.ca", "english"), "uk": ("api.audible.co.uk", "english"),
    "au": ("api.audible.com.au", "english"), "in": ("api.audible.in", "english"), "de": ("api.audible.de", "german"),
    "fr": ("api.audible.fr", "french"), "it": ("api.audible.it", "italian"), "es": ("api.audible.es", "spanish"),
    "jp": ("api.audible.co.jp", "japanese"),
}
RESPONSE_GROUPS = "contributors,media,product_attrs,product_desc,product_extended_attrs,series,rating"
BOOK_TYPES = {"SinglePartBook", "MultiPartBook"}
_cache: dict[str, tuple[float, object]] = {}


class AudibleError(RuntimeError):
    pass


def region() -> str:
    r = str(db.setting("audible_region") or "us").lower()
    return r if r in REGIONS else "us"


def language() -> str:
    return REGIONS[region()][1]


async def _get(path: str, params: dict) -> dict:
    url = f"https://{REGIONS[region()][0]}/1.0/catalog/products{path}"
    try:
        async with httpx.AsyncClient(timeout=20, headers={"Accept": "application/json", "User-Agent": "omniarr"}) as c:
            r = await c.get(url, params={"response_groups": RESPONSE_GROUPS, "image_sizes": "500,1024", **params})
    except httpx.HTTPError as e:
        raise AudibleError(f"Audible didn't answer: {type(e).__name__}") from None
    if r.status_code == 404:
        raise AudibleError("Audible doesn't know that book")
    if r.status_code >= 400:
        raise AudibleError(f"Audible answered {r.status_code}")
    return r.json()


def _strip_html(s: str) -> str:
    s = re.sub(r"<br\s*/?>", "\n", s or "", flags=re.I)
    s = re.sub(r"</p>", "\n\n", s, flags=re.I)
    s = html.unescape(re.sub(r"<[^>]+>", "", s))
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def map_product(p: dict) -> dict:
    imgs = p.get("product_images") or {}
    rating = ((p.get("rating") or {}).get("overall_distribution") or {}).get("display_average_rating")
    try:
        rating = float(rating) if rating else None
    except (TypeError, ValueError):
        rating = None
    return {
        "asin": p.get("asin"), "title": p.get("title") or "", "subtitle": p.get("subtitle") or "",
        "authors": [a.get("name") for a in p.get("authors") or [] if a.get("name")],
        "narrators": [a.get("name") for a in p.get("narrators") or [] if a.get("name")],
        "minutes": p.get("runtime_length_min") or 0,
        "abridged": p.get("format_type") == "abridged",
        "release_date": p.get("release_date") or p.get("issue_date") or "",
        "series": [{"title": s.get("title"), "seq": s.get("sequence") or ""} for s in p.get("series") or [] if s.get("title")],
        "cover": imgs.get("1024") or imgs.get("500") or "",
        "cover_small": imgs.get("500") or imgs.get("1024") or "",
        "language": p.get("language") or "",
        "publisher": p.get("publisher_name") or "",
        "summary": _strip_html(p.get("publisher_summary") or p.get("merchandising_summary") or ""),
        "rating": rating,
    }


def _books(d: dict, language_only: bool) -> list[dict]:
    lang = language()
    return [map_product(p) for p in d.get("products") or []
            if (not p.get("content_delivery_type") or p["content_delivery_type"] in BOOK_TYPES)
            and (not language_only or p.get("language") == lang)]


async def _cached(key: str, ttl: float, fn):
    hit = _cache.get(key)
    if hit and hit[0] > time.time():
        return hit[1]
    value = await fn()
    _cache[key] = (time.time() + ttl, value)
    return value


async def search(term: str) -> list[dict]:
    """Books matching a title, author or narrator; an ASIN (B0…) goes straight to that book."""
    term = (term or "").strip()
    if re.fullmatch(r"[A-Z0-9]{10}", term.upper()) and term.upper().startswith("B"):
        try:
            return [await get_book(term.upper())]
        except AudibleError:
            pass
    d = await _get("", {"keywords": term, "num_results": 30, "products_sort_by": "Relevance"})
    return _books(d, language_only=False)


async def get_book(asin: str) -> dict:
    async def fetch():
        d = await _get("/" + asin, {})
        if not (d.get("product") or {}).get("title"):
            raise AudibleError("Audible doesn't know that book")
        return map_product(d["product"])
    return await _cached(f"book:{region()}:{asin}", 6 * 3600, fetch)


async def discover() -> list[dict]:
    """Rows for the Audiobooks page: best sellers (out now), and those released in the last 90 days."""
    async def fetch():
        from datetime import date, timedelta
        today = date.today().isoformat()
        cutoff = (date.today() - timedelta(days=90)).isoformat()
        rows = []
        try:
            best = _books(await _get("", {"products_sort_by": "BestSellers", "num_results": 50}), language_only=True)
        except AudibleError as e:
            return [{"id": "popular", "title": "Popular", "items": [], "error": str(e)}]
        out = [b for b in best if not b["release_date"] or b["release_date"] <= today]  # no pre-orders
        rows.append({"id": "popular", "title": "Popular", "items": out[:24]})
        # (sorting Audible by release date gives pre-orders a year out, so new = recent best sellers)
        new = [b for b in best if b["release_date"] and cutoff <= b["release_date"] <= today]
        rows.append({"id": "new", "title": "New in the last 90 days", "items": new[:24]})
        return rows
    return await _cached(f"discover:{region()}", 1800, fetch)
