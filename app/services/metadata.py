"""Metadata provider: The Movie Database (TMDB) for movies and TV."""
from __future__ import annotations

import asyncio
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
    m = await _tmdb(f"/movie/{tmdb_id}", append_to_response="external_ids,release_dates")
    coll = m.get("belongs_to_collection") or {}
    dates = release_dates(m.get("release_dates"))
    return {
        "collection_id": coll.get("id"), "collection_name": coll.get("name"),
        "tmdb_id": m["id"], "imdb_id": m.get("imdb_id") or m.get("external_ids", {}).get("imdb_id"),
        "title": m["title"], "original_title": m.get("original_title"),
        "year": _year(m.get("release_date")), "overview": m.get("overview"),
        "poster": TMDB_IMG + m["poster_path"] if m.get("poster_path") else None,
        "runtime": m.get("runtime") or 0, "release_date": m.get("release_date"),
        "digital_release": home_date(dates), "physical_release": dates["physical"],
    }


def home_date(dates: dict) -> str | None:
    """Earliest digital or TV premiere: a TV-movie (Hallmark etc.) is on Usenet the day it airs."""
    ds = [d for d in (dates.get("digital"), dates.get("tv")) if d]
    return min(ds) if ds else None


# TMDB release types: 1 premiere, 2 limited theatrical, 3 theatrical, 4 digital, 5 physical (disc), 6 TV
_RELEASE_TYPES = {"premiere": (1,), "theatrical": (3, 2), "digital": (4,), "physical": (5,), "tv": (6,)}


def release_dates(data: dict | None, region: str | None = None) -> dict:
    """Earliest date of each release type for the region, else the US, else any country.
    Also returns which country each date came from and the age rating (certification)."""
    by_country = {r["iso_3166_1"]: r.get("release_dates") or [] for r in (data or {}).get("results", [])}
    region = (region or _region()).upper()
    order = [region] + [c for c in ("US",) if c != region]
    out: dict = {"region": region, "certification": None}
    for name, types in _RELEASE_TYPES.items():
        out[name], out[name + "_country"] = None, None
        for country in order + sorted(set(by_country) - set(order)):
            ds = sorted(d["release_date"][:10] for d in by_country.get(country, [])
                        if d.get("type") in types and d.get("release_date"))
            if ds:
                out[name], out[name + "_country"] = ds[0], country
                break
    for country in order:
        cert = next((d.get("certification") for d in by_country.get(country, []) if d.get("certification")), None)
        if cert:
            out["certification"] = cert
            break
    return out


def _person(p: dict, role: str | None = None) -> dict:
    return {"id": p.get("id"), "name": p.get("name"), "role": role,
            "photo": TMDB_FACE + p["profile_path"] if p.get("profile_path") else None}


def _trailer(videos: dict | None) -> str | None:
    vids = [v for v in (videos or {}).get("results", []) if v.get("site") == "YouTube" and v.get("key")]
    vids.sort(key=lambda v: (v.get("type") != "Trailer", not v.get("official"), v.get("published_at") or ""))
    return f"https://www.youtube.com/watch?v={vids[0]['key']}" if vids else None


def _streaming(wp: dict | None) -> list[dict]:
    reg = ((wp or {}).get("results") or {}).get(_region()) or {}
    return [{"id": p["provider_id"], "name": p["provider_name"],
             "logo": TMDB_LOGO + p["logo_path"] if p.get("logo_path") else None}
            for p in reg.get("flatrate") or [] if not has_ads(p.get("provider_name", ""))
            or not db.setting("streaming_hide_ads")]


_info_cache: dict = {}


async def _cached(key: str, fn):
    hit = _info_cache.get(key)
    if hit and hit[0] > time.time():
        return hit[1]
    val = await fn()
    _info_cache[key] = (time.time() + 6 * 3600, val)
    if len(_info_cache) > 3000:  # Christmas lists look up keywords / services of a few hundred titles
        for k in sorted(_info_cache, key=lambda k: _info_cache[k][0])[:500]:
            _info_cache.pop(k, None)
    return val


