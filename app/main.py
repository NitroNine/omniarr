"""Omniarr web app: REST API + background scheduler + static UI."""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db, decision, importer, library, search, trash
from .services import metadata, newznab, nzbget

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
            ("downloads", 30, importer.process_downloads),
            ("trash", 3600, _trash_auto),
            ("series_refresh", 3600, _refresh_all_series),
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
    trash.ensure_loaded()
    task = asyncio.create_task(scheduler())
    log.info("Omniarr started — config dir %s", db.CONFIG_DIR)
    yield
    task.cancel()


app = FastAPI(title="Omniarr", lifespan=lifespan)


# ------------------------------------------------------------------ auth --
@app.middleware("http")
async def basic_auth(request: Request, call_next):
    password = os.environ.get("OMNIARR_PASSWORD")
    if password and request.url.path != "/health":
        user = os.environ.get("OMNIARR_USERNAME", "admin")
        hdr = request.headers.get("authorization", "")
        ok = False
        if hdr.lower().startswith("basic "):
            try:
                u, _, p = base64.b64decode(hdr[6:]).decode().partition(":")
                ok = secrets.compare_digest(u, user) and secrets.compare_digest(p, password)
            except (ValueError, UnicodeDecodeError):
                ok = False
        if not ok:
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="Omniarr"'})
    return await call_next(request)


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


# -------------------------------------------------------------- settings --
SECRET_KEYS = {"tmdb_api_key", "tvdb_api_key", "tvdb_pin", "nzbget_password"}


@app.get("/api/settings")
def get_settings():
    s = db.get_settings()
    out = {k: v for k, v in s.items() if not k.startswith("_")}
    for k in SECRET_KEYS:
        out[k] = "********" if out.get(k) else ""
    out["_trash_synced"] = s.get("_trash_synced")
    return out


@app.put("/api/settings")
def put_settings(values: dict):
    values = {k: v for k, v in values.items() if not (k in SECRET_KEYS and v == "********")}
    db.save_settings(values)
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
    allowed = {k: v for k, v in ix.items() if k in IndexerIn.model_fields and not (k == "apikey" and "…" in str(v))}
    if "enabled" in allowed:
        allowed["enabled"] = int(bool(allowed["enabled"]))
    db.update("indexers", iid, allowed)
    return {"ok": True}


@app.delete("/api/indexers/{iid}")
def delete_indexer(iid: int):
    db.ex("DELETE FROM indexers WHERE id=?", (iid,))
    return {"ok": True}


@app.post("/api/indexers/{iid}/test")
async def test_indexer(iid: int):
    ix = db.one("SELECT * FROM indexers WHERE id=?", (iid,))
    if not ix:
        raise HTTPException(404)
    try:
        return {"ok": True, "message": await newznab.test(ix)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": str(e)}


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
    have = {r["tmdb_id"] for r in db.q("SELECT tmdb_id FROM movies")}
    for r in res:
        r["in_library"] = r["tmdb_id"] in have
    return res


class AddMovie(BaseModel):
    tmdb_id: int
    tier: str = "hd"
    monitored: bool = True
    search: bool = True


@app.post("/api/movies")
async def add_movie(body: AddMovie):
    m = await library.add_movie(body.tmdb_id, body.tier, body.monitored)
    if body.search and body.monitored and library.movie_available(m):
        asyncio.create_task(_job(f"search-movie-{m['id']}", lambda: search.search_movie(m["id"])))
    return m


@app.get("/api/movies")
def list_movies():
    rows = db.q("SELECT * FROM movies ORDER BY title")
    active = {r["movie_id"]: r for r in db.q(
        "SELECT movie_id, status, progress FROM downloads WHERE kind='movie' AND status IN ('queued','downloading','importing')")}
    for r in rows:
        r["download"] = active.get(r["id"])
    return rows


@app.get("/api/movies/{mid}")
def get_movie(mid: int):
    m = db.one("SELECT * FROM movies WHERE id=?", (mid,))
    if not m:
        raise HTTPException(404)
    m["history"] = db.q("SELECT * FROM downloads WHERE kind='movie' AND movie_id=? ORDER BY id DESC", (mid,))
    m["path"] = library.movie_dir(m)
    return m


@app.put("/api/movies/{mid}")
def edit_movie(mid: int, body: dict):
    if "tier" in body:
        library.change_tier("movie", mid, body["tier"])
    if "monitored" in body:
        db.update("movies", mid, {"monitored": int(bool(body["monitored"]))})
    return get_movie(mid)


@app.delete("/api/movies/{mid}")
def delete_movie(mid: int, delete_files: bool = False):
    m = db.one("SELECT * FROM movies WHERE id=?", (mid,))
    if m and delete_files and os.path.isdir(library.movie_dir(m)):
        import shutil
        shutil.rmtree(library.movie_dir(m), ignore_errors=True)
    db.ex("DELETE FROM movies WHERE id=?", (mid,))
    return {"ok": True}


@app.post("/api/movies/{mid}/search")
async def search_movie(mid: int, interactive: bool = False):
    return await search.search_movie(mid, auto_grab=not interactive)


# ---------------------------------------------------------------- series --
@app.get("/api/lookup/series")
async def lookup_series(term: str):
    res = await metadata.search_series(term)
    have = {r["tvdb_id"] for r in db.q("SELECT tvdb_id FROM series")}
    for r in res:
        r["in_library"] = r["tvdb_id"] in have
    return res


class AddSeries(BaseModel):
    tvdb_id: int
    tier: str = "hd"
    monitor: str = "all"
    search: bool = True


@app.post("/api/series")
async def add_series(body: AddSeries):
    s = await library.add_series(body.tvdb_id, body.tier, body.monitor)
    if body.search and body.monitor != "none":
        asyncio.create_task(_job(f"search-series-{s['id']}", lambda: search.search_series(s["id"])))
    return s


@app.get("/api/series")
def list_series():
    rows = db.q("""SELECT s.*,
        (SELECT COUNT(*) FROM episodes e WHERE e.series_id=s.id AND e.season>0 AND e.air_date<=date('now')) AS aired,
        (SELECT COUNT(*) FROM episodes e WHERE e.series_id=s.id AND e.file_path IS NOT NULL) AS have
        FROM series s ORDER BY title""")
    return rows


@app.get("/api/series/{sid}")
def get_series(sid: int):
    s = db.one("SELECT * FROM series WHERE id=?", (sid,))
    if not s:
        raise HTTPException(404)
    s["episodes"] = db.q("SELECT * FROM episodes WHERE series_id=? ORDER BY season DESC, episode DESC", (sid,))
    s["path"] = library.series_dir(s)
    active = {}
    for dl in db.q("SELECT * FROM downloads WHERE kind='tv' AND series_id=? AND status IN ('queued','downloading','importing')", (sid,)):
        import json
        for eid in json.loads(dl["episode_ids"] or "[]"):
            active[eid] = {"status": dl["status"], "progress": dl["progress"]}
    for e in s["episodes"]:
        e["download"] = active.get(e["id"])
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
    s = db.one("SELECT * FROM series WHERE id=?", (sid,))
    if s and delete_files and os.path.isdir(library.series_dir(s)):
        import shutil
        shutil.rmtree(library.series_dir(s), ignore_errors=True)
    db.ex("DELETE FROM episodes WHERE series_id=?", (sid,))
    db.ex("DELETE FROM series WHERE id=?", (sid,))
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
