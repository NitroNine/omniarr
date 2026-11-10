"""Omniarr web app: REST API + background scheduler + static UI."""
from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import auth, collections_, db, requests_, decision, importer, library, scanner, search, trash
from .services import downloader, metadata, newznab, nzbget

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("omniarr")
STATIC = os.path.join(os.path.dirname(__file__), "static")


# ------------------------------------------------------------- scheduler --
_running: dict[str, bool] = {}
_last_run: dict[str, float] = {}


async def _job(name: str, coro_fn):
    if _running.get(name):
        return
    _running[name] = True
    try:
        await coro_fn()
    except Exception as e:  # noqa: BLE001
        db.log(f"Task {name} failed: {e}", "error")
    finally:
        _running[name] = False
        _last_run[name] = time.time()


async def _refresh_all_series():
    for s in db.q("SELECT id FROM series WHERE last_refresh IS NULL OR last_refresh < datetime('now', ?)",
                  (f"-{int(db.setting('series_refresh_hours'))} hours",)):
        await library.refresh_series(s["id"])


async def _trash_auto():
    synced = db.one("SELECT value FROM settings WHERE key='_trash_synced'")
    hours = int(db.setting("trash_auto_sync_hours") or 0)
    if not synced or (hours and db.one("SELECT ? < datetime('now', ?) old", (synced["value"], f"-{hours} hours"))["old"]):
        await trash.sync()


async def scheduler():
    await asyncio.sleep(3)
    while True:
        s = db.get_settings()
        now = time.time()
        jobs = [
            ("downloader", 30, downloader.ensure),  # keeps the built-in NZBGet running
            ("downloads", 30, importer.process_downloads),
            ("trash", 3600, _trash_auto),
            ("series_refresh", 3600, _refresh_all_series),
            ("collections", 3600, collections_.refresh_due),
            ("movie_refresh", 3600, library.refresh_due_movies),
            ("rss", int(s["rss_interval_minutes"]) * 60, search.rss_sync),
            ("missing", int(s["search_interval_minutes"]) * 60, search.missing_search),
        ]
        for name, every, fn in jobs:
            if every > 0 and now - _last_run.get(name, 0) >= every:
                if name in ("rss", "missing") and not trash.ensure_loaded().loaded:
                    continue
                asyncio.create_task(_job(name, fn))
        await asyncio.sleep(10)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.connect()
    auth.bootstrap_from_env()
    trash.ensure_loaded()
    try:
        await downloader.ensure()
    except Exception as e:  # noqa: BLE001
        log.warning("Built-in downloader didn't start: %s", e)
    task = asyncio.create_task(scheduler())
    log.info("Omniarr started — config dir %s", db.CONFIG_DIR)
    yield
    task.cancel()
    await asyncio.to_thread(downloader.stop)


app = FastAPI(title="Omniarr", lifespan=lifespan)


# ------------------------------------------------------------------ auth --
@app.middleware("http")
async def require_login(request: Request, call_next):
    """Every /api call needs a logged-in user; non-admins only get the routes in auth.USER_ROUTES."""
    path = request.url.path
    request.state.user = None
    if path.startswith("/api/") and not path.startswith("/api/auth/") or path in ("/api/auth/me", "/api/auth/logout",
                                                                                   "/api/auth/password"):
        user = auth.user_for_token(request.cookies.get(auth.SESSION_COOKIE))
        if not user:
            return JSONResponse({"detail": "Please log in", "login": True}, status_code=401)
        if user["role"] != "admin" and not auth.user_may(request.method, path):
            return JSONResponse({"detail": "Only an admin can do that"}, status_code=403)
        request.state.user = user
    resp = await call_next(request)
    if not path.startswith("/api/") and not path.endswith((".png", ".svg", ".ico")):
        # the UI has no build step / versioned file names: make browsers re-check it, so updates show at once
        resp.headers["Cache-Control"] = "no-cache"
    return resp


def _user(request: Request) -> dict:
    return request.state.user


def _is_admin(request: Request) -> bool:
    u = getattr(request.state, "user", None)
    return bool(u and u["role"] == "admin")


@app.exception_handler(PermissionError)
async def forbidden(_req, exc):
    return JSONResponse({"detail": str(exc)}, status_code=403)


@app.exception_handler(metadata.MetadataError)
@app.exception_handler(nzbget.NZBGetError)
@app.exception_handler(ValueError)
@app.exception_handler(KeyError)
async def friendly_errors(_req, exc):
    msg = exc.args[0] if isinstance(exc, KeyError) and exc.args else str(exc)
    return JSONResponse({"detail": msg}, status_code=400)


@app.get("/health")
def health():
    return {"ok": True}


# ------------------------------------------------------------------ auth --
class LoginIn(BaseModel):
    username: str
    password: str


