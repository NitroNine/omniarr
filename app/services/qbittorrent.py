"""Built-in torrent client: qBittorrent (qbittorrent-nox from Debian, in the image) run by Omniarr.

Torrents are only ever added by hand (Activity → Torrents). Omniarr starts it on localhost with its own
profile in /config/qbittorrent, sets the download folder and listening port, and restarts it if it stops.
Its Web API is reached on 127.0.0.1 without a password (qBittorrent's "bypass authentication for clients
on localhost"); the port is never published."""
from __future__ import annotations

import asyncio
import base64
import configparser
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
from urllib.parse import parse_qs, unquote, urlsplit

import httpx

from .. import db

log = logging.getLogger("omniarr.torrent")

BINARY = os.environ.get("OMNIARR_QBITTORRENT", "/usr/bin/qbittorrent-nox")
PORT = 16790  # Web API, inside the container only
CATEGORY = "omniarr"
DONE_STATES = {"uploading", "stalledUP", "pausedUP", "stoppedUP", "queuedUP", "forcedUP"}

_proc: subprocess.Popen | None = None
_applied: dict | None = None  # preferences last sent


class TorrentError(RuntimeError):
    pass


def installed() -> bool:
    return os.access(BINARY, os.X_OK)


def enabled() -> bool:
    return bool(db.setting("torrent_enabled"))


def profile_dir() -> str:
    return os.path.join(db.CONFIG_DIR, "qbittorrent")


def conf_path() -> str:
    return os.path.join(profile_dir(), "qBittorrent", "config", "qBittorrent.conf")


def save_dir() -> str:
    """Settings' torrent folder, else /data/torrents (same disk as the library, so finished files can be
    hardlinked while the torrent keeps seeding), else <download folder>/torrents."""
    chosen = (db.setting("torrent_dir") or "").strip().rstrip("/")
    if chosen:
        return chosen
    if os.path.isdir("/data"):
        return "/data/torrents"
    from .downloader import download_dir
    return os.path.join(download_dir(), "torrents")


def _write_conf() -> None:
    """What has to be right before it starts: legal notice accepted, Web UI on localhost only, no password
    from localhost. The rest is set through the API once it runs (`_apply_prefs`)."""
    path = conf_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    cp = configparser.RawConfigParser(strict=False, interpolation=None)
    cp.optionxform = str  # keys are case-sensitive (WebUI\Port)
    if os.path.exists(path):
        try:
            cp.read(path, encoding="utf-8")
        except configparser.Error:
            cp = configparser.RawConfigParser(strict=False, interpolation=None)
            cp.optionxform = str
    for sec, key, val in (("LegalNotice", "Accepted", "true"),
                          ("Preferences", "WebUI\\Address", "127.0.0.1"),
                          ("Preferences", "WebUI\\Port", str(PORT)),
                          ("Preferences", "WebUI\\LocalHostAuth", "false"),
                          ("Preferences", "WebUI\\CSRFProtection", "false"),
                          ("Preferences", "WebUI\\HostHeaderValidation", "false"),
                          ("Preferences", "General\\Locale", "en")):
        if not cp.has_section(sec):
            cp.add_section(sec)
        cp.set(sec, key, val)
    with open(path, "w", encoding="utf-8") as f:
        cp.write(f, space_around_delimiters=False)


def running() -> bool:
    return _proc is not None and _proc.poll() is None


