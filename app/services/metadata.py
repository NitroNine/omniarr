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


async def movie_details(tmdb_id: int) -> dict:
    m = await _tmdb(f"/movie/{tmdb_id}", append_to_response="external_ids")
    return {
        "tmdb_id": m["id"], "imdb_id": m.get("imdb_id") or m.get("external_ids", {}).get("imdb_id"),
        "title": m["title"], "original_title": m.get("original_title"),
        "year": _year(m.get("release_date")), "overview": m.get("overview"),
        "poster": TMDB_IMG + m["poster_path"] if m.get("poster_path") else None,
        "runtime": m.get("runtime") or 0, "release_date": m.get("release_date"),
    }


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