def _set_session(resp: JSONResponse, token: str) -> JSONResponse:
    resp.set_cookie(auth.SESSION_COOKIE, token, max_age=auth.SESSION_DAYS * 86400, httponly=True, samesite="lax",
                    secure=os.environ.get("OMNIARR_SECURE_COOKIES", "").lower() in ("1", "true", "yes"))
    return resp


@app.get("/api/auth/status")
def auth_status(request: Request):
    return {"setup_needed": auth.user_count() == 0,
            "user": auth.user_for_token(request.cookies.get(auth.SESSION_COOKIE))}


@app.post("/api/auth/setup")
def auth_setup(body: LoginIn):
    """First run only: create the admin account."""
    if auth.user_count() > 0:
        raise PermissionError("Setup is already done")
    auth.create_user(body.username, body.password, role="admin")
    token, user = auth.login(body.username, body.password, "setup")
    return _set_session(JSONResponse({"user": user}), token)


@app.post("/api/auth/login")
def auth_login(body: LoginIn, request: Request):
    token, user = auth.login(body.username, body.password, request.client.host if request.client else "?")
    return _set_session(JSONResponse({"user": user}), token)


@app.post("/api/auth/logout")
def auth_logout(request: Request):
    auth.logout(request.cookies.get(auth.SESSION_COOKIE))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.SESSION_COOKIE)
    return resp


@app.get("/api/auth/me")
def auth_me(request: Request):
    u = dict(_user(request))
    if u["role"] == "admin":
        u["pending_requests"] = requests_.pending_count()
    return u


@app.post("/api/auth/password")
def auth_password(body: dict, request: Request):
    auth.change_own_password(_user(request), body.get("current", ""), body.get("new", ""))
    return {"ok": True}


# ----------------------------------------------------------------- users --
class UserIn(BaseModel):
    username: str
    password: str
    role: str = "user"
    auto_approve: bool = False
    allow_uhd: bool = False


@app.get("/api/users")
def users_list():
    return auth.list_users()


@app.post("/api/users")
def users_create(body: UserIn):
    return auth.create_user(body.username, body.password, body.role, body.auto_approve, body.allow_uhd)


@app.put("/api/users/{uid}")
def users_update(uid: int, body: dict, request: Request):
    return auth.update_user(uid, body, _user(request))


@app.delete("/api/users/{uid}")
def users_delete(uid: int, request: Request):
    auth.delete_user(uid, _user(request))
    return {"ok": True}


# -------------------------------------------------------------- requests --
@app.get("/api/requests")
def requests_list(request: Request, status: str | None = None):
    return requests_.list_for(_user(request), status)


@app.post("/api/requests")
async def requests_create(body: dict, request: Request):
    return await requests_.create(_user(request), body)


@app.post("/api/requests/{rid}/approve")
async def requests_approve(rid: int, request: Request, body: dict | None = None):
    return await requests_.approve(rid, _user(request), tier=(body or {}).get("tier"))


@app.post("/api/requests/{rid}/decline")
def requests_decline(rid: int, request: Request, body: dict | None = None):
    return requests_.decline(rid, _user(request), (body or {}).get("note"))


@app.delete("/api/requests/{rid}")
def requests_cancel(rid: int, request: Request):
    requests_.cancel(rid, _user(request))
    return {"ok": True}


# -------------------------------------------------------------- settings --
SECRET_KEYS = {"tmdb_api_key", "nzbget_password"}


@app.get("/api/settings")
def get_settings():
    s = db.get_settings()
    out = {k: v for k, v in s.items() if not k.startswith("_")}
    for k in SECRET_KEYS:
        out[k] = "********" if out.get(k) else ""
    out["_trash_synced"] = s.get("_trash_synced")
    return out


@app.put("/api/settings")
async def put_settings(values: dict):
    values = {k: v for k, v in values.items() if not (k in SECRET_KEYS and v == "********") and not k.startswith("_")}
    db.save_settings(values)
    asyncio.create_task(_job("downloader", downloader.ensure))
    return get_settings()


@app.get("/api/trash/profiles")
def trash_profiles():
    st = trash.ensure_loaded()
    return {"movie": st.list_profiles("movie"), "tv": st.list_profiles("tv"),
            "naming": {"movie": st.naming["radarr"], "tv": st.naming["sonarr"]}, "loaded": st.loaded}


@app.post("/api/trash/sync")
async def trash_sync():
    return await trash.sync()


@app.get("/api/trash/profile/{kind}/{tier}")
def trash_profile_detail(kind: str, tier: str):
    p = decision.profile_for(kind, tier)
    st = trash.ensure_loaded()
    app_ = "radarr" if kind == "movie" else "sonarr"
    return {"name": p.name, "cutoff": p.cutoff, "min_format_score": p.min_format_score,
            "cutoff_format_score": p.cutoff_format_score,
            "qualities": [{"name": i["name"], "allowed": i.get("allowed")} for i in p.items],
            "formats": sorted(({"name": st.cfs[app_][t].name, "score": s} for t, s in p.format_scores.items()),
                              key=lambda x: -x["score"])}


