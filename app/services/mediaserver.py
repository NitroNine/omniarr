"""Tell the media server (Silo, Jellyfin or Emby) about new files right after an import, so they show up within a
minute instead of at its next scheduled scan.

- Silo: its own API, `POST /api/v2/scan {library_id, path}` (a subtree scan of that title's folder), with an API key
  (`Authorization: Bearer sa_…`) of an admin account.
- Jellyfin / Emby: `POST /Library/Media/Updated` with the folder (what Sonarr and Radarr send), `X-Emby-Token`.

Paths: the server may see the library under another path than Omniarr's /data. An explicit mapping in Settings
wins; otherwise a folder is matched to a library by its root folder's name (Omniarr /data/TV/Show →
Silo library root /mnt/user/media/TV → /mnt/user/media/TV/Show)."""
from __future__ import annotations

import asyncio
import logging
import os
import time

import httpx

from .. import db

log = logging.getLogger("omniarr.mediaserver")
TYPES = {"silo": "Silo", "jellyfin": "Jellyfin", "emby": "Emby"}
_libs: tuple[float, list[dict]] | None = None  # (fetched, [{id, name, paths}])


class MediaServerError(RuntimeError):
    pass


def configured() -> bool:
    s = db.get_settings()
    return s.get("media_server_type") in TYPES and bool((s.get("media_server_url") or "").strip()) \
        and bool(s.get("media_server_api_key"))


def _base() -> str:
    return (db.setting("media_server_url") or "").strip().rstrip("/")


def _headers() -> dict:
    key = db.setting("media_server_api_key") or ""
    if db.setting("media_server_type") == "silo":
        return {"Authorization": f"Bearer {key}", "Accept": "application/json"}
    return {"X-Emby-Token": key, "Accept": "application/json"}


async def _call(method: str, path: str, json_body=None):
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as c:
            r = await c.request(method, _base() + path, headers=_headers(), json=json_body)
    except httpx.HTTPError as e:
        raise MediaServerError(f"{TYPES.get(db.setting('media_server_type'), 'The media server')} didn't answer at "
                               f"{_base()}: {type(e).__name__}") from None
    if r.status_code in (401, 403):
        raise MediaServerError("The media server refused the API key"
                               + (" (it needs an admin account's key)" if r.status_code == 403 else ""))
    if r.status_code >= 400:
        raise MediaServerError(f"The media server answered {r.status_code}: {r.text[:200]}")
    if "json" in r.headers.get("content-type", ""):
        return r.json()
    return r.text


async def libraries(fresh: bool = False) -> list[dict]:
    """[{id, name, paths}] of the server's libraries (10 min cache)."""
    global _libs
    if not fresh and _libs and time.time() - _libs[0] < 600:
        return _libs[1]
    if db.setting("media_server_type") == "silo":
        d = await _call("GET", "/api/v2/libraries")
        libs = [{"id": str(x["id"]), "name": x.get("name") or "", "paths": x.get("paths") or []}
                for x in (d or {}).get("items") or [] if x.get("enabled", True)]
    else:
        d = await _call("GET", "/Library/VirtualFolders")
        libs = [{"id": x.get("ItemId") or x.get("Name"), "name": x.get("Name") or "", "paths": x.get("Locations") or []}
                for x in d or []]
    _libs = (time.time(), libs)
    return libs


def _mapped(path: str) -> str | None:
    src = (db.setting("media_server_path_from") or "").rstrip("/")
    dst = (db.setting("media_server_path_to") or "").rstrip("/")
    if src and dst and (path == src or path.startswith(src + "/")):
        return dst + path[len(src):]
    return None


def server_path(path: str, libs: list[dict]) -> tuple[dict, str] | None:
    """(library, the path as the server sees it) for one of Omniarr's paths, or None."""
    path = os.path.normpath(path)
    mapped = _mapped(path)
    candidates = [mapped] if mapped else [path]
    for p in candidates:  # the same path, or the mapped one, inside a library root
        for lib in libs:
            for root in lib["paths"]:
                root = root.rstrip("/")
                if p == root or p.startswith(root + "/"):
                    return lib, p
    if mapped:
        return None
    parts = path.split("/")
    lower = [x.lower() for x in parts[:-1]]
    for lib in libs:  # match by the root folder's name (any case): /data/TV/Show ↔ /mnt/user/media/TV
        for root in lib["paths"]:
            name = os.path.basename(root.rstrip("/")).lower()
            if name and name in lower:
                i = len(lower) - 1 - lower[::-1].index(name)
                return lib, root.rstrip("/") + "/" + "/".join(parts[i + 1:])
    return None


async def scan(paths: list[str]) -> list[str]:
    """Ask the server to scan these folders. Returns what was sent; never raises (logged instead)."""
    if not configured():
        return []
    paths = list(dict.fromkeys(p for p in paths if p))
    sent: list[str] = []
    try:
        kind = db.setting("media_server_type")
        if kind == "silo":
            libs = await libraries()
            for p in paths:
                hit = server_path(p, libs) or server_path(p, await libraries(fresh=True))
                if not hit:
                    db.log(f"Silo has no library holding {p}; set the path mapping in Settings → Media server", "warn")
                    continue
                lib, sp = hit
                await _call("POST", "/api/v2/scan", {"library_id": lib["id"], "path": sp})
                sent.append(sp)
        else:
            try:
                libs = await libraries()
            except MediaServerError:
                libs = []
            ups = []
            for p in paths:
                hit = server_path(p, libs)
                sp = hit[1] if hit else (_mapped(os.path.normpath(p)) or p)
                ups.append({"Path": sp, "UpdateType": "Created"})
                sent.append(sp)
            if ups:
                await _call("POST", "/Library/Media/Updated", {"Updates": ups})
    except MediaServerError as e:
        db.log(f"Couldn't tell the media server about new files: {e}", "warn")
        return sent
    if sent:
        db.log(f"Asked {TYPES[db.setting('media_server_type')]} to scan " + ", ".join(sent))
    return sent


def scan_soon(paths: list[str]) -> None:
    """Fire and forget from the importer (the import itself never waits on the media server)."""
    if not configured():
        return
    try:
        asyncio.get_running_loop().create_task(scan(paths))
    except RuntimeError:
        pass


async def test() -> dict:
    if (db.setting("media_server_type") or "") not in TYPES:
        raise MediaServerError("Choose the media server first")
    if not _base():
        raise MediaServerError("Enter the media server's address")
    libs = await libraries(fresh=True)
    root_movies, root_tv = db.setting("root_movies") or "", db.setting("root_tv") or ""
    checks = []
    for label, root in (("Movies", root_movies), ("TV", root_tv)):
        if root:
            hit = server_path(os.path.join(root, "x"), libs)
            checks.append(f"{label} → {hit[0]['name']} ({os.path.dirname(hit[1])})" if hit else f"{label}: no matching library")
    return {"ok": True, "libraries": [lib["name"] for lib in libs], "checks": checks}
