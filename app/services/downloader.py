"""Built-in downloader: NZBGet shipped inside the Omniarr image and run by Omniarr.

Omniarr owns its configuration (Usenet servers, download folder, speed limit), starts it
as a background process listening on localhost only, and restarts it if it stops. The
rest of Omniarr talks to it through the normal NZBGet client in ``nzbget.py``.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import shutil
import subprocess
import time

from .. import db

log = logging.getLogger("omniarr.downloader")

APP_DIR = os.environ.get("OMNIARR_NZBGET_DIR", "/opt/nzbget")
DOWNLOADS_MOUNT = os.environ.get("OMNIARR_DOWNLOADS", "/downloads")  # the template's Downloads path
PORT = 16789  # inside the container only; never published
USERNAME = "omniarr"

_proc: subprocess.Popen | None = None
_written: str | None = None  # config text NZBGet was last started/reloaded with
_cleaned = 0.0  # when old .nzb copies were last removed
NZB_KEEP_DAYS = 7


def builtin() -> bool:
    return db.setting("download_client") == "builtin"


def installed() -> bool:
    return os.access(os.path.join(APP_DIR, "nzbget"), os.X_OK)


def conf_dir() -> str:
    return os.path.join(db.CONFIG_DIR, "nzbget")


def conf_path() -> str:
    return os.path.join(conf_dir(), "nzbget.conf")


def control_password() -> str:
    pw = db.setting("_downloader_password")
    if not pw:
        pw = secrets.token_urlsafe(24)
        db.save_settings({"_downloader_password": pw})
    return pw


def url() -> str:
    return f"http://127.0.0.1:{PORT}"


def download_dir() -> str:
    """The folder set in Settings, else the Downloads path from the container setup, else /data/usenet."""
    chosen = (db.setting("download_dir") or "").strip().rstrip("/")
    if chosen:
        return chosen
    return DOWNLOADS_MOUNT if os.path.isdir(DOWNLOADS_MOUNT) else "/data/usenet"


def _article_cache_mb() -> int:
    """1 GB of article cache when the machine has RAM to spare (8 GB+), else 200 MB."""
    try:
        with open("/proc/meminfo") as f:
            total_kb = int(next(line for line in f if line.startswith("MemTotal")).split()[1])
        return 1024 if total_kb >= 8 * 1024 * 1024 else 200
    except (OSError, StopIteration, ValueError):
        return 200


def servers() -> list[dict]:
    return db.q("SELECT * FROM usenet_servers ORDER BY level, id")


def _cert_store() -> str:
    for p in (os.path.join(APP_DIR, "cacert.pem"), "/etc/ssl/certs/ca-certificates.crt"):
        if os.path.exists(p):
            return p
    return ""


def options() -> dict[str, str]:
    """Every option Omniarr sets; anything else keeps NZBGet's own default."""
    s = db.get_settings()
    main = download_dir()
    cfg = conf_dir()
    rate = max(0, int(s.get("download_speed_limit") or 0))  # KB/s, 0 = unlimited
    opts = {
        "MainDir": main,
        "DestDir": "${MainDir}/completed",
        "InterDir": "${MainDir}/intermediate",
        "TempDir": "${MainDir}/tmp",
        "NzbDir": f"{cfg}/nzb",
        "QueueDir": f"{cfg}/queue",
        "ScriptDir": f"{cfg}/scripts",
        "LockFile": f"{cfg}/nzbget.lock",
        "LogFile": f"{cfg}/nzbget.log",
        "WebDir": os.path.join(APP_DIR, "webui"),
        "ConfigTemplate": os.path.join(APP_DIR, "nzbget.conf"),
        "UnrarCmd": os.path.join(APP_DIR, "unrar"),
        "SevenZipCmd": os.path.join(APP_DIR, "7za"),
        "CertStore": _cert_store(),
        "CertCheck": "yes",
        # only Omniarr can talk to it
        "ControlIP": "127.0.0.1",
        "ControlPort": str(PORT),
        "ControlUsername": USERNAME,
        "ControlPassword": control_password(),
        "RestrictedUsername": "", "RestrictedPassword": "", "AddUsername": "", "AddPassword": "",
        "AuthorizedIP": "127.0.0.1",
        "SecureControl": "no",
        "FormAuth": "no",
        "UpdateCheck": "none",
        # Omniarr picks releases and handles duplicates and failures itself
        "DupeCheck": "no",
        "HealthCheck": "delete",  # hopeless downloads fail fast -> blocklist + search again
        "ParCheck": "auto", "ParRepair": "yes", "Unpack": "yes", "DirectUnpack": "yes",
        "AppendCategoryDir": "yes",
        # speed: big in-memory article cache, parallel repair/unpack on all cores, files renamed while downloading
        "ArticleCache": str(_article_cache_mb()), "WriteBuffer": "1024", "DirectWrite": "yes",
        "PostStrategy": "rocket", "ParThreads": "0", "ParBuffer": "500", "DirectRename": "yes",
        "DiskSpace": "1000",  # MB; pause instead of filling the array
        "NzbDirInterval": "0",
        "DownloadRate": str(rate),
        "WriteLog": "rotate", "RotateLog": "3",
        # per-article detail and debug lines made the daily log ~650 MB; keep info, warnings and errors
        "DetailTarget": "none", "DebugTarget": "none",
        "UMask": "1000",  # keep the container's umask
    }
    for i, cat in enumerate((s["nzbget_category_movies"] or "movies", s["nzbget_category_tv"] or "tv"), 1):
        opts[f"Category{i}.Name"] = cat
        opts[f"Category{i}.Unpack"] = "yes"
    for i, sv in enumerate(servers(), 1):
        p = f"Server{i}."
        opts.update({
            p + "Active": "yes" if sv["active"] else "no",
            p + "Name": sv["name"] or sv["host"],
            p + "Level": str(sv["level"] or 0),
            p + "Optional": "yes" if sv["level"] else "no",
            p + "Group": "0",
            p + "Host": sv["host"],
            p + "Port": str(sv["port"] or (563 if sv["ssl"] else 119)),
            p + "Encryption": "yes" if sv["ssl"] else "no",
            p + "Username": sv["username"] or "",
            p + "Password": sv["password"] or "",
            p + "JoinGroup": "no",
            p + "Connections": str(sv["connections"] or 8),
            p + "Retention": str(sv["retention"] or 0),
            p + "CertVerification": "strict" if sv["ssl"] else "none",
        })
    return opts