# --------------------------------------------------------- quality limits --
@app.get("/api/quality-limits")
def get_quality_limits():
    """Per library: the TRaSH defaults plus the user's overrides."""
    ov = db.setting("quality_overrides") or {}
    out = {}
    for kind in ("movie", "tv"):
        for tier in library.TIERS:
            key = decision.library_key(kind, tier)
            try:
                base = decision.profile_for(kind, tier, apply_overrides=False)
                eff = decision.profile_for(kind, tier)
            except KeyError as e:
                out[key] = {"error": str(e.args[0])}
                continue
            out[key] = {
                "profile": base.name,
                "qualities": [{"name": it["name"], "items": it.get("items", []),
                               "trash_allowed": bool(it.get("allowed")),
                               "allowed": bool(eff_it.get("allowed"))}
                              for it, eff_it in zip(base.items, eff.items)],
                "trash": {"cutoff": base.cutoff, "cutoff_score": base.cutoff_format_score,
                          "upgrades": base.upgrade_allowed, "min_upgrade_score": base.min_upgrade_format_score},
                "effective": {"cutoff": eff.cutoff, "cutoff_score": eff.cutoff_format_score,
                              "upgrades": eff.upgrade_allowed},
                "override": ov.get(key) or {},
            }
    return out


@app.put("/api/quality-limits/{key}")
def put_quality_limits(key: str, body: dict):
    if key not in {decision.library_key(k, t) for k in ("movie", "tv") for t in library.TIERS}:
        raise HTTPException(404)
    ov = dict(db.setting("quality_overrides") or {})
    if body.get("reset"):
        ov.pop(key, None)
    else:
        clean = {}
        if isinstance(body.get("allowed"), list):
            if not body["allowed"]:
                raise ValueError("Allow at least one quality")
            clean["allowed"] = [str(x) for x in body["allowed"]]
        if body.get("cutoff"):
            if "allowed" in clean and body["cutoff"] not in clean["allowed"]:
                raise ValueError("The 'upgrade until' quality must be one of the allowed qualities")
            clean["cutoff"] = str(body["cutoff"])
        if "upgrades" in body:
            clean["upgrades"] = bool(body["upgrades"])
        if body.get("cutoff_score") not in (None, ""):
            clean["cutoff_score"] = int(body["cutoff_score"])
        ov[key] = clean
    db.save_settings({"quality_overrides": ov})
    return get_quality_limits()[key]


# ----------------------------------------------------------- folder picker --
# Admin only (not in auth.USER_ROUTES). Paths are as the container sees them:
# /data is the Data share from the Unraid template.
def _existing_dir(path: str) -> str:
    base = os.path.abspath(path or ("/data" if os.path.isdir("/data") else "/"))
    while not os.path.isdir(base) and base != "/":  # a folder that doesn't exist yet: start at its parent
        base = os.path.dirname(base)
    return base


@app.get("/api/fs/browse")
def browse_folders(path: str = ""):
    base = _existing_dir(path)
    try:
        folders = sorted((e.name for e in os.scandir(base)
                          if e.is_dir(follow_symlinks=True) and not e.name.startswith((".", "@"))), key=str.lower)
    except PermissionError:
        raise HTTPException(403, f"No permission to read {base}")
    return {"path": base, "parent": None if base == "/" else os.path.dirname(base), "folders": folders[:2000]}


@app.post("/api/fs/mkdir")
def make_folder(body: dict):
    parent, name = _existing_dir(body.get("path", "")), (body.get("name") or "").strip()
    if not name or "/" in name or name in (".", ".."):
        raise ValueError("Give the new folder a name (no slashes)")
    new = os.path.join(parent, name)
    os.makedirs(new, exist_ok=True)
    return {"path": new}


# --------------------------------------------------------- library import --
@app.get("/api/import/scan")
def import_scan(kind: str = "movie"):
    if kind not in ("movie", "tv"):
        raise HTTPException(400)
    return scanner.scan(kind)


@app.post("/api/import/match")
async def import_match(body: dict):
    return await scanner.match_many(body.get("items", [])[:50])


@app.post("/api/import")
async def import_start(body: dict):
    if scanner.JOB["running"]:
        raise ValueError("An import is already running")
    items = [i for i in body.get("items", []) if i.get("id")]
    if not items:
        raise ValueError("Nothing selected")
    asyncio.create_task(scanner.run_import(items))
    return {"started": len(items)}


@app.get("/api/import/status")
def import_status():
    return scanner.JOB


@app.post("/api/movies/{mid}/rescan")
async def rescan_movie(mid: int, rename: bool = False):
    return await scanner.rescan_movie(mid, rename=rename)


