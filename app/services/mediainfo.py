"""Media info from the file itself, for naming tokens like {MediaInfo VideoCodec} and
{MediaInfo VideoDynamicRangeType}. Uses the MediaInfo CLI (what Sonarr/Radarr use; small), else ffprobe if
that's installed; with neither, naming falls back to what the release title says."""
from __future__ import annotations

import asyncio
import json
import shutil

_AUDIO = {"truehd": "TrueHD", "eac3": "EAC3", "ac3": "AC3", "aac": "AAC", "flac": "FLAC",
          "opus": "Opus", "mp3": "MP3", "pcm_s24le": "PCM", "pcm_s16le": "PCM"}
_MI_AUDIO = {"E-AC-3": "EAC3", "AC-3": "AC3", "AAC": "AAC", "FLAC": "FLAC", "Opus": "Opus", "MPEG Audio": "MP3",
             "PCM": "PCM", "MLP FBA": "TrueHD", "Vorbis": "Vorbis"}
# Matroska stores ISO 639-2/B codes (eng, ger, fre); MediaInfo reports 639-1 (en, de, fr). Keep the 3-letter form.
_LANG3 = {"en": "ENG", "de": "GER", "fr": "FRE", "es": "SPA", "it": "ITA", "ja": "JPN", "ko": "KOR", "zh": "CHI",
          "pt": "POR", "ru": "RUS", "nl": "DUT", "sv": "SWE", "no": "NOR", "nb": "NOB", "da": "DAN", "fi": "FIN",
          "pl": "POL", "hi": "HIN", "ar": "ARA", "tr": "TUR", "cs": "CZE", "hu": "HUN", "el": "GRE", "he": "HEB",
          "th": "THA", "uk": "UKR", "ro": "RUM", "is": "ICE", "id": "IND", "vi": "VIE", "ta": "TAM", "te": "TEL"}
_CHANNELS = {1: "1.0", 2: "2.0", 6: "5.1", 8: "7.1"}


async def _run(*cmd: str) -> bytes:
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.DEVNULL)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), 60)
    except asyncio.TimeoutError:
        proc.kill()
        raise
    return out or b""


async def probe(path: str) -> dict:
    try:
        if shutil.which("mediainfo"):
            return from_mediainfo(json.loads(await _run("mediainfo", "--Output=JSON", path) or b"{}"))
        if shutil.which("ffprobe"):
            out = await _run("ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", path)
            return from_ffprobe(json.loads(out or b"{}").get("streams", []))
    except (asyncio.TimeoutError, json.JSONDecodeError, OSError, ValueError, TypeError):
        pass
    return {}


def _resolution(w: int, h: int) -> int | None:
    if not (w or h):
        return None
    return (2160 if w >= 3200 or h >= 2000 else 1080 if w >= 1800 or h >= 1000
            else 720 if w >= 1200 or h >= 700 else 576 if h >= 540 else 480)


def _int(v) -> int:
    try:
        return int(str(v).split()[0].split("/")[0])
    except (ValueError, IndexError):
        return 0


def from_mediainfo(data: dict) -> dict:
    tracks = ((data or {}).get("media") or {}).get("track") or []
    video = next((t for t in tracks if t.get("@type") == "Video"), None)
    audio = [t for t in tracks if t.get("@type") == "Audio"]
    info: dict = {}
    if video:
        fmt = video.get("Format", "")
        lib = (video.get("Encoded_Library_Name") or video.get("Encoded_Library") or "").lower()
        if fmt == "HEVC":
            info["video_codec"] = "x265" if "x265" in lib else "h265"
        elif fmt == "AVC":
            info["video_codec"] = "x264" if "x264" in lib else "h264"
        elif fmt:
            info["video_codec"] = {"AV1": "AV1", "VC-1": "VC1", "MPEG Video": "mpeg2video",
                                   "VP9": "vp9"}.get(fmt, fmt.lower())
        res = _resolution(_int(video.get("Width")), _int(video.get("Height")))
        if res:
            info["resolution"] = res
        info["bit_depth"] = _int(video.get("BitDepth")) or 8
        hdr_fmt = " ".join(str(video.get(k) or "") for k in ("HDR_Format", "HDR_Format_Compatibility"))
        transfer = video.get("transfer_characteristics", "")
        dv = "Dolby Vision" in hdr_fmt
        hdr = None
        if "2094" in hdr_fmt or "HDR10+" in hdr_fmt:
            hdr = "HDR10Plus"
        elif transfer == "PQ" or "2086" in hdr_fmt or "HDR10" in hdr_fmt:
            hdr = "HDR10"
        elif transfer == "HLG" or "HLG" in hdr_fmt:
            hdr = "HLG"
        if dv:
            hdr = f"DV {hdr}" if hdr else "DV"
        info["hdr"] = hdr
    if audio:
        a = audio[0]
        fmt = a.get("Format", "")
        commercial = a.get("Format_Commercial_IfAny") or ""
        extra = a.get("Format_AdditionalFeatures") or ""
        if fmt == "DTS":
            name = ("DTS-X" if "X" in extra.split() or "DTS:X" in commercial else
                    "DTS-HD MA" if "XLL" in extra or "Master Audio" in commercial else "DTS")
        else:
            name = _MI_AUDIO.get(fmt, fmt.upper())
        if "Atmos" in commercial or ("JOC" in extra and fmt == "E-AC-3") or ("16-ch" in extra and fmt == "MLP FBA"):
            name += " Atmos"
        info["audio_codec"] = name
        ch = _int(a.get("Channels"))
        if ch:
            info["audio_channels"] = _CHANNELS.get(ch, f"{ch}.0")
        langs = sorted({_LANG3.get(str(t.get("Language", "")).lower().split("-")[0],
                                   str(t.get("Language", "")).upper()) for t in audio} - {"", "UND"})
        if langs:
            info["audio_languages"] = "[" + "+".join(langs) + "]"
    return info


def from_ffprobe(streams: list[dict]) -> dict:
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
        res = _resolution(int(video.get("width") or 0), int(video.get("height") or 0))
        if res:
            info["resolution"] = res
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
            info["audio_channels"] = _CHANNELS.get(ch, f"{ch}.0")
        langs = sorted({(s.get("tags") or {}).get("language", "").upper() for s in audio} - {"", "UND"})
        if langs:
            info["audio_languages"] = "[" + "+".join(langs) + "]"
    return info
