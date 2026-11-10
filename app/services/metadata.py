"""Metadata providers: The Movie Database (movies) and TheTVDB v4 (TV)."""
from __future__ import annotations

import time

import httpx

from .. import db

TMDB = "https://api.themoviedb.org/3"
TMDB_IMG = "https://image.tmdb.org/t/p/w342"
TVDB = "https://api4.thetvdb.com/v4"

_LANG3 = {"en": "eng", "fr": "fra", "de": "deu", "es": "spa", "it": "ita", "nl": "nld",
          "pt": "por", "sv": "swe", "da": "dan", "no": "nor", "fi": "fin", "ja": "jpn"}


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


async def watch_providers() -> list[dict]:
    """Streaming services available in the configured region (movies + TV merged)."""
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


# ---------------------------------------------------------------- TVDB ----
_tvdb_token: dict = {"token": None, "exp": 0, "key": None}


async def _tvdb_login(client: httpx.AsyncClient) -> str:
    key = db.setting("tvdb_api_key")
    if not key:
        raise MetadataError("TheTVDB API key is not set (Settings → Metadata).")
    if _tvdb_token["token"] and _tvdb_token["exp"] > time.time() and _tvdb_token["key"] == key:
        return _tvdb_token["token"]
    body = {"apikey": key}
    if db.setting("tvdb_pin"):
        body["pin"] = db.setting("tvdb_pin")
    r = await client.post(f"{TVDB}/login", json=body)
    if r.status_code == 401:
        raise MetadataError("TheTVDB rejected the API key / PIN.")
    r.raise_for_status()
    _tvdb_token.update(token=r.json()["data"]["token"], exp=time.time() + 25 * 86400, key=key)
    return _tvdb_token["token"]


async def _tvdb(path: str, **params) -> dict:
    async with httpx.AsyncClient(timeout=30) as c:
        token = await _tvdb_login(c)
        r = await c.get(TVDB + path, params=params, headers={"Authorization": f"Bearer {token}"})
        if r.status_code == 401:
            _tvdb_token["token"] = None
            token = await _tvdb_login(c)
            r = await c.get(TVDB + path, params=params, headers={"Authorization": f"Bearer {token}"})
    r.raise_for_status()
    return r.json()


def _lang3() -> str:
    lang = db.setting("metadata_language") or "en"
    return _LANG3.get(lang, lang if len(lang) == 3 else "eng")


async def search_series(term: str) -> list[dict]:
    if term.lower().startswith("tvdb:"):
        return [await series_details(int(term[5:]))]
    data = await _tvdb("/search", query=term, type="series", limit=20)
    lang = _lang3()
    out = []
    for s in data.get("data", []):
        title = (s.get("translations") or {}).get(lang) or s.get("name")
        overview = (s.get("overviews") or {}).get(lang) or s.get("overview")
        out.append({
            "tvdb_id": int(s.get("tvdb_id") or s["id"].split("-")[-1]), "title": title,
            "year": int(s["year"]) if str(s.get("year", "")).isdigit() else None,
            "overview": overview, "poster": s.get("image_url") or s.get("thumbnail"),
            "status": s.get("status"), "network": s.get("network"),
        })
    return out


async def series_by_remote_id(remote_id: str) -> dict | None:
    """Look a show up by IMDb (tt...) or TMDB id."""
    data = await _tvdb(f"/search/remoteid/{remote_id}")
    for hit in data.get("data") or []:
        if hit.get("series"):
            return await series_details(int(hit["series"]["id"]))
    return None


async def series_details(tvdb_id: int) -> dict:
    d = (await _tvdb(f"/series/{tvdb_id}/extended", meta="translations", short="true"))["data"]
    lang = _lang3()
    name, overview = d.get("name"), d.get("overview")
    for t in (d.get("translations") or {}).get("nameTranslations", []) or []:
        if t.get("language") == lang and t.get("name"):
            name = t["name"]
    for t in (d.get("translations") or {}).get("overviewTranslations", []) or []:
        if t.get("language") == lang and t.get("overview"):
            overview = t["overview"]
    imdb = tmdb = None
    for rid in d.get("remoteIds") or []:
        src = (rid.get("sourceName") or "").lower()
        if src == "imdb":
            imdb = rid.get("id")
        elif "themoviedb" in src or src == "tmdb":
            tmdb = int(rid["id"]) if str(rid.get("id", "")).isdigit() else None
    genres = [g.get("name", "").lower() for g in d.get("genres") or []]
    series_type = "anime" if "anime" in genres and d.get("originalLanguage") == "jpn" else "standard"
    return {
        "tvdb_id": d["id"], "title": name, "year": int(d["year"]) if str(d.get("year", "")).isdigit() else None,
        "overview": overview, "poster": d.get("image"), "imdb_id": imdb, "tmdb_id": tmdb,
        "runtime": d.get("averageRuntime") or 0, "status": (d.get("status") or {}).get("name"),
        "series_type": series_type,
    }


async def series_episodes(tvdb_id: int) -> list[dict]:
    lang = _lang3()
    page, eps = 0, []
    while True:
        try:
            data = (await _tvdb(f"/series/{tvdb_id}/episodes/default/{lang}", page=page))
        except httpx.HTTPStatusError:
            data = (await _tvdb(f"/series/{tvdb_id}/episodes/default", page=page))
        for e in data["data"].get("episodes", []):
            if e.get("seasonNumber") is None or e.get("number") is None:
                continue
            eps.append({
                "tvdb_id": e["id"], "season": e["seasonNumber"], "episode": e["number"],
                "absolute": e.get("absoluteNumber"), "title": e.get("name"),
                "air_date": e.get("aired"), "overview": e.get("overview"),
            })
        if not (data.get("links") or {}).get("next"):
            break
        page += 1
    return eps