@app.post("/api/series/{sid}/rescan")
async def rescan_series(sid: int, rename: bool = False):
    return await scanner.rescan_series(sid, rename=rename)


# -------------------------------------------------------------- indexers --
class IndexerIn(BaseModel):
    name: str
    url: str
    apikey: str
    movie_categories: str = "2000"
    tv_categories: str = "5000"
    enabled: bool = True
    priority: int = 25


@app.get("/api/indexers")
def list_indexers():
    rows = db.q("SELECT * FROM indexers ORDER BY priority, id")
    for r in rows:
        r["apikey"] = r["apikey"][:4] + "…" if r["apikey"] else ""
    return rows


@app.post("/api/indexers")
def add_indexer(ix: IndexerIn):
    return {"id": db.insert("indexers", {**ix.model_dump(), "enabled": int(ix.enabled)})}


@app.put("/api/indexers/{iid}")
def edit_indexer(iid: int, ix: dict):
    # an empty or masked ("abcd…") API key means "keep the saved one"
    allowed = {k: v for k, v in ix.items() if k in IndexerIn.model_fields
               and not (k == "apikey" and ("…" in str(v) or not str(v).strip()))}
    for k in ("name", "url"):
        if k in allowed:
            allowed[k] = str(allowed[k]).strip()
            if not allowed[k]:
                raise HTTPException(400, f"The indexer {k} can't be empty")
    if "priority" in allowed:
        allowed["priority"] = int(allowed["priority"])
    if not db.one("SELECT 1 FROM indexers WHERE id=?", (iid,)):
        raise HTTPException(404)
    if "enabled" in allowed:
        allowed["enabled"] = int(bool(allowed["enabled"]))
    db.update("indexers", iid, allowed)
    return {"ok": True}


@app.delete("/api/indexers/{iid}")
def delete_indexer(iid: int):
    db.ex("DELETE FROM indexers WHERE id=?", (iid,))
    return {"ok": True}


@app.post("/api/indexers/caps")
async def indexer_caps(body: dict):
    """Categories offered by an indexer: from the URL and key typed in the form, or a saved
    indexer (id) using its stored key when the key field was left empty."""
    url, key = (body.get("url") or "").strip(), (body.get("apikey") or "").strip()
    if body.get("id") and (not key or "…" in key):
        saved = db.one("SELECT url, apikey FROM indexers WHERE id=?", (int(body["id"]),))
        if not saved:
            raise HTTPException(404)
        url, key = url or saved["url"], saved["apikey"]
    if not url or not key:
        raise HTTPException(400, "Enter the URL and API key first")
    try:
        return await newznab.caps(url, key)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Couldn't load categories: {e}") from e