async def movie_info(tmdb_id: int) -> dict:
    """Everything for a movie's title page: facts, cast and crew, release dates, trailer, streaming."""
    async def fetch():
        m = await _tmdb(f"/movie/{tmdb_id}",
                        append_to_response="credits,release_dates,videos,external_ids,watch/providers")
        crew = (m.get("credits") or {}).get("crew") or []
        coll = m.get("belongs_to_collection") or {}
        return {
            "kind": "movie", "tmdb_id": m["id"], "imdb_id": m.get("imdb_id"), "title": m.get("title"),
            "original_title": m.get("original_title"), "tagline": m.get("tagline") or None,
            "overview": m.get("overview"), "year": _year(m.get("release_date")),
            "poster": TMDB_POSTER + m["poster_path"] if m.get("poster_path") else None,
            "backdrop": TMDB_BACKDROP + m["backdrop_path"] if m.get("backdrop_path") else None,
            "genres": [g["name"] for g in m.get("genres") or []], "runtime": m.get("runtime") or None,
            "rating": round(m["vote_average"], 1) if m.get("vote_count") else None, "votes": m.get("vote_count") or 0,
            "status": m.get("status"), "language": m.get("original_language"),
            "budget": m.get("budget") or None, "revenue": m.get("revenue") or None,
            "studios": [c["name"] for c in m.get("production_companies") or []][:4],
            "directors": [_person(c) for c in crew if c.get("job") == "Director"],
            "writers": [_person(c, c.get("job")) for c in crew if c.get("department") == "Writing"][:4],
            "cast": [_person(c, c.get("character")) for c in ((m.get("credits") or {}).get("cast") or [])[:24]],
            "dates": release_dates(m.get("release_dates")),
            "trailer": _trailer(m.get("videos")), "streaming": _streaming(m.get("watch/providers")),
            "collection": {"id": coll["id"], "name": coll.get("name")} if coll.get("id") else None,
        }
    return await _cached(f"movie:{tmdb_id}:{_region()}", fetch)


async def tv_info(tmdb_id: int) -> dict:
    """Everything for a show's title page: facts, creators, cast, seasons, trailer, streaming."""
    async def fetch():
        d = await _tmdb(f"/tv/{tmdb_id}",
                        append_to_response="aggregate_credits,content_ratings,videos,external_ids,watch/providers")
        ratings = {r["iso_3166_1"]: r.get("rating") for r in (d.get("content_ratings") or {}).get("results", [])}
        cast = []
        for c in ((d.get("aggregate_credits") or {}).get("cast") or [])[:24]:
            roles = c.get("roles") or []
            cast.append({**_person(c, roles[0].get("character") if roles else None),
                         "episodes": c.get("total_episode_count")})
        nxt = d.get("next_episode_to_air") or {}
        ext = d.get("external_ids") or {}
        return {
            "kind": "tv", "tmdb_id": d["id"], "tvdb_id": ext.get("tvdb_id"), "imdb_id": ext.get("imdb_id"),
            "title": d.get("name"), "original_title": d.get("original_name"), "tagline": d.get("tagline") or None,
            "overview": d.get("overview"), "year": _year(d.get("first_air_date")),
            "poster": TMDB_POSTER + d["poster_path"] if d.get("poster_path") else None,
            "backdrop": TMDB_BACKDROP + d["backdrop_path"] if d.get("backdrop_path") else None,
            "genres": [g["name"] for g in d.get("genres") or []],
            "runtime": (d.get("episode_run_time") or [None])[0],
            "rating": round(d["vote_average"], 1) if d.get("vote_count") else None, "votes": d.get("vote_count") or 0,
            "status": d.get("status"), "language": d.get("original_language"),
            "certification": ratings.get(_region()) or ratings.get("US"),
            "networks": [n["name"] for n in d.get("networks") or []],
            "creators": [_person(c) for c in d.get("created_by") or []],
            "first_air": d.get("first_air_date") or None, "last_air": d.get("last_air_date") or None,
            "next_episode": {"season": nxt.get("season_number"), "episode": nxt.get("episode_number"),
                             "name": nxt.get("name"), "air_date": nxt.get("air_date")} if nxt else None,
            "seasons": [{"season": s.get("season_number"), "name": s.get("name"), "episodes": s.get("episode_count"),
                         "air_date": s.get("air_date")} for s in d.get("seasons") or []],
            "number_of_seasons": d.get("number_of_seasons"), "number_of_episodes": d.get("number_of_episodes"),
            "cast": cast, "trailer": _trailer(d.get("videos")), "streaming": _streaming(d.get("watch/providers")),
        }
    return await _cached(f"tv:{tmdb_id}:{_region()}", fetch)


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
TMDB_POSTER = "https://image.tmdb.org/t/p/w500"
TMDB_BACKDROP = "https://image.tmdb.org/t/p/w1280"
TMDB_FACE = "https://image.tmdb.org/t/p/w185"
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