_KEY = re.compile(r"^([A-Za-z][A-Za-z0-9.]*)=")
_LISTED = re.compile(r"^(Server|Category|Feed|Task)\d+\.")


def render(template: str, opts: dict[str, str]) -> str:
    """NZBGet's own config template with Omniarr's values filled in. Its example servers,
    categories, feeds and tasks are dropped; ours are appended."""
    out, done = [], set()
    for line in template.splitlines():
        m = _KEY.match(line)
        if m:
            key = m.group(1)
            if _LISTED.match(key):
                continue
            if key in opts:
                out.append(f"{key}={opts[key]}")
                done.add(key)
                continue
        out.append(line)
    rest = [k for k in opts if k not in done]
    if rest:
        out += ["", "# ---- set by Omniarr ----"] + [f"{k}={opts[k]}" for k in rest]
    return "\n".join(out) + "\n"


def write_config() -> str:
    tmpl_path = os.path.join(APP_DIR, "nzbget.conf")
    template = open(tmpl_path, encoding="utf-8").read() if os.path.exists(tmpl_path) else ""
    text = render(template, options())
    os.makedirs(conf_dir(), exist_ok=True)
    for d in ("nzb", "queue", "scripts"):
        os.makedirs(os.path.join(conf_dir(), d), exist_ok=True)
    main = download_dir()
    try:
        os.makedirs(main, exist_ok=True)
    except OSError as e:
        log.warning("Cannot create download folder %s: %s", main, e)
    tmp = conf_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(tmp, 0o600)  # holds the Usenet passwords
    os.replace(tmp, conf_path())
    return text


def running() -> bool:
    return _proc is not None and _proc.poll() is None


def start() -> None:
    global _proc, _written
    if running():
        return
    _written = write_config()
    try:  # a lock file left by a crash would stop it starting
        os.remove(os.path.join(conf_dir(), "nzbget.lock"))
    except FileNotFoundError:
        pass
    _proc = subprocess.Popen([os.path.join(APP_DIR, "nzbget"), "-s", "-c", conf_path(), "-o", "OutputMode=loggable"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    log.info("Built-in downloader started (pid %s)", _proc.pid)


def stop() -> None:
    global _proc
    if not running():
        _proc = None
        return
    _proc.terminate()
    try:
        _proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        _proc.kill()
    _proc = None


def clean_nzb_dir(days: int = NZB_KEEP_DAYS) -> int:
    """NZBGet keeps a copy of every .nzb it was given (NzbDir, *.queued / *.nzb). Its queue lives in QueueDir, so
    copies older than a week only take space (~0.5 MB each). Returns how many were removed."""
    d = os.path.join(conf_dir(), "nzb")
    if not os.path.isdir(d):
        return 0
    cutoff = time.time() - days * 86400
    n = 0
    for f in os.listdir(d):
        path = os.path.join(d, f)
        try:
            if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                os.remove(path)
                n += 1
        except OSError:
            pass
    return n


async def ensure() -> None:
    """Start, stop, restart or reconfigure the built-in downloader to match the settings."""
    global _written, _cleaned
    if time.time() - _cleaned > 6 * 3600:
        _cleaned = time.time()
        await asyncio.to_thread(clean_nzb_dir)
    if not builtin() or not installed():
        if running():
            await asyncio.to_thread(stop)
        return
    if not running():
        if _proc is not None:
            db.log("The built-in downloader stopped unexpectedly; restarting it", "warn")
        await asyncio.to_thread(start)
        return
    text = render(open(os.path.join(APP_DIR, "nzbget.conf"), encoding="utf-8").read()
                  if os.path.exists(os.path.join(APP_DIR, "nzbget.conf")) else "", options())
    if text != _written:  # settings changed: NZBGet re-reads its config file on "reload"
        _written = write_config()
        from . import nzbget
        try:
            await nzbget.call("reload")
        except Exception:  # noqa: BLE001
            await asyncio.to_thread(stop)
            await asyncio.to_thread(start)


def parse_servers(config: list[dict] | dict) -> list[dict]:
    """News servers out of another NZBGet's settings (the "config" API call or a parsed
    nzbget.conf), in the shape of the usenet_servers table."""
    if isinstance(config, list):
        config = {o["Name"]: o["Value"] for o in config}
    out = []
    for n in sorted({int(m.group(1)) for k in config if (m := re.match(r"Server(\d+)\.Host$", k))}):
        g = lambda k, d="": config.get(f"Server{n}.{k}", d)  # noqa: E731
        if not g("Host"):
            continue
        ssl = g("Encryption", "no").lower() == "yes"
        out.append({"name": g("Name") or g("Host"), "host": g("Host"), "port": int(g("Port") or (563 if ssl else 119)),
                    "ssl": int(ssl), "username": g("Username"), "password": g("Password"),
                    "connections": int(g("Connections") or 8), "level": int(g("Level") or 0),
                    "retention": int(g("Retention") or 0), "active": int(g("Active", "yes").lower() == "yes")})
    return out


def disk_free(path: str) -> int | None:
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None
