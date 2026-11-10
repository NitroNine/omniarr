"""Metadata provider: The Movie Database (TMDB) for movies and TV."""
from __future__ import annotations

import re
import time

import httpx

from .. import db

TMDB = "https://api.themoviedb.org/3"
TMDB_IMG = "https://image.tmdb.org/t/p/w342"


class MetadataError(Exception):
    pass


# ---------------------------------------------------------------- TMDB ----
def _tmdb_auth() -> tuple[dict, dict]:
    key = db.setting("tmdb_api_key")
    if not key:
        raise MetadataError("TMDB API key is not set (Settings → Metadata).")
    if len(key) > 40:  # v4 "API Read Access Token"
        return {"Authorization": f"Bearer {key}"}, {}
    return {}, {"api_key": key}


async def _tmdb(path: str, **params) -> dict:
    headers, auth = _tmdb_auth()
    params = {**auth, "language": db.setting("metadata_language") or "en", **params}
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(TMDB + path, params=params, headers=headers)
    if r.status_code == 401:
        raise MetadataError("TMDB rejected the API key.")
    r.raise_for_status()
    return r.json()


def _year(date: str | None) -> int | None:
    return int(date[:4]) if date and len(date) >= 4 and date[:4].isdigit() else None


async def search_movies(term: str) -> list[dict]:
    year = None
    parts = term.rsplit(" ", 1)
    if len(parts) == 2 and parts[1].isdigit() and len(parts[1]) == 4:
        term, year = parts[0], parts[1]
    if term.lower().startswith("tmdb:"):
        m = await movie_details(int(term[5:]))
        return [m]
    data = await _tmdb("/search/movie", query=term, **({"year": year} if year else {}))
    return [{
        "tmdb_id": m["id"], "title": m["title"], "original_title": m.get("original_title"),
        "year": _year(m.get("release_date")), "overview": m.get("overview"),
        "poster": TMDB_IMG + m["poster_path"] if m.get("poster_path") else None,
        "release_date": m.get("release_date"),
    } for m in data.get("results", [])[:20]]


async def movie_by_imdb(imdb_id: str) -> dict | None:
    data = await _tmdb(f"/find/{imdb_id}", external_source="imdb_id")
    res = data.get("movie_results") or []
    return await movie_details(res[0]["id"]) if res else None


async def movie_details(tmdb_id: int) -> dict:
    m = await _tmdb(f"/movie/{tmdb_id}", append_to_response="external_ids")
    coll = m.get("belongs_to_collection") or {}
    return {
        "collection_id": coll.get("id"), "collection_name": coll.get("name"),
        "tmdb_id": m["id"], "imdb_id": m.get("imdb_id") or m.get("external_ids", {}).get("imdb_id"),
        "title": m["title"], "original_title": m.get("original_title"),
        "year": _year(m.get("release_date")), "overview": m.get("overview"),
        "poster": TMDB_IMG + m["poster_path"] if m.get("poster_path") else None,
        "runtime": m.get("runtime") or 0, "release_date": m.get("release_date"),
    }


def _movie_summary(m: dict) -> dict:
    return {
        "tmdb_id": m["id"], "title": m.get("title"), "year": _year(m.get("release_date")),
        "release_date": m.get("release_date") or None, "overview": m.get("overview"),
        "poster": TMDB_IMG + m["poster_path"] if m.get("poster_path") else None,
    }


async def search_collections(term: str) -> list[dict]:
    """TMDB collections ("The Lord of the Rings Collection") and keywords that
    act as universes ("marvel cinematic universe (mcu)")."""
    import asyncio
    cols, kws = await asyncio.gather(_tmdb("/search/collection", query=term), _tmdb("/search/keyword", query=term))
    out = [{"source": "collection", "id": c["id"], "name": c["name"], "overview": c.get("overview"),
            "poster": TMDB_IMG + c["poster_path"] if c.get("poster_path") else None}
           for c in cols.get("results", [])[:15]]
    out += [{"source": "keyword", "id": k["id"], "name": k["name"], "overview": "TMDB keyword — every movie tagged with it",
             "poster": None} for k in kws.get("results", [])[:10]]
    return out


