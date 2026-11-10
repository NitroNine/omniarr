"""Renders Radarr/Sonarr-style naming formats (as published by TRaSH Guides).

Supports token prefixes/suffixes ("{[Quality Full]}", "{-Release Group}"),
dotted/underscored tokens ("{Movie.CleanTitle}"), number padding
("{season:00}"), truncation ("{Episode CleanTitle:90}") and escaped braces
("{{Edition Tags}}" -> "{Director's Cut}", as Radarr does).
"""
from __future__ import annotations

import re
from typing import Any

_TOKEN = re.compile(
    r"\{(?P<prefix>[- ._\[(]*)(?P<token>(?:[a-z0-9]+)(?:(?P<sep>[- ._]+)(?:[a-z0-9]+))*)"
    r"(?::(?P<fmt>[ ,a-z0-9+-]+(?<![- ])))?(?P<suffix>[- ._)\]]*)\}",
    re.I,
)
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def clean_title(title: str) -> str:
    t = title.replace("&", "and")
    t = re.sub(r"[/\\]", " ", t)
    t = re.sub(r"[:;'’!?,\"*<>|.]", "", t)
    return re.sub(r"\s+", " ", t).strip()


def strip_year(title: str) -> str:
    return re.sub(r"\s*\((19|20)\d{2}\)\s*$", "", title).strip()


def _fmt_value(val: Any, fmt: str | None) -> str:
    if val is None or val == "" or val == []:
        return ""
    if fmt:
        if isinstance(val, int) and re.fullmatch(r"0+", fmt):
            return str(val).zfill(len(fmt))
        if re.fullmatch(r"\d+", fmt) and isinstance(val, str):
            n = int(fmt)
            return val if len(val) <= n else val[: n - 3].rstrip() + "..."
    return str(val)


def render(fmt: str, values: dict[str, Any]) -> str:
    """values keys are lowercase token names with single spaces, e.g. 'movie cleantitle'."""

    def repl(m: re.Match) -> str:
        raw = m.group("token")
        sep = m.group("sep")
        key = re.sub(r"[- ._]+", " ", raw).lower()
        if key not in values:
            # Air-Date style tokens use '-' as part of the name
            key = raw.lower()
            if key not in values:
                return ""
        val = _fmt_value(values[key], m.group("fmt"))
        if not val:
            return ""
        if sep in (".", "_"):
            val = re.sub(r"\s+", sep, val)
        return f"{m.group('prefix')}{val}{m.group('suffix')}"

    out = _TOKEN.sub(repl, fmt)
    # tidy up empty wrappers left over by missing values
    for _ in range(3):
        out = re.sub(r"\{(?:[a-z]+-)?\}|\[\s*\]|\(\s*\)", "", out, flags=re.I)
    out = re.sub(r"\s+-\s*$", "", out)
    out = re.sub(r"\s+-\s+(?=\[)", " ", out)
    out = re.sub(r" {2,}", " ", out)
    out = re.sub(r"\.{2,}", ".", out)
    return out.strip(" .-")


def safe(name: str) -> str:
    name = name.replace(":", " -").replace("?", "")
    name = _ILLEGAL.sub("", name)
    return re.sub(r"\s{2,}", " ", name).strip(" .")


def media_values(rel, quality: str, custom_formats: list[str], mediainfo: dict | None = None) -> dict[str, Any]:
    mi = mediainfo or {}
    qfull = quality + (" Proper" if rel.proper and rel.revision <= 2 else f" REPACK{rel.revision - 1}" if rel.revision > 2 else "")
    return {
        "quality full": qfull,
        "quality title": quality,
        "custom formats": " ".join(custom_formats),
        "mediainfo videocodec": mi.get("video_codec") or rel.codec,
        "mediainfo audiocodec": mi.get("audio_codec") or rel.audio,
        "mediainfo audiochannels": mi.get("audio_channels") or rel.channels,
        "mediainfo videodynamicrangetype": mi.get("hdr") or rel.hdr,
        "mediainfo videobitdepth": mi.get("bit_depth"),
        "mediainfo audiolanguages": mi.get("audio_languages"),
        "mediainfo 3d": "3D" if rel.is_3d else "",
        "release group": rel.group,
        "edition tags": rel.edition,
        "original title": rel.title,
    }


def movie_values(movie: dict) -> dict[str, Any]:
    return {
        "movie title": movie["title"],
        "movie cleantitle": clean_title(movie["title"]),
        "movie titlethe": movie["title"],
        "release year": movie.get("year"),
        "imdbid": movie.get("imdb_id"),
        "tmdbid": movie.get("tmdb_id"),
    }


def series_values(series: dict) -> dict[str, Any]:
    title = series["title"]
    year = series.get("year")
    no_year = strip_year(title)
    return {
        "series title": title,
        "series cleantitle": clean_title(title),
        "series cleantitlewithoutyear": clean_title(no_year),
        "series titlewithoutyear": no_year,
        "series cleantitleyear": f"{clean_title(no_year)} {year}" if year else clean_title(no_year),
        "series titleyear": f"{no_year} ({year})" if year else no_year,
        "series year": year,
        "tvdbid": series.get("tvdb_id"),
        "imdbid": series.get("imdb_id"),
        "tmdbid": series.get("tmdb_id"),
    }


def episode_values(eps: list[dict]) -> dict[str, Any]:
    eps = sorted(eps, key=lambda e: (e["season"], e["episode"]))
    first = eps[0]
    titles = []
    for e in eps:
        t = re.sub(r"\s*\(\d+\)$", "", e.get("title") or "")
        if t and t not in titles:
            titles.append(t)
    title = " + ".join(titles) if titles else "TBA"
    return {
        "season": first["season"],
        "episode": first["episode"],
        "absolute": first.get("absolute"),
        "episode title": title,
        "episode cleantitle": clean_title(title),
        "air-date": first.get("air_date"),
        "air date": first.get("air_date"),
        "_extra_episodes": [e["episode"] for e in eps[1:]],
    }


def render_episode(fmt: str, values: dict[str, Any]) -> str:
    """Render, then expand SxxEyy into a multi-episode range (S01E01-E02)."""
    out = render(fmt, values)
    extra = values.get("_extra_episodes") or []
    if extra:
        first = f"E{str(values['episode']).zfill(2)}"
        out = out.replace(first, first + "".join(f"-E{str(e).zfill(2)}" for e in extra), 1)
    return out