@app.post("/api/indexers/{iid}/test")
async def test_indexer(iid: int):
    ix = db.one("SELECT * FROM indexers WHERE id=?", (iid,))
    if not ix:
        raise HTTPException(404)
    try:
        return {"ok": True, "message": await newznab.test(ix)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": str(e)}


# ------------------------------------------------- built-in downloader --
class ServerIn(BaseModel):
    name: str = ""
    host: str
    port: int = 563
    ssl: bool = True
    username: str = ""
    password: str = ""
    connections: int = 8
    level: int = 0
    retention: int = 0
    active: bool = True


MASK = "********"


def _server_row(body: dict) -> dict:
    row = {k: v for k, v in body.items() if k in ServerIn.model_fields}
    for k in ("ssl", "active"):
        if k in row:
            row[k] = int(bool(row[k]))
    if row.get("password") == MASK:
        row.pop("password")
    if "host" in row:
        row["host"] = str(row["host"]).strip()
        if not row["host"]:
            raise HTTPException(400, "Server address is required")
    return row


def _reconfigure():
    asyncio.create_task(_job("downloader", downloader.ensure))


@app.get("/api/usenet-servers")
def list_servers():
    return [{**s, "password": MASK if s["password"] else ""} for s in downloader.servers()]


@app.post("/api/usenet-servers")
async def add_server(body: ServerIn):
    sid = db.insert("usenet_servers", _server_row(body.model_dump()))
    _reconfigure()
    return {"id": sid}


@app.put("/api/usenet-servers/{sid}")
async def edit_server(sid: int, body: dict):
    db.update("usenet_servers", sid, _server_row(body))
    _reconfigure()
    return {"ok": True}


@app.delete("/api/usenet-servers/{sid}")
async def delete_server(sid: int):
    db.ex("DELETE FROM usenet_servers WHERE id=?", (sid,))
    _reconfigure()
    return {"ok": True}


@app.post("/api/usenet-servers/test")
async def test_server(body: dict):
    """Test a server as typed in the form, or a saved one (id) with its stored password."""
    if body.get("id"):
        saved = db.one("SELECT * FROM usenet_servers WHERE id=?", (int(body["id"]),)) or {}
        body = {**saved, **{k: v for k, v in body.items() if v != MASK}}
    if not body.get("host"):
        return {"ok": False, "message": "Enter the server address first"}
    try:
        err = await nzbget.test_server(body["host"], body.get("port") or 563, body.get("username", ""),
                                       body.get("password", ""), bool(body.get("ssl")))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": str(e)}
    return {"ok": not err, "message": err or "Connected and logged in"}


@app.post("/api/usenet-servers/copy")
async def copy_servers():
    """One-time move from a separate NZBGet: copy its news servers (using the external
    NZBGet address and login saved in settings)."""
    try:
        found = downloader.parse_servers(await nzbget.call("config", external=True))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Couldn't read the other NZBGet's settings: {e}") from e
    have = {(s["host"].lower(), s["username"] or "") for s in downloader.servers()}
    added = [s for s in found if (s["host"].lower(), s["username"] or "") not in have]
    for s in added:
        db.insert("usenet_servers", s)
    _reconfigure()
    return {"found": len(found), "added": len(added)}


@app.get("/api/downloader/status")
async def downloader_status():
    s = db.get_settings()
    out = {"client": s["download_client"], "installed": downloader.installed(), "running": downloader.running(),
           "servers": len(downloader.servers()), "download_dir": downloader.download_dir(),
           "downloads_mapped": os.path.isdir(downloader.DOWNLOADS_MOUNT), "ok": False}
    try:
        st = await nzbget.status()
        out.update(ok=True, version=await nzbget.version(), speed=st.get("DownloadRate", 0),
                   paused=bool(st.get("DownloadPaused")), remaining_mb=st.get("RemainingSizeMB", 0),
                   speed_limit=st.get("DownloadLimit", 0))
    except Exception as e:  # noqa: BLE001
        out["message"] = str(e)
    if s["download_client"] == "builtin":
        out["free"] = downloader.disk_free(downloader.download_dir())
    return out


@app.post("/api/downloader/{action}")
async def downloader_pause(action: str):
    if action not in ("pause", "resume"):
        raise HTTPException(404)
    await nzbget.pause(action == "pause")
    return {"ok": True}


@app.post("/api/nzbget/test")
async def test_nzbget():
    try:
        return {"ok": True, "message": f"NZBGet {await nzbget.version()}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": str(e)}


# ---------------------------------------------------------------- movies --
@app.get("/api/lookup/movie")
async def lookup_movie(term: str):
    res = await metadata.search_movies(term)
    have: dict[int, list[str]] = {}
    for r in db.q("SELECT tmdb_id, tier FROM movies ORDER BY tier"):
        have.setdefault(r["tmdb_id"], []).append(r["tier"])
    for r in res:
        r["library_tiers"] = have.get(r["tmdb_id"], [])
        r["in_library"] = len(r["library_tiers"]) == len(library.TIERS)

    # say which of the top results belong to a collection, so the UI can offer "add whole collection"
    async def coll(r):
        try:
            d = await metadata.movie_details(r["tmdb_id"])
            r["collection"] = {"id": d["collection_id"], "name": d["collection_name"]} if d.get("collection_id") else None
        except Exception:  # noqa: BLE001
            r["collection"] = None
    await asyncio.gather(*(coll(r) for r in res[:10]))
    return res


# ------------------------------------------------------------ title pages --
@app.get("/api/info/{kind}/{tmdb_id}")
async def title_info(kind: str, tmdb_id: int, request: Request):
    """TMDB details for a title page (cast, release dates, trailer, …) plus its library state."""
    if kind not in ("movie", "tv"):
        raise HTTPException(400)
    info = dict(await (metadata.movie_info(tmdb_id) if kind == "movie" else metadata.tv_info(tmdb_id)))
    if kind == "movie":
        rows = db.q("SELECT id, tier FROM movies WHERE tmdb_id=? ORDER BY tier", (tmdb_id,))
        d = info["dates"]
        info["available_date"] = library.available_date({"release_date": d.get("theatrical") or d.get("premiere"),
                                                         "digital_release": d.get("digital"),
                                                         "physical_release": d.get("physical")})
        match = ("target_id=?", (tmdb_id,))
    else:
        tvdb = info.get("tvdb_id") or -1
        rows = db.q("SELECT id, tier FROM series WHERE tmdb_id=? OR tvdb_id=? ORDER BY tier", (tmdb_id, tvdb))
        match = ("((source='tmdb' AND target_id=?) OR (source='tvdb' AND target_id=?))", (tmdb_id, tvdb))
    info["library"] = rows
    user = _user(request)
    info["requested"] = [r["tier"] for r in db.q(
        f"SELECT tier FROM requests WHERE kind=? AND status='pending' AND {match[0]} AND (? OR user_id=?)",
        (kind, *match[1], user["role"] == "admin", user["id"]))]
    return info


# --------------------------------------------- discover (streaming services) --
@app.get("/api/providers")
async def providers():
    return {"region": db.setting("streaming_region") or "US", "providers": await metadata.watch_providers()}


@app.get("/api/providers/{pid}/{kind}")
async def provider_titles(pid: int, kind: str, page: int = 1, sort: str = "popular", originals: bool = False):
    if kind not in ("movie", "tv"):
        raise HTTPException(400)
    prov = next((p for p in await metadata.watch_providers() if p["id"] == pid), None)
    data = await metadata.discover(kind, pid, page, sort, prov["name"] if (originals and prov) else None)
    if kind == "movie":
        have = {r["tmdb_id"]: r["id"] for r in db.q("SELECT id, tmdb_id FROM movies")}
    else:
        have = {r["tmdb_id"]: r["id"] for r in db.q("SELECT id, tmdb_id FROM series WHERE tmdb_id IS NOT NULL")}
    for r in data["results"]:
        r["library_id"] = have.get(r["tmdb_id"])
    data["provider"] = prov
    return data


class AddSeriesTmdb(BaseModel):
    tmdb_id: int
    tier: str = "hd"
    monitor: str = "all"
    search: bool = True


@app.post("/api/series/from-tmdb")
async def add_series_from_tmdb(body: AddSeriesTmdb):
    return await add_series(AddSeries(tmdb_id=body.tmdb_id, tier=body.tier, monitor=body.monitor, search=body.search))


# ------------------------------------------------------------ collections --
@app.get("/api/lookup/collection")
async def lookup_collection(term: str):
    res = await metadata.search_collections(term)
    saved = {(c["source"], c["tmdb_id"]) for c in db.q("SELECT source, tmdb_id FROM collections")}
    for r in res:
        r["saved"] = (r["source"], r["id"]) in saved
    return res


@app.get("/api/collection/{source}/{cid}")
async def collection_details(source: str, cid: int):
    return await collections_.details(source, cid)


class AddCollection(BaseModel):
    source: str = "collection"
    tmdb_id: int
    tier: str = "hd"
    tmdb_ids: list[int] | None = None
    monitor: bool = True
    search: bool = True


@app.post("/api/collections")
async def add_collection(body: AddCollection):
    return await collections_.add(body.source, body.tmdb_id, body.tier, body.tmdb_ids, body.monitor, body.search)


@app.get("/api/collections")
def list_collections():
    return collections_.list_all()


@app.put("/api/collections/{cid}")
def edit_collection(cid: int, body: dict):
    data = {}
    if "monitored" in body:
        data["monitored"] = int(bool(body["monitored"]))
    if body.get("tier") in (*library.TIERS, "both"):
        data["tier"] = body["tier"]  # applies to movies added from now on
    db.update("collections", cid, data)
    return {"ok": True}


@app.post("/api/collections/{cid}/refresh")
async def refresh_collection(cid: int):
    return {"added": await collections_.refresh(cid)}


@app.delete("/api/collections/{cid}")
def delete_collection(cid: int):
    db.ex("DELETE FROM collections WHERE id=?", (cid,))  # movies stay in the library
    return {"ok": True}


class AddMovie(BaseModel):
    tmdb_id: int
    tier: str = "hd"
    monitored: bool = True
    search: bool = True


@app.post("/api/movies")
async def add_movie(body: AddMovie):
    """tier: hd | uhd | both. Adds the versions that aren't in the library yet."""
    added = await library.add_movie_tiers(body.tmdb_id, body.tier, body.monitored)
    for m in added:
        if body.search and body.monitored and library.movie_available(m):
            asyncio.create_task(_job(f"search-movie-{m['id']}", lambda mid=m["id"]: search.search_movie(mid)))
    return {**added[0], "added": [{"id": m["id"], "tier": m["tier"]} for m in added]}


@app.get("/api/movies")
def list_movies():
    rows = db.q("SELECT * FROM movies ORDER BY title")
    active = {r["movie_id"]: r for r in db.q(
        "SELECT movie_id, status, progress FROM downloads WHERE kind='movie' AND status IN ('queued','downloading','importing')")}
    for r in rows:
        r["download"] = active.get(r["id"])
    return rows


@app.get("/api/movies/{mid}")
def get_movie(mid: int, request: Request = None):
    m = db.one("SELECT * FROM movies WHERE id=?", (mid,))
    if not m:
        raise HTTPException(404)
    m["versions"] = _versions("movie", m)
    if request is not None and not _is_admin(request):
        m["file_path"] = bool(m["file_path"]) or None
        m["history"], m["path"] = [], ""
        return m
    m["history"] = db.q("SELECT * FROM downloads WHERE kind='movie' AND movie_id=? ORDER BY id DESC", (mid,))
    m["path"] = library.movie_dir(m)
    return m


def _versions(kind: str, item: dict) -> list[dict]:
    out = []
    for v in library.versions(kind, item):
        if kind == "movie":
            have, total = int(bool(v["file_path"])), 1
        else:
            c = db.one("SELECT SUM(file_path IS NOT NULL) have, SUM(season>0 AND air_date<=date('now')) aired "
                       "FROM episodes WHERE series_id=?", (v["id"],))
            have, total = c["have"] or 0, c["aired"] or 0
        out.append({"id": v["id"], "tier": v["tier"], "monitored": v["monitored"], "have": have, "total": total,
                    "file_quality": v.get("file_quality")})
    return out


@app.put("/api/movies/{mid}")
def edit_movie(mid: int, body: dict):
    if "tier" in body:
        library.change_tier("movie", mid, body["tier"])
    if "monitored" in body:
        db.update("movies", mid, {"monitored": int(bool(body["monitored"]))})
    return get_movie(mid)


@app.delete("/api/movies/{mid}")
def delete_movie(mid: int, delete_files: bool = False):
    """Removes this version; the other version (if any) and its files stay."""
    library.delete_version("movie", mid, delete_files)
    return {"ok": True}


@app.post("/api/movies/{mid}/search")
async def search_movie(mid: int, interactive: bool = False):
    return await search.search_movie(mid, auto_grab=not interactive)


# ---------------------------------------------------------------- series --
@app.get("/api/lookup/series")
async def lookup_series(term: str):
    res = await metadata.search_series(term)
    rows = db.q("SELECT tvdb_id, tmdb_id, tier FROM series ORDER BY tier")
    for r in res:
        r["library_tiers"] = sorted({x["tier"] for x in rows if (r.get("tvdb_id") and x["tvdb_id"] == r["tvdb_id"])
                                     or (r.get("tmdb_id") and x["tmdb_id"] == r["tmdb_id"])})
        r["in_library"] = len(r["library_tiers"]) == len(library.TIERS)
    return res


class AddSeries(BaseModel):
    tvdb_id: int | None = None
    tmdb_id: int | None = None
    tier: str = "hd"
    monitor: str = "all"
    search: bool = True


@app.post("/api/series")
async def add_series(body: AddSeries):
    """tier: hd | uhd | both. monitor 'same' copies episode monitoring from the other version."""
    added = await library.add_series_tiers(body.tvdb_id, body.tier, body.monitor, tmdb_id=body.tmdb_id)
    for s in added:
        if body.search and body.monitor != "none":
            asyncio.create_task(_job(f"search-series-{s['id']}", lambda sid=s["id"]: search.search_series(sid)))
    return {**added[0], "added": [{"id": s["id"], "tier": s["tier"]} for s in added]}


@app.get("/api/series")
def list_series():
    rows = db.q("""SELECT s.*,
        (SELECT COUNT(*) FROM episodes e WHERE e.series_id=s.id AND e.season>0 AND e.air_date<=date('now')) AS aired,
        (SELECT COUNT(*) FROM episodes e WHERE e.series_id=s.id AND e.file_path IS NOT NULL) AS have
        FROM series s ORDER BY title""")
    return rows


@app.get("/api/series/{sid}")
def get_series(sid: int, request: Request = None):
    s = db.one("SELECT * FROM series WHERE id=?", (sid,))
    if not s:
        raise HTTPException(404)
    s["versions"] = _versions("tv", s)
    s["episodes"] = db.q("SELECT * FROM episodes WHERE series_id=? ORDER BY season DESC, episode DESC", (sid,))
    s["path"] = library.series_dir(s)
    active = {}
    for dl in db.q("SELECT * FROM downloads WHERE kind='tv' AND series_id=? AND status IN ('queued','downloading','importing')", (sid,)):
        import json
        for eid in json.loads(dl["episode_ids"] or "[]"):
            active[eid] = {"status": dl["status"], "progress": dl["progress"]}
    for e in s["episodes"]:
        e["download"] = active.get(e["id"])
    if request is not None and not _is_admin(request):
        s["path"] = ""
        for e in s["episodes"]:
            e["file_path"] = bool(e["file_path"]) or None
    return s


@app.put("/api/series/{sid}")
def edit_series(sid: int, body: dict):
    if "tier" in body:
        library.change_tier("tv", sid, body["tier"])
    if "monitored" in body:
        db.update("series", sid, {"monitored": int(bool(body["monitored"]))})
    if "series_type" in body:
        db.update("series", sid, {"series_type": body["series_type"]})
    return {"ok": True}


@app.put("/api/episodes/monitor")
def monitor_episodes(body: dict):
    ids = body.get("ids", [])
    for i in ids:
        db.update("episodes", int(i), {"monitored": int(bool(body.get("monitored")))})
    return {"ok": True}


@app.delete("/api/series/{sid}")
def delete_series(sid: int, delete_files: bool = False):
    """Removes this version; the other version (if any) and its files stay."""
    library.delete_version("tv", sid, delete_files)
    return {"ok": True}


@app.post("/api/series/{sid}/refresh")
async def refresh_series(sid: int):
    await library.refresh_series(sid)
    return {"ok": True}


@app.post("/api/series/{sid}/search")
async def search_series(sid: int, season: int | None = None, episode_id: int | None = None, interactive: bool = False):
    return await search.search_series(sid, season=season, episode_id=episode_id, auto_grab=not interactive)


# ----------------------------------------------------- manual grab / queue --
class GrabIn(BaseModel):
    kind: str
    item_id: int
    guid: str
    title: str
    link: str
    indexer: str = ""
    indexer_id: int = 0
    size: int = 0
    episode_ids: list[int] = []


@app.post("/api/grab")
async def manual_grab(body: GrabIn):
    rel = newznab.Release(title=body.title, guid=body.guid, link=body.link, size=body.size,
                          indexer=body.indexer, indexer_id=body.indexer_id)
    prof = decision.profile_for(body.kind, (db.one(
        f"SELECT tier FROM {'movies' if body.kind == 'movie' else 'series'} WHERE id=?", (body.item_id,)) or {})["tier"])
    d = decision.evaluate(rel, body.kind, prof)
    d.episode_ids = body.episode_ids
    if body.kind == "movie":
        return {"id": await search.grab(d, "movie", movie_id=body.item_id)}
    return {"id": await search.grab(d, "tv", series_id=body.item_id)}


@app.get("/api/queue")
def get_queue(limit: int = 200):
    rows = db.q("""SELECT d.*, m.title AS movie_title, s.title AS series_title FROM downloads d
        LEFT JOIN movies m ON m.id=d.movie_id LEFT JOIN series s ON s.id=d.series_id
        ORDER BY d.id DESC LIMIT ?""", (limit,))
    return rows


@app.post("/api/queue/{did}/retry-import")
async def retry_import(did: int):
    await importer.retry_import(did)
    return {"ok": True}


@app.delete("/api/queue/{did}")
async def remove_download(did: int, blocklist: bool = False):
    dl = db.one("SELECT * FROM downloads WHERE id=?", (did,))
    if not dl:
        raise HTTPException(404)
    if dl["status"] in ("queued", "downloading"):
        try:
            await nzbget.delete(dl["nzbget_id"])
        except nzbget.NZBGetError:
            pass
    if blocklist:
        await importer.fail_download(dl, "Removed by user", research=True)
    else:
        db.ex("UPDATE downloads SET status='removed' WHERE id=?", (did,))
    return {"ok": True}


@app.get("/api/blocklist")
def get_blocklist():
    return db.q("SELECT * FROM blocklist ORDER BY id DESC")


@app.delete("/api/blocklist/{bid}")
def del_blocklist(bid: int):
    db.ex("DELETE FROM blocklist WHERE id=?", (bid,))
    return {"ok": True}


# ---------------------------------------------------------------- system --
@app.get("/api/status")
def status():
    return {
        "trash_loaded": trash.ensure_loaded().loaded,
        "trash_synced": (db.one("SELECT value FROM settings WHERE key='_trash_synced'") or {}).get("value"),
        "tasks": {k: {"running": _running.get(k, False), "last_run": v} for k, v in _last_run.items()},
        "counts": {
            "movies": db.one("SELECT COUNT(*) c FROM movies")["c"],
            "series": db.one("SELECT COUNT(*) c FROM series")["c"],
            "wanted_movies": db.one("SELECT COUNT(*) c FROM movies WHERE monitored=1 AND file_path IS NULL")["c"],
            "queue": db.one("SELECT COUNT(*) c FROM downloads WHERE status IN ('queued','downloading','importing')")["c"],
        },
    }


@app.get("/api/events")
def events(limit: int = 200):
    return db.q("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))


@app.post("/api/tasks/{name}")
async def run_task(name: str):
    fns = {"rss": search.rss_sync, "missing": search.missing_search, "downloads": importer.process_downloads,
           "series_refresh": _refresh_all_series, "trash": trash.sync}
    if name not in fns:
        raise HTTPException(404)
    asyncio.create_task(_job(name, fns[name]))
    return {"started": name}


@app.get("/api/parse")
def parse_release(title: str, kind: str = "movie", tier: str = "hd"):
    """Debug helper: how would this release title be parsed and scored?"""
    from .parser import parse
    p = parse(title)
    prof = decision.profile_for(kind, tier)
    score, cfs = trash.score_release(prof, p)
    return {"parsed": p.__dict__, "quality": p.quality_name(kind), "allowed": prof.quality_rank(p.quality_name(kind)) is not None,
            "score": score, "custom_formats": cfs, "profile": prof.name}


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/{path:path}")
def spa(path: str):
    return FileResponse(os.path.join(STATIC, "index.html"))