async def collection(source: str, cid: int) -> dict:
    """Name, overview and member movies (oldest first) of a collection or keyword universe."""
    if source == "collection":
        c = await _tmdb(f"/collection/{cid}")
        parts = [_movie_summary(m) for m in c.get("parts", [])]
        info = {"name": c.get("name"), "overview": c.get("overview"),
                "poster": TMDB_IMG + c["poster_path"] if c.get("poster_path") else None}
    elif source == "keyword":
        k = await _tmdb(f"/keyword/{cid}")
        parts, page = [], 1
        while page <= 10:
            d = await _tmdb("/discover/movie", with_keywords=cid, sort_by="primary_release_date.asc",
                            include_adult="false", page=page)
            parts += [_movie_summary(m) for m in d.get("results", [])]
            if page >= d.get("total_pages", 1):
                break
            page += 1
        info = {"name": k.get("name"), "overview": "Every movie TMDB tags with this keyword.", "poster": None}
    else:
        raise ValueError("source must be 'collection' or 'keyword'")
    parts.sort(key=lambda m: (m["release_date"] is None, m["release_date"] or ""))
    return {"source": source, "id": cid, **info, "parts": parts}


# ------------------------------------------------- streaming services ----
TMDB_LOGO = "https://image.tmdb.org/t/p/w154"
# TMDB "network" ids, used for the "originals only" TV filter
ORIGINALS_NETWORK = {
    "netflix": 213, "apple tv": 2552, "amazon prime video": 1024, "prime video": 1024, "disney": 2739,
    "hulu": 453, "max": 3186, "hbo max": 3186, "paramount": 4330, "peacock": 3353,
}
_providers_cache: dict = {}


def _region() -> str:
    return (db.setting("streaming_region") or "US").upper()


def originals_network(name: str) -> int | None:
    """'Apple TV+' / 'Disney Plus' / 'Peacock Premium' -> the TMDB network that makes their originals."""
    import re
    n = re.sub(r"\s*(\+|\bplus\b|\bpremium\b)\s*$", "", name.lower().strip()).strip()
    return ORIGINALS_NETWORK.get(n)


# Free, ad-supported services (FAST). Ad tiers of paid services are caught by name ("... with Ads").
_AD_SUPPORTED = {"tubi", "tubi tv", "pluto tv", "freevee", "amazon freevee", "the roku channel", "roku channel", "plex",
                 "plex channel", "plex player", "crackle", "xumo play", "xumo", "sling freestream", "fandango at home free",
                 "vudu free", "popcornflix", "filmrise", "shout! factory tv", "redbox free live tv", "local now",
                 "youtube free", "youtube (free)", "distrotv", "cineverse", "kanopy with ads", "cwtv", "the cw"}
_ADS_RX = re.compile(r"\b(with ads|ads|ad-supported|ad supported|free with ads)\b", re.I)


def has_ads(name: str) -> bool:
    n = (name or "").strip().lower()
    return n in _AD_SUPPORTED or bool(_ADS_RX.search(n))


async def watch_providers() -> list[dict]:
    """Streaming services available in the configured region (movies + TV merged).
    Ad-supported services and ad tiers are left out unless the streaming_hide_ads setting is off."""
    out = await _all_watch_providers()
    if db.setting("streaming_hide_ads"):
        out = [p for p in out if not has_ads(p["name"])]
    return out