def start() -> None:
    global _proc, _applied
    if running():
        return
    _write_conf()
    try:
        os.makedirs(save_dir(), exist_ok=True)
    except OSError as e:
        log.warning("Cannot create torrent folder %s: %s", save_dir(), e)
    port = int(db.setting("torrent_port") or 6882)
    env = {**os.environ, "HOME": profile_dir()}
    _proc = subprocess.Popen([BINARY, f"--profile={profile_dir()}", f"--webui-port={PORT}",
                              f"--torrenting-port={port}", "--confirm-legal-notice"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, env=env)
    _applied = None
    log.info("Built-in torrent client started (pid %s)", _proc.pid)


def stop() -> None:
    global _proc
    if not running():
        _proc = None
        return
    _proc.terminate()
    try:
        _proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        _proc.kill()
    _proc = None


def preferences() -> dict:
    s = db.get_settings()
    ratio = float(s.get("torrent_seed_ratio") or 0)
    return {
        "save_path": save_dir(), "temp_path_enabled": False, "auto_tmm_enabled": False,
        "listen_port": int(s.get("torrent_port") or 6882), "random_port": False, "upnp": False,
        "bypass_local_auth": True, "web_ui_address": "127.0.0.1",
        "max_ratio_enabled": ratio > 0, "max_ratio": ratio if ratio > 0 else -1, "max_ratio_act": 0,  # 0 = stop seeding
        "up_limit": int(s.get("torrent_upload_limit") or 0) * 1024, "dl_limit": 0,
        "create_subfolder_enabled": True, "torrent_content_layout": "Original",
    }


async def _apply_prefs() -> None:
    global _applied
    prefs = preferences()
    if prefs == _applied:
        return
    await call("POST", "app/setPreferences", data={"json": json.dumps(prefs)})
    try:
        await call("POST", "torrents/createCategory", data={"category": CATEGORY, "savePath": prefs["save_path"]})
    except TorrentError:
        await call("POST", "torrents/editCategory", data={"category": CATEGORY, "savePath": prefs["save_path"]})
    _applied = prefs


async def ensure() -> None:
    """Scheduler job: start, stop or restart the built-in client to match the settings."""
    if not enabled() or not installed():
        if running():
            await asyncio.to_thread(stop)
        return
    if not running():
        if _proc is not None:
            db.log("The built-in torrent client stopped unexpectedly; restarting it", "warn")
        await asyncio.to_thread(start)
        for _ in range(20):  # give the Web API a moment to come up
            await asyncio.sleep(0.5)
            try:
                await _apply_prefs()
                return
            except TorrentError:
                continue
        return
    try:
        await _apply_prefs()
    except TorrentError as e:
        log.warning("Couldn't apply torrent settings: %s", e)


# ------------------------------------------------------------------ API --
async def call(method: str, path: str, data: dict | None = None, files: dict | None = None, params: dict | None = None):
    url = f"http://127.0.0.1:{PORT}/api/v2/{path}"
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.request(method, url, data=data, files=files, params=params)
    except httpx.HTTPError:
        raise TorrentError("The built-in torrent client isn't running yet"
                           + ("" if installed() else " (qBittorrent isn't in this image)")) from None
    if r.status_code == 409:
        raise TorrentError(r.text or "qBittorrent refused that")
    if r.status_code >= 400:
        raise TorrentError(f"qBittorrent answered {r.status_code}: {r.text[:200]}")
    ctype = r.headers.get("content-type", "")
    return r.json() if "json" in ctype else r.text


async def version() -> str:
    return str(await call("GET", "app/version"))


async def transfer() -> dict:
    return await call("GET", "transfer/info") or {}


async def torrents(hashes: list[str] | None = None) -> list[dict]:
    params = {"hashes": "|".join(hashes)} if hashes else None
    return await call("GET", "torrents/info", params=params) or []


async def get(h: str) -> dict | None:
    rows = await torrents([h])
    return rows[0] if rows else None


def finished(t: dict) -> bool:
    return (t.get("progress") or 0) >= 1 and t.get("state") in DONE_STATES


async def add_magnet(magnet: str) -> str:
    h = magnet_hash(magnet)
    if not h:
        raise ValueError("That isn't a magnet link (it needs xt=urn:btih:…)")
    res = await call("POST", "torrents/add", data={"urls": magnet, "category": CATEGORY, "savepath": save_dir()})
    if str(res).strip().lower().startswith("fail"):
        raise ValueError("qBittorrent didn't take it (already added?)")
    return h


async def add_file(data: bytes, filename: str = "file.torrent") -> str:
    h = torrent_hash(data)
    res = await call("POST", "torrents/add", data={"category": CATEGORY, "savepath": save_dir()},
                     files={"torrents": (filename, data, "application/x-bittorrent")})
    if str(res).strip().lower().startswith("fail"):
        raise ValueError("qBittorrent didn't take it (already added?)")
    return h


async def delete(h: str, files: bool = False) -> None:
    await call("POST", "torrents/delete", data={"hashes": h, "deleteFiles": "true" if files else "false"})


async def pause(h: str, paused: bool) -> None:
    # qBittorrent 5 calls it stop/start, 4.x pause/resume
    try:
        await call("POST", "torrents/" + ("stop" if paused else "start"), data={"hashes": h})
    except TorrentError:
        await call("POST", "torrents/" + ("pause" if paused else "resume"), data={"hashes": h})


# ---------------------------------------------------------------- hashes --
def magnet_hash(magnet: str) -> str | None:
    """The v1 info-hash (40 hex) of a magnet link; base32 hashes are converted."""
    if not magnet.strip().lower().startswith("magnet:?"):
        return None
    for xt in parse_qs(urlsplit(magnet.strip()).query).get("xt", []):
        m = re.fullmatch(r"urn:btih:([0-9a-fA-F]{40}|[A-Za-z2-7]{32})", xt)
        if m:
            v = m.group(1)
            return v.lower() if len(v) == 40 else base64.b32decode(v.upper()).hex()
    return None


def magnet_name(magnet: str) -> str:
    dn = parse_qs(urlsplit(magnet.strip()).query).get("dn", [""])[0]
    return unquote(dn).strip()


def _bdecode_end(b: bytes, i: int) -> int:
    """Index just past the bencoded value starting at i."""
    c = b[i:i + 1]
    if c == b"i":
        return b.index(b"e", i) + 1
    if c in (b"l", b"d"):
        i += 1
        while b[i:i + 1] != b"e":
            i = _bdecode_end(b, i)
        return i + 1
    colon = b.index(b":", i)
    return colon + 1 + int(b[i:colon])


def torrent_hash(data: bytes) -> str:
    """v1 info-hash: SHA-1 of the raw bencoded `info` dictionary."""
    if not data.startswith(b"d"):
        raise ValueError("That isn't a .torrent file")
    i = 1
    try:
        while data[i:i + 1] != b"e":
            key_end = _bdecode_end(data, i)
            key = data[data.index(b":", i) + 1:key_end]
            val_end = _bdecode_end(data, key_end)
            if key == b"info":
                return hashlib.sha1(data[key_end:val_end]).hexdigest()
            i = val_end
    except (ValueError, IndexError):
        pass
    raise ValueError("That isn't a .torrent file")


def torrent_name(data: bytes) -> str:
    m = re.search(rb"4:name(\d+):", data)
    if not m:
        return ""
    n = int(m.group(1))
    return data[m.end():m.end() + n].decode("utf-8", "replace")


def disk_free() -> int | None:
    try:
        return shutil.disk_usage(save_dir()).free
    except OSError:
        return None
