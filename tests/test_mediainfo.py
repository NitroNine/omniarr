"""MediaInfo JSON (what the image ships) and ffprobe JSON parse to the same naming facts."""
import os
import time

from app.services import downloader
from app.services.mediainfo import from_mediainfo


def _mi(video: dict, *audio: dict) -> dict:
    return {"media": {"track": [{"@type": "General"}, {"@type": "Video", **video},
                                *({"@type": "Audio", **a} for a in audio)]}}


def test_x264_aac():
    assert from_mediainfo(_mi({"Format": "AVC", "Width": "1920", "Height": "1080", "BitDepth": "8",
                               "Encoded_Library_Name": "x264"},
                              {"Format": "AAC", "Channels": "2", "Language": "en"})) == {
        "video_codec": "x264", "resolution": 1080, "bit_depth": 8, "hdr": None,
        "audio_codec": "AAC", "audio_channels": "2.0", "audio_languages": "[ENG]"}


def test_x265_hdr10_eac3():
    info = from_mediainfo(_mi({"Format": "HEVC", "Width": "3840", "Height": "2160", "BitDepth": "10",
                               "HDR_Format": "SMPTE ST 2086", "HDR_Format_Compatibility": "HDR10",
                               "transfer_characteristics": "PQ", "Encoded_Library_Name": "x265"},
                              {"Format": "E-AC-3", "Format_Commercial_IfAny": "Dolby Digital Plus",
                               "Channels": "6", "Language": "fr"}))
    assert (info["video_codec"], info["resolution"], info["bit_depth"], info["hdr"]) == ("x265", 2160, 10, "HDR10")
    assert (info["audio_codec"], info["audio_channels"], info["audio_languages"]) == ("EAC3", "5.1", "[FRE]")


def test_hlg_scope_and_two_languages():
    assert from_mediainfo(_mi({"Format": "HEVC", "Width": "1280", "Height": "720", "BitDepth": "10",
                               "transfer_characteristics": "HLG"}, {"Format": "FLAC", "Channels": "2"}))["hdr"] == "HLG"
    info = from_mediainfo(_mi({"Format": "AVC", "Width": "1920", "Height": "800"},
                              {"Format": "AC-3", "Channels": "6", "Language": "de"},
                              {"Format": "AC-3", "Channels": "6", "Language": "en"}))
    assert (info["resolution"], info["video_codec"], info["audio_languages"]) == (1080, "h264", "[ENG+GER]")


def test_dolby_vision_atmos_dts():
    info = from_mediainfo(_mi({"Format": "HEVC", "Width": "3840", "Height": "2160", "BitDepth": "10",
                               "HDR_Format": "Dolby Vision / SMPTE ST 2086",
                               "HDR_Format_Compatibility": "Blu-ray / HDR10", "transfer_characteristics": "PQ"},
                              {"Format": "MLP FBA", "Format_Commercial_IfAny": "Dolby TrueHD with Dolby Atmos",
                               "Channels": "8"}))
    assert (info["hdr"], info["audio_codec"], info["audio_channels"]) == ("DV HDR10", "TrueHD Atmos", "7.1")
    plus = from_mediainfo(_mi({"Format": "HEVC", "HDR_Format": "SMPTE ST 2094 App 4",
                               "HDR_Format_Compatibility": "HDR10+ Profile A / HDR10"},
                              {"Format": "E-AC-3", "Format_AdditionalFeatures": "JOC", "Channels": "6"}))
    assert (plus["hdr"], plus["audio_codec"]) == ("HDR10Plus", "EAC3 Atmos")
    ma = from_mediainfo(_mi({"Format": "AVC"}, {"Format": "DTS", "Format_AdditionalFeatures": "XLL",
                                                "Format_Commercial_IfAny": "DTS-HD Master Audio", "Channels": "6"}))
    assert ma["audio_codec"] == "DTS-HD MA"
    x = from_mediainfo(_mi({"Format": "AVC"}, {"Format": "DTS", "Format_AdditionalFeatures": "XLL X", "Channels": "8"}))
    assert x["audio_codec"] == "DTS-X"


def test_empty_output():
    assert from_mediainfo({}) == {} and from_mediainfo({"media": None}) == {}


def test_old_nzb_copies_are_removed(env):
    d = os.path.join(downloader.conf_dir(), "nzb")
    os.makedirs(d, exist_ok=True)
    old, new = os.path.join(d, "old.nzb.queued"), os.path.join(d, "new.nzb.queued")
    for p in (old, new):
        open(p, "w").close()
    os.utime(old, (time.time() - 8 * 86400,) * 2)
    assert downloader.clean_nzb_dir() == 1
    assert sorted(os.listdir(d)) == ["new.nzb.queued"]


def test_nzbget_logs_without_detail_or_debug(env):
    o = downloader.options()
    assert o["DetailTarget"] == "none" and o["DebugTarget"] == "none" and o["WriteLog"] == "rotate"