async def _all_watch_providers() -> list[dict]:
    import asyncio
    region = _region()
    hit = _providers_cache.get(region)
    if hit and hit[0] > time.time():
        return hit[1]
    mv, tv = await asyncio.gather(_tmdb("/watch/providers/movie", watch_region=region),
                                  _tmdb("/watch/providers/tv", watch_region=region))
    merged: dict[int, dict] = {}
    for kind, data in (("movie", mv), ("tv", tv)):
        for p in data.get("results", []):
            prio = (p.get("display_priorities") or {}).get(region, p.get("display_priority", 999))
            e = merged.setdefault(p["provider_id"], {
                "id": p["provider_id"], "name": p["provider_name"], "priority": prio,
                "logo": TMDB_LOGO + p["logo_path"] if p.get("logo_path") else None, "kinds": []})
            e["kinds"].append(kind)
            e["priority"] = min(e["priority"], prio)
    out = sorted(merged.values(), key=lambda p: (p["priority"], p["name"]))
    for p in out:
        p["originals"] = originals_network(p["name"]) is not None
    _providers_cache[region] = (time.time() + 12 * 3600, out)
    return out


_SORTS = {"popular": "popularity.desc", "newest": None, "rating": "vote_average.desc", "title": None}


async def discover(kind: str, provider_id: int, page: int = 1, sort: str = "popular",
                   originals_of: str | None = None) -> dict:
    """Movies or TV shows streaming on a service (flat-rate subscription) in the region.
    originals_of: provider name -> TV limited to that service's own network."""
    region = _region()
    params: dict = {"page": page, "watch_region": region, "include_adult": "false"}
    if kind == "tv" and originals_of and originals_network(originals_of):
        params["with_networks"] = originals_network(originals_of)
    else:
        params.update(with_watch_providers=provider_id, with_watch_monetization_types="flatrate")
    if sort == "newest":
        params["sort_by"] = "primary_release_date.desc" if kind == "movie" else "first_air_date.desc"
        today = time.strftime("%Y-%m-%d")
        params["primary_release_date.lte" if kind == "movie" else "first_air_date.lte"] = today
    elif sort == "rating":
        params.update(sort_by="vote_average.desc", **{"vote_count.gte": 300})
    elif sort == "title":
        params["sort_by"] = "title.asc" if kind == "movie" else "name.asc"
    else:
        params["sort_by"] = "popularity.desc"
    data = await _tmdb("/discover/movie" if kind == "movie" else "/discover/tv", **params)
    results = []
    for r in data.get("results", []):
        date = r.get("release_date") if kind == "movie" else r.get("first_air_date")
        results.append({
            "tmdb_id": r["id"], "title": r.get("title") or r.get("name"), "year": _year(date),
            "overview": r.get("overview"), "rating": r.get("vote_average"),
            "poster": TMDB_IMG + r["poster_path"] if r.get("poster_path") else None,
        })
    return {"page": data.get("page", page), "total_pages": min(data.get("total_pages", 1), 500),
            "total_results": data.get("total_results", 0), "results": results}


async def tvdb_id_for_tmdb_tv(tmdb_id: int) -> int | None:
    ids = await _tmdb(f"/tv/{tmdb_id}/external_ids")
    if ids.get("tvdb_id"):
        return int(ids["tvdb_id"])
    if ids.get("imdb_id"):
        s = await series_by_remote_id(ids["imdb_id"])
        if s:
            return s["tvdb_id"]
    return None


# ------------------------------------------------- TV: TMDB ----
# Each show also stores its TheTVDB and IMDb ids (from TMDB's external ids):
# indexers search by them and folder names use {tvdb-…}.

def _tmdb_tv_summary(r: dict) -> dict:
    return {"tmdb_id": r["id"], "tvdb_id": None, "title": r.get("name"), "year": _year(r.get("first_air_date")),
            "overview": r.get("overview"), "poster": TMDB_IMG + r["poster_path"] if r.get("poster_path") else None,
            "status": None}


async def _tmdb_find(external_id: str, source: str) -> int | None:
    d = await _tmdb(f"/find/{external_id}", external_source=source)
    res = d.get("tv_results") or []
    return res[0]["id"] if res else None