# Ad tiers of paid services ("Netflix Standard with Ads", "Amazon Prime Video with Ads"). Free
# services such as Tubi or Pluto TV stay: they have no ad-free plan to prefer.
_ADS_RX = re.compile(r"\bwith ads\b", re.I)


def has_ads(name: str) -> bool:
    return bool(_ADS_RX.search(name or ""))


async def watch_providers() -> list[dict]:
    """Streaming services available in the configured region (movies + TV merged).
    Ad tiers of paid services ("... with Ads") are left out unless the streaming_hide_ads setting is off."""
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
                   originals_of: str | None = None, released: bool = False, since: str | None = None) -> dict:
    """Movies or TV shows streaming on a service (flat-rate subscription) in the region.
    originals_of: provider name -> TV limited to that service's own network.
    released: only titles whose release (movies) or first air date (TV) has passed."""
    region = _region()
    params: dict = {"page": page, "watch_region": region, "include_adult": "false"}
    if kind == "tv" and originals_of and originals_network(originals_of):
        params["with_networks"] = originals_network(originals_of)
    else:
        params.update(with_watch_providers=provider_id, with_watch_monetization_types="flatrate")
    if since:
        params["primary_release_date.gte" if kind == "movie" else "first_air_date.gte"] = since
    if sort == "newest" or released:
        params["primary_release_date.lte" if kind == "movie" else "first_air_date.lte"] = time.strftime("%Y-%m-%d")
    if sort == "newest":
        params["sort_by"] = "primary_release_date.desc" if kind == "movie" else "first_air_date.desc"
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
            "overview": r.get("overview"), "rating": r.get("vote_average"), "date": date or None,
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


# ------------------------------------------------------- Christmas movies --
_XMAS_TITLE = re.compile(r"christmas|xmas|santa|st\.? nick|noel|mistletoe|merry|jingle|sleigh|yule|reindeer|"
                         r"\bnorth pole|'?tis the season|snowman|nutcracker|holly\b|navidad|candy cane|gingerbread|"
                         r"\bholiday", re.I)


def _xmas_keyword(name: str) -> bool:
    n = name.lower()
    return "christmas" in n or "santa" in n or n in ("holiday", "holidays", "holiday season", "holiday romance",
                                                       "holiday movie", "winter holiday")


def _countdown_season(date: str | None) -> bool:
    """Hallmark's Countdown to Christmas: from mid-October every new premiere is a Christmas movie."""
    return bool(date) and date[5:] >= "10-17"


async def is_christmas(r: dict, hallmark: bool) -> bool:
    if _XMAS_TITLE.search(r.get("title") or ""):
        return True
    if hallmark and _countdown_season(r.get("date")):
        return True
    try:
        kw = await _cached(f"kw:{r['tmdb_id']}", lambda: _tmdb(f"/movie/{r['tmdb_id']}/keywords"))
    except Exception:  # noqa: BLE001
        return False
    return any(_xmas_keyword(k.get("name", "")) for k in kw.get("keywords", []))


async def discover_christmas(provider_id: int, provider_name: str, sort: str = "popular",
                             released: bool = False) -> dict:
    """Christmas movies on a service from this year on: TMDB "christmas"/"holiday" keywords, a Christmas word in
    the title, or (Hallmark) a premiere in the Countdown to Christmas season. One list, no paging."""
    since = time.strftime("%Y-01-01")
    found: list[dict] = []
    page, pages = 1, 1
    while page <= min(pages, 15):
        d = await discover("movie", provider_id, page, sort, None, released, since=since)
        found += d["results"]
        pages = d["total_pages"]
        page += 1
    hallmark = "hallmark" in (provider_name or "").lower()
    sem = asyncio.Semaphore(8)

    async def check(r):
        async with sem:
            return await is_christmas(r, hallmark)
    keep = await asyncio.gather(*(check(r) for r in found))
    results = [r for r, k in zip(found, keep) if k]
    return {"page": 1, "total_pages": 1, "total_results": len(results), "results": results}


