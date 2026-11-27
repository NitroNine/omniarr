"""Deezer's public catalog API (api.deezer.com, no account or key): artists, albums, new releases, charts.

Deezer allows 50 requests per 5 seconds per IP; Omniarr stays well under that (one at a time, 120 ms apart).
The account login (ARL) is only used by deemix to download, and by `check_arl` to show who is logged in."""
from __future__ import annotations

import asyncio
import time

import httpx

API = "https://api.deezer.com"
_lock = asyncio.Lock()
_last = 0.0
_cache: dict[str, tuple[float, object]] = {}
TTL = 3600


class DeezerError(Exception):
    pass


async def _get(path: str, **params) -> dict:
    global _last
    key = path + "?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    hit = _cache.get(key)
    if hit and hit[0] > time.time():
        return hit[1]  # type: ignore[return-value]
    async with _lock:
        wait = 0.12 - (time.monotonic() - _last)
        if wait > 0:
            await asyncio.sleep(wait)
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(API + path, params=params)
        _last = time.monotonic()
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict) and data.get("error"):
        err = data["error"]
        if err.get("code") == 4:  # quota: wait and try once more
            await asyncio.sleep(5)
            return await _get(path, **params)
        raise DeezerError(err.get("message") or str(err))
    _cache[key] = (time.time() + TTL, data)
    if len(_cache) > 2000:
        for k in sorted(_cache, key=lambda k: _cache[k][0])[:500]:
            _cache.pop(k, None)
    return data


def _artist(a: dict) -> dict:
    return {"deezer_id": a["id"], "name": a.get("name"), "picture": a.get("picture_medium") or a.get("picture"),
            "albums": a.get("nb_album"), "fans": a.get("nb_fan")}


def _album(a: dict, artist: dict | None = None) -> dict:
    art = a.get("artist") or artist or {}
    return {"deezer_id": a["id"], "title": a.get("title"), "cover": a.get("cover_medium") or a.get("cover"),
            "cover_big": a.get("cover_xl") or a.get("cover_big"), "release_date": a.get("release_date") or None,
            "record_type": a.get("record_type") or "album", "explicit": bool(a.get("explicit_lyrics")),
            "tracks": a.get("nb_tracks"), "artist_id": art.get("id"), "artist": art.get("name")}


async def search_artists(term: str, limit: int = 12) -> list[dict]:
    return [_artist(a) for a in (await _get("/search/artist", q=term, limit=limit)).get("data", [])]


async def search_albums(term: str, limit: int = 24) -> list[dict]:
    return [_album(a) for a in (await _get("/search/album", q=term, limit=limit)).get("data", [])]


async def artist(deezer_id: int) -> dict:
    return _artist(await _get(f"/artist/{deezer_id}"))


async def artist_albums(deezer_id: int) -> list[dict]:
    """Every album, EP, single and compilation of an artist (newest first, as Deezer lists them)."""
    art = await _get(f"/artist/{deezer_id}")
    out, index = [], 0
    while index < 2000:
        d = await _get(f"/artist/{deezer_id}/albums", index=index, limit=100)
        out += [_album(a, art) for a in d.get("data", [])]
        if not d.get("next"):
            break
        index += 100
    return out


async def album(deezer_id: int) -> dict:
    a = await _get(f"/album/{deezer_id}")
    row = _album(a)
    row["genres"] = [g["name"] for g in (a.get("genres") or {}).get("data", [])]
    row["label"] = a.get("label")
    row["duration"] = a.get("duration")
    # the album endpoint's track list has no positions: they come in album order
    row["track_list"] = [{"n": t.get("track_position") or i, "disc": t.get("disk_number") or 1, "title": t.get("title"),
                          "duration": t.get("duration")} for i, t in enumerate((a.get("tracks") or {}).get("data", []), 1)]
    return row


async def editors_picks(limit: int = 30) -> list[dict]:
    """Deezer's editors' selection of recent albums (its "/editorial/0/releases" list has been empty for a while)."""
    return [_album(a) for a in (await _get("/editorial/0/selection", limit=limit)).get("data", [])]


async def chart_albums(limit: int = 30) -> list[dict]:
    return [_album(a) for a in (await _get("/chart/0/albums", limit=limit)).get("data", [])]


async def check_arl(arl: str) -> dict:
    """Who an ARL logs in as, and whether that account can download FLAC (Deezer HiFi / Premium)."""
    if not arl:
        return {"ok": False, "error": "No Deezer ARL set"}
    async with httpx.AsyncClient(timeout=20, cookies={"arl": arl}) as c:
        r = await c.get("https://www.deezer.com/ajax/gw-light.php",
                        params={"method": "deezer.getUserData", "input": "3", "api_version": "1.0", "api_token": ""})
    r.raise_for_status()
    user = (r.json().get("results") or {}).get("USER") or {}
    if not user.get("USER_ID"):
        return {"ok": False, "error": "Deezer didn't accept this ARL (expired or mistyped)"}
    opts = user.get("OPTIONS") or {}
    return {"ok": True, "name": user.get("BLOG_NAME") or "", "flac": bool(opts.get("web_lossless") or opts.get("mobile_lossless")),
            "hq": bool(opts.get("web_hq") or opts.get("mobile_hq")), "country": opts.get("license_country")}