async def _tmdb_series_details(tmdb_id: int) -> dict:
    d = await _tmdb(f"/tv/{tmdb_id}", append_to_response="external_ids")
    ext = d.get("external_ids") or {}
    genres = [g.get("name", "").lower() for g in d.get("genres") or []]
    runtimes = d.get("episode_run_time") or []
    return {
        "tmdb_id": d["id"], "tvdb_id": ext.get("tvdb_id") or None, "imdb_id": ext.get("imdb_id") or None,
        "title": d.get("name"), "year": _year(d.get("first_air_date")), "overview": d.get("overview"),
        "poster": TMDB_IMG + d["poster_path"] if d.get("poster_path") else None,
        "runtime": runtimes[0] if runtimes else 0, "status": d.get("status"),
        "series_type": "anime" if "animation" in genres and d.get("original_language") == "ja" else "standard",
        "seasons": [s["season_number"] for s in d.get("seasons") or [] if s.get("season_number") is not None],
        "source": "tmdb",
    }


async def _tmdb_series_episodes(tmdb_id: int, seasons: list[int] | None = None) -> list[dict]:
    import asyncio
    if seasons is None:
        seasons = (await _tmdb_series_details(tmdb_id))["seasons"]
    sem = asyncio.Semaphore(5)

    async def one(n):
        async with sem:
            try:
                return n, (await _tmdb(f"/tv/{tmdb_id}/season/{n}")).get("episodes", [])
            except httpx.HTTPStatusError:
                return n, []
    eps, absolute = [], 0
    for n, rows in sorted(await asyncio.gather(*(one(n) for n in seasons))):
        for e in sorted(rows, key=lambda x: x.get("episode_number") or 0):
            if e.get("episode_number") is None:
                continue
            if n > 0:
                absolute += 1
            eps.append({"tvdb_id": None, "season": n, "episode": e["episode_number"],
                        "absolute": absolute if n > 0 else None, "title": e.get("name"),
                        "air_date": e.get("air_date") or None, "overview": e.get("overview")})
    return eps


async def search_series(term: str) -> list[dict]:
    t = term.strip().lower()
    if t.startswith("tvdb:") and t[5:].isdigit():
        return [await series_details(tvdb_id=int(t[5:]))]
    if t.startswith("tmdb:") and t[5:].isdigit():
        return [await series_details(tmdb_id=int(t[5:]))]
    year = None
    parts = term.rsplit(" ", 1)
    if len(parts) == 2 and parts[1].isdigit() and len(parts[1]) == 4:
        term, year = parts[0], parts[1]
    data = await _tmdb("/search/tv", query=term, **({"first_air_date_year": year} if year else {}))
    return [_tmdb_tv_summary(r) for r in data.get("results", [])[:20]]


async def series_details(tvdb_id: int | None = None, tmdb_id: int | None = None) -> dict:
    """Show details with both ids filled in where known."""
    if not tmdb_id and tvdb_id:
        tmdb_id = await _tmdb_find(str(tvdb_id), "tvdb_id")
        if not tmdb_id:
            raise MetadataError(f"TMDB doesn't know the show with TheTVDB id {tvdb_id}.")
    if not tmdb_id:
        raise MetadataError("No show id given")
    d = await _tmdb_series_details(int(tmdb_id))
    if not d["tvdb_id"] and tvdb_id:
        d["tvdb_id"] = tvdb_id
    return d


async def series_episodes(series: dict | int) -> list[dict]:
    """Episodes for a series row/details dict (an int is treated as a TheTVDB id)."""
    if isinstance(series, int):
        series = {"tvdb_id": series}
    tmdb_id = series.get("tmdb_id")
    if not tmdb_id and series.get("tvdb_id"):
        tmdb_id = await _tmdb_find(str(series["tvdb_id"]), "tvdb_id")
    if not tmdb_id:
        raise MetadataError("Can't load episodes: the show has no TMDB id")
    return await _tmdb_series_episodes(int(tmdb_id), series.get("seasons"))


async def series_by_remote_id(imdb_id: str) -> dict | None:
    """Look a show up by its IMDb id (tt...)."""
    tmdb = await _tmdb_find(imdb_id, "imdb_id")
    return await series_details(tmdb_id=tmdb) if tmdb else None