async def _xmas_keyword_ids() -> str:
    """TMDB keyword ids for Christmas / holiday ("christmas", "christmas movie", "holiday"...), OR-ed for discover."""
    async def find():
        ids = []
        for q in ("christmas", "holiday"):
            for page in (1, 2):
                d = await _tmdb("/search/keyword", query=q, page=page)
                ids += [k["id"] for k in d.get("results", []) if _xmas_keyword(k.get("name", ""))]
                if page >= d.get("total_pages", 1):
                    break
        return "|".join(str(i) for i in dict.fromkeys(ids))
    return await _cached("xmas-keywords", find)


async def services_for(tmdb_id: int) -> list[str]:
    """Subscription streaming services a movie is on in the streaming region."""
    region = _region()
    d = await _cached(f"wp:{tmdb_id}", lambda: _tmdb(f"/movie/{tmdb_id}/watch/providers"))
    us = (d.get("results") or {}).get(region) or {}
    names = [p["provider_name"] for p in us.get("flatrate", []) + us.get("free", []) + us.get("ads", [])]
    if db.setting("streaming_hide_ads"):
        names = [n for n in names if not has_ads(n)]
    return list(dict.fromkeys(names))


async def discover_christmas_all(released: bool = False) -> dict:
    return await _cached(f"xmas-all:{_region()}:{released}:{time.strftime('%Y-%m-%d')}",
                         lambda: _discover_christmas_all(released))


async def _discover_christmas_all(released: bool) -> dict:
    """Christmas movies from this year on (English-language ones while english_only is on), from everywhere: TMDB's Christmas/holiday keywords, plus US TV premieres
    (Hallmark, Lifetime, GAF... often have no keywords yet) with a Christmas word in the title or keywords.
    Each one lists the streaming services it is on."""
    since, today = time.strftime("%Y-01-01"), time.strftime("%Y-%m-%d")
    found: dict[int, dict] = {}

    async def pages(params: dict, limit: int, check: bool):
        page, total = 1, 1
        while page <= min(total, limit):
            d = await _tmdb("/discover/movie", page=page, include_adult="false", **params)
            total = d.get("total_pages", 1)
            for r in d.get("results", []):
                if english and r.get("original_language") not in (None, "", "en"):
                    continue
                date = r.get("release_date") or None
                item = {"tmdb_id": r["id"], "title": r.get("title"), "year": _year(date), "overview": r.get("overview"),
                        "rating": r.get("vote_average"), "date": date,
                        "poster": TMDB_IMG + r["poster_path"] if r.get("poster_path") else None, "_check": check}
                if r["id"] not in found or not check:
                    found[r["id"]] = item
            page += 1
    upto = {"primary_release_date.lte": today} if released else {}
    english = bool(db.setting("english_only"))
    kws = await _xmas_keyword_ids()
    if kws:
        await pages({"with_keywords": kws, "primary_release_date.gte": since, "sort_by": "popularity.desc", **upto}, 40, False)
    await pages({"region": "US", "with_release_type": "6", "release_date.gte": since, "sort_by": "popularity.desc",
                 **({"release_date.lte": today} if released else {})}, 40, True)  # TV movies are rarely "popular": read them all
    sem = asyncio.Semaphore(8)

    async def keep(r):
        async with sem:
            if r.pop("_check") and not await is_christmas(r, False):
                return False
            try:
                r["services"] = await services_for(r["tmdb_id"])
            except Exception:  # noqa: BLE001
                r["services"] = []
            return True
    rows = list(found.values())
    ok = await asyncio.gather(*(keep(r) for r in rows))
    results = sorted((r for r, k in zip(rows, ok) if k), key=lambda r: r.get("date") or "9999", reverse=True)
    return {"page": 1, "total_pages": 1, "total_results": len(results), "results": results}
