"""The built-in deemix (Deezer downloader): the command-line tool from bambanah/deemix (GPL-3.0), bundled in the image
at /opt/deemix/deemix.cjs and run with the image's Node.js, once per album.

Its config folder is /config/deemix: Omniarr writes config.json (folder and file naming, quality) and login.json
(the Deezer ARL from Settings → Music) before every run. Files go to <dest>/<Artist>/<Album>/NN- Title.flac."""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from typing import Callable

from .. import db

CLI = os.environ.get("OMNIARR_DEEMIX", "/opt/deemix/deemix.cjs")
QUALITY = {"flac": 9, "320": 3, "128": 1}
_PROGRESS = re.compile(r"Downloading: (\d+(?:\.\d+)?)%")
# info lines look like "[Album] Artist - Track :: Track downloaded."; anything else after " :: " is an error
_INFO = ("Getting tags", "Tags got", "Getting download URL", "Download URL got", "Downloading album art",
         "Album art downloaded", "Downloading track", "Track downloaded", "Track already downloaded", "Tagging track",
         "Track tagged", "Deezer timed out", "Desired bitrate not found", "This track has been searched")


class DeemixError(Exception):
    pass


def config_dir() -> str:
    return os.path.join(db.CONFIG_DIR, "deemix")


def available() -> str | None:
    """Why the built-in deemix can't run, or None."""
    if not shutil.which("node"):
        return "Node.js isn't installed (it comes with the Omniarr Docker image)"
    if not os.path.isfile(CLI):
        return f"deemix isn't installed ({CLI} is missing; it comes with the Omniarr Docker image)"
    return None


def settings_for(quality: str, artist_folder: bool = True) -> dict:
    return {
        "tracknameTemplate": "%artist% - %title%",
        "albumTracknameTemplate": "%tracknumber%- %title%",
        "createArtistFolder": artist_folder, "artistNameTemplate": "%artist%",
        "createAlbumFolder": True, "albumNameTemplate": "%album%",
        "createCDFolder": True, "createSingleFolder": True, "createPlaylistFolder": True,
        "padTracks": True, "padSingleDigit": True, "paddingSize": "2",
        "illegalCharacterReplacer": "_",
        "queueConcurrency": 3,
        "maxBitrate": QUALITY.get(quality, 9),
        "fallbackBitrate": False,  # a FLAC library shouldn't quietly fill up with MP3s
        "fallbackSearch": False,
        "overwriteFile": "n",  # a retry only fetches the tracks that are missing
        "saveArtwork": True, "coverImageTemplate": "cover", "saveArtworkArtist": True, "artistImageTemplate": "folder",
        "embeddedArtworkSize": 1200, "localArtworkSize": 1200, "localArtworkFormat": "jpg",
        "albumVariousArtists": True, "removeDuplicateArtists": True,
        "createM3U8File": False, "syncedLyrics": False, "logErrors": True, "logSearched": False,
        "executeCommand": "",
    }


def _write_config(quality: str, artist_folder: bool = True) -> None:
    d = config_dir()
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "config.json")
    cur = {}
    if os.path.isfile(path):
        try:
            with open(path) as f:
                cur = json.load(f)
        except (OSError, ValueError):
            cur = {}
    cur.update(settings_for(quality, artist_folder))
    with open(path, "w") as f:
        json.dump(cur, f, indent=2)
    arl = (db.setting("deezer_arl") or "").strip()
    login = os.path.join(d, "login.json")
    fd = os.open(login, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"arl": arl}, f)


def parse_line(line: str) -> tuple[float | None, str | None]:
    """(progress %, error message) from one line of deemix output."""
    m = _PROGRESS.search(line)
    if m:
        return float(m.group(1)), None
    if " :: " in line:
        msg = line.split(" :: ", 1)[1].strip()
        if not msg.startswith(_INFO):
            return None, line.split("] ", 1)[-1].strip()
    if line.startswith(("No valid ARL", "Not logged in")):
        return None, "Deezer login failed: check the ARL in Settings → Music"
    return None, None


_procs: dict[str, asyncio.subprocess.Process] = {}


async def download(url: str, dest: str, quality: str = "flac", on_progress: Callable[[float], None] | None = None,
                   key: str = "", artist_folder: bool = True) -> list[str]:
    """Download one Deezer album/track URL into dest (dest/Artist/Album, or dest/Album when artist_folder is False).
    Returns the error lines (empty = every track came down)."""
    why = available()
    if why:
        raise DeemixError(why)
    if not (db.setting("deezer_arl") or "").strip():
        raise DeemixError("No Deezer ARL set (Settings → Music)")
    os.makedirs(dest, exist_ok=True)
    await asyncio.to_thread(_write_config, quality, artist_folder)
    # deemix keeps cover art in $TMPDIR/deemix-imgs; give it a folder of its own so a /tmp/deemix-imgs left by
    # another user (e.g. a run as root) can't make every track fail with "ENOENT ... unlink"
    tmp = os.path.join(config_dir(), "tmp")
    os.makedirs(tmp, exist_ok=True)
    env = {**os.environ, "DEEMIX_DATA_DIR": config_dir() + "/", "NODE_ENV": "production", "TMPDIR": tmp}
    proc = await asyncio.create_subprocess_exec(
        "node", CLI, "--path", dest, "--bitrate", quality, url, cwd=config_dir(), env=env,
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    if key:
        _procs[key] = proc
    errors: list[str] = []
    try:
        assert proc.stdout
        async for raw in proc.stdout:
            line = raw.decode(errors="replace").strip()
            if not line:
                continue
            prog, err = parse_line(line)
            if prog is not None and on_progress:
                on_progress(prog)
            if err and err not in errors:
                errors.append(err)
        code = await proc.wait()
    finally:
        _procs.pop(key, None)
    if code and not errors:
        errors.append(f"deemix stopped with exit code {code}")
    return errors


def cancel(key: str) -> bool:
    p = _procs.get(key)
    if p and p.returncode is None:
        p.terminate()
        return True
    return False
