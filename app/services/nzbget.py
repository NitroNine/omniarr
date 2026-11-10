"""NZBGet JSON-RPC client."""
from __future__ import annotations

import base64

import httpx

from .. import db


class NZBGetError(Exception):
    pass


async def call(method: str, *params):
    s = db.get_settings()
    url = s["nzbget_url"].rstrip("/") + "/jsonrpc"
    auth = (s["nzbget_username"], s["nzbget_password"]) if s["nzbget_username"] else None
    async with httpx.AsyncClient(timeout=30, auth=auth) as c:
        try:
            r = await c.post(url, json={"method": method, "params": list(params), "id": 1})
        except httpx.HTTPError as e:
            raise NZBGetError(f"Cannot reach NZBGet at {s['nzbget_url']}: {e}") from e
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
