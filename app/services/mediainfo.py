"""Optional ffprobe-based media info, used for naming tokens like
{Mediainfo VideoCodec} and {MediaInfo VideoDynamicRangeType}. Falls back to
what the release title says when ffprobe is unavailable."""
from __future__ import annotations

import asyncio
import json
import shutil

_AUDIO = {"truehd": "TrueHD", "eac3": "EAC3", "ac3": "AC3", "aac": "AAC", "flac": "FLAC",
          "opus": "Opus", "mp3": "MP3", "pcm_s24le": "PCM", "pcm_s16le": "PCM"}


async def probe(path: str) -> dict:
    if not shutil.which("ffprobe"):
        return {}
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), 60)
        streams = json.loads(out or b"{}").get("streams", [])
    except (asyncio.TimeoutError, json.JSONDecodeError, OSError):
        return {}
    info: dict = {}
    video = next((s for s in streams if s.get("codec_type") == "video" and s.get("disposition", {}).get("attached_pic") != 1), None)
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    if video:
        codec = video.get("codec_name", "")
        enc = (video.get("tags") or {}).get("ENCODER", "") or ""
        if codec == "hevc":
            info["video_codec"] = "x265" if "x265" in enc else "h265"
        elif codec == "h264":
            info["video_codec"] = "x264" if "x264" in enc else "h264"
        elif codec:
            info["video_codec"] = codec.upper() if codec in ("av1", "vc1") else codec
        w, h = int(video.get("width") or 0), int(video.get("height") or 0)
        if w or h:
            info["resolution"] = (2160 if w >= 3200 or h >= 2000 else 1080 if w >= 1800 or h >= 1000
                                  else 720 if w >= 1200 or h >= 700 else 576 if h >= 540 else 480)
        pix = video.get("pix_fmt", "")
        info["bit_depth"] = 10 if "10" in pix else 12 if "12" in pix else 8
        transfer = video.get("color_transfer", "")
        side = [d.get("side_data_type", "") for d in video.get("side_data_list", [])]
        dv = any("DOVI" in s or "Dolby Vision" in s for s in side)
        hdr10p = any("HDR Dynamic Metadata" in s or "2094" in s for s in side)
        hdr = None
        if transfer == "smpte2084":
            hdr = "HDR10Plus" if hdr10p else "HDR10"
        elif transfer == "arib-std-b67":
            hdr = "HLG"
        if dv:
            hdr = f"DV {hdr}" if hdr else "DV"
        info["hdr"] = hdr
    if audio:
        a = audio[0]
        name = _AUDIO.get(a.get("codec_name", ""), (a.get("codec_name") or "").upper())
        profile = (a.get("profile") or "")
        if a.get("codec_name") == "dts":
            name = "DTS-HD MA" if "MA" in profile else "DTS-X" if "X" in profile else "DTS"
        if "atmos" in (a.get("tags", {}).get("title", "") + profile).lower():
            name += " Atmos"
        info["audio_codec"] = name
        ch = a.get("channels")
        if ch:
            info["audio_channels"] = {1: "1.0", 2: "2.0", 6: "5.1", 8: "7.1"}.get(ch, f"{ch}.0")
        langs = sorted({(s.get("tags") or {}).get("language", "").upper() for s in audio} - {"", "UND"})
        if langs:
            info["audio_languages"] = "[" + "+".join(langs) + "]"
    return info
