"""NZBGet JSON-RPC client."""
from __future__ import annotations

import base64

import httpx

from .. import db


class NZBGetError(Exception):
    pass


def _target(external: bool = False) -> tuple[str, tuple[str, str] | None, str]:
    """(url, auth, name) of the downloader in use: the built-in one, or a separate NZBGet."""
    s = db.get_settings()
    if s["download_client"] == "builtin" and not external:
        from . import downloader
        return downloader.url(), (downloader.USERNAME, downloader.control_password()), "the built-in downloader"
    auth = (s["nzbget_username"], s["nzbget_password"]) if s["nzbget_username"] else None
    return s["nzbget_url"].rstrip("/"), auth, f"NZBGet at {s['nzbget_url']}"


async def call(method: str, *params, external: bool = False):
    base, auth, name = _target(external)
    async with httpx.AsyncClient(timeout=30, auth=auth) as c:
        try:
            r = await c.post(base + "/jsonrpc", json={"method": method, "params": list(params), "id": 1})
        except httpx.HTTPError as e:
            if base.startswith("http://127.0.0.1"):
                raise NZBGetError("The built-in downloader isn't running yet. Add a Usenet server under "
                                  "Settings → Downloads, or check the Events log.") from e
            raise NZBGetError(f"Cannot reach {name}: {e}") from e
    if r.status_code == 401:
        raise NZBGetError("NZBGet rejected the username/password.")
    r.raise_for_status()
    body = r.json()
    if body.get("error"):
        raise NZBGetError(str(body["error"]))
    return body.get("result")


async def version() -> str:
    return await call("version")


async def add(nzb: bytes, name: str, category: str, priority: int = 0, dupe_key: str = "", dupe_score: int = 0) -> int:
    nzb_id = await call(
        "append", name if name.endswith(".nzb") else name + ".nzb",
        base64.b64encode(nzb).decode(), category, priority,
        False, False, dupe_key, dupe_score, "ALL", [])
    if not nzb_id or nzb_id <= 0:
        raise NZBGetError("NZBGet refused the NZB.")
    return nzb_id


async def queue() -> dict[int, dict]:
    return {g["NZBID"]: g for g in (await call("listgroups", 0) or [])}


async def history() -> dict[int, dict]:
    return {h["NZBID"]: h for h in (await call("history", False) or [])}


async def delete(nzb_id: int, from_history: bool = False) -> None:
    await call("editqueue", "HistoryDelete" if from_history else "GroupFinalDelete", "", [nzb_id])


async def status() -> dict:
    return await call("status") or {}


async def pause(paused: bool) -> None:
    await call("pausedownload" if paused else "resumedownload")


async def test_server(host: str, port: int, username: str, password: str, ssl: bool) -> str:
    """Connects and logs in to a news server (NZBGet's own "Test connection").
    Returns "" when it works, otherwise NZBGet's error message."""
    cert_level = 2 if ssl else 0  # strict | none
    return await call("testserver", host, int(port), username or "", password or "", bool(ssl), "", 30, cert_level) or ""
