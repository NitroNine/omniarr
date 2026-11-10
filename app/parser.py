"""Release-title parser.

Extracts the information the decision engine needs from a Usenet release name:
resolution, source, modifiers (remux / BR-DISK / ...), season / episode numbers,
year, release group, proper/repack and a few media-info hints used for naming.

The quality names produced match the ones used in TRaSH Guides quality
profiles (Radarr style for movies, Sonarr style for TV).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# ---- Source enums (mirror Radarr / Sonarr so TRaSH SourceSpecification values work)
RADARR_SOURCE = {"unknown": 0, "cam": 1, "telesync": 2, "telecine": 3, "workprint": 4,
                 "dvd": 5, "tv": 6, "webdl": 7, "webrip": 8, "bluray": 9}
SONARR_SOURCE = {"unknown": 0, "tv": 1, "tvraw": 2, "webdl": 3, "webrip": 4,
                 "dvd": 5, "bluray": 6, "blurayraw": 7}
# Radarr QualityModifier: 0 none, 1 REGIONAL, 2 SCREENER, 3 RAWHD, 4 BRDISK, 5 REMUX
RADARR_MODIFIER = {"none": 0, "regional": 1, "screener": 2, "rawhd": 3, "brdisk": 4, "remux": 5}

_RES = [
    (2160, re.compile(r"\b(2160p|4k|uhd)\b", re.I)),
    (1080, re.compile(r"\b(1080[pi])\b", re.I)),
    (720, re.compile(r"\b(720p)\b", re.I)),
    (576, re.compile(r"\b(576[pi])\b", re.I)),
    (480, re.compile(r"\b(480[pi]|sd)\b", re.I)),
]
_REMUX = re.compile(r"\b(remux|bdremux)\b", re.I)
_BRDISK = re.compile(r"\b(complete[ ._-]?blu[ ._-]?ray|bdmv|bd(25|50|66|100)|br[ ._-]?disk|full[ ._-]?blu[ ._-]?ray|iso)\b", re.I)
_BLURAY = re.compile(r"\b(blu[ ._-]?ray|bd(rip)?|brrip|uhd[ ._-]?bluray|b[dr][ ._-]?remux)\b", re.I)
_WEBDL = re.compile(r"\b(web[ ._-]?dl|webhd|web(?![ ._-]?rip)|amzn|nf|dsnp|hmax|atvp|pcok|hulu|max)\b", re.I)
_WEBRIP = re.compile(r"\b(web[ ._-]?rip|webrip)\b", re.I)
_HDTV = re.compile(r"\b(hdtv|pdtv|sdtv|dsr|tvrip|uhdtv)\b", re.I)
_RAWHD = re.compile(r"\b(raw[ ._-]?hd|mpeg[ ._-]?2.*hdtv|hdtv.*mpeg[ ._-]?2)\b", re.I)
_DVD = re.compile(r"\b(dvd(rip)?|ntsc|pal|xvidvd|dvd[ ._-]?r|dvd9|dvd5)\b", re.I)
_CAM = re.compile(r"\b(cam|camrip|hdcam|hd[ ._-]?cam)\b", re.I)
_TS = re.compile(r"\b(ts|telesync|hdts|pdvd)\b", re.I)
_TC = re.compile(r"\b(tc|telecine|hdtc)\b", re.I)
_SCR = re.compile(r"\b(dvdscr|screener|scr|bdscr)\b", re.I)
_WP = re.compile(r"\b(workprint|wp)\b", re.I)
_REGIONAL = re.compile(r"\b(r[1-9]|regional)\b", re.I)
_PROPER = re.compile(r"\b(proper|repack\d?|rerip)\b", re.I)
_REPACKN = re.compile(r"\brepack(\d)\b", re.I)

_SXXEXX = re.compile(r"\bS(?P<s>\d{1,3})[ ._-]?(?P<eps>(?:E\d{1,4}(?:[-_]?E?\d{1,4})*))\b", re.I)
_NXNN = re.compile(r"\b(?P<s>\d{1,2})x(?P<e>\d{2,3})\b", re.I)
_SEASON_ONLY = re.compile(r"\b(?:S(?P<s>\d{1,3})(?![ ._-]?E\d)|Season[ ._-]?(?P<s2>\d{1,3}))\b", re.I)
_DAILY = re.compile(r"\b(?P<y>(19|20)\d{2})[ ._-](?P<m>\d{2})[ ._-](?P<d>\d{2})\b")
_YEAR = re.compile(r"(?<![0-9])[(\[ ._-]?((?:19|20)\d{2})[)\] ._-]?(?![0-9p])")
_GROUP = re.compile(r"-(?P<g>[A-Za-z0-9][A-Za-z0-9._]*?)(?:\[[^\]]*\])?(?:\.(?:nzb|mkv|mp4|avi))?$")

_CODECS = [
    ("x265", re.compile(r"\bx[ ._-]?265\b", re.I)),
    ("h265", re.compile(r"\b(h[ ._-]?265|hevc)\b", re.I)),
    ("x264", re.compile(r"\bx[ ._-]?264\b", re.I)),
    ("h264", re.compile(r"\b(h[ ._-]?264|avc)\b", re.I)),
    ("AV1", re.compile(r"\bav1\b", re.I)),
    ("VC1", re.compile(r"\bvc[ ._-]?1\b", re.I)),
    ("XviD", re.compile(r"\bxvid\b", re.I)),
]
_AUDIO = [
    ("TrueHD Atmos", re.compile(r"\btruehd[ ._-]?(?:\d[ ._]\d[ ._-]?)?atmos\b", re.I)),
    ("TrueHD", re.compile(r"\btruehd\b", re.I)),
    ("DTS-HD MA", re.compile(r"\bdts[ ._-]?(hd[ ._-]?)?ma\b", re.I)),
    ("DTS-X", re.compile(r"\bdts[ ._-]?x\b", re.I)),
    ("DTS", re.compile(r"\bdts\b", re.I)),
    ("EAC3 Atmos", re.compile(r"\b(e[ ._-]?ac[ ._-]?3|ddp|dd\+)[ ._-]?(?:\d[ ._]\d[ ._-]?)?atmos\b", re.I)),
    ("EAC3", re.compile(r"\b(e[ ._-]?ac[ ._-]?3|ddp|dd\+)", re.I)),
    ("AC3", re.compile(r"\b(ac3|dd(?=[ ._-]?\d)|dolby[ ._-]?digital)", re.I)),
    ("FLAC", re.compile(r"\bflac\b", re.I)),
    ("AAC", re.compile(r"\baac", re.I)),
    ("Opus", re.compile(r"\bopus\b", re.I)),
]
_CHANNELS = re.compile(r"(?<![0-9])([1-9])[ ._]([01])(?![0-9])")
_HDR = [
    ("DV HDR10Plus", re.compile(r"(?=.*\b(dv|dovi|dolby[ ._-]?vision)\b)(?=.*\bhdr10(\+|p(lus)?)\b)", re.I)),
    ("DV HDR10", re.compile(r"(?=.*\b(dv|dovi|dolby[ ._-]?vision)\b)(?=.*\bhdr(10)?\b)", re.I)),
    ("DV", re.compile(r"\b(dv|dovi|dolby[ ._-]?vision)\b", re.I)),
    ("HDR10Plus", re.compile(r"\bhdr10(\+|p(lus)?)", re.I)),
    ("HDR10", re.compile(r"\bhdr10\b", re.I)),
    ("HDR", re.compile(r"\bhdr\b", re.I)),
    ("HLG", re.compile(r"\bhlg\b", re.I)),
]
_EDITIONS = re.compile(
    r"\b(director'?s[ ._-]?cut|extended(?:[ ._-]?(?:cut|edition))?|unrated|uncut|theatrical(?:[ ._-]?cut)?|"
    r"imax(?:[ ._-]?enhanced)?|remastered|criterion|special[ ._-]?edition|ultimate[ ._-]?(?:cut|edition)|"
    r"final[ ._-]?cut|open[ ._-]?matte|anniversary[ ._-]?edition)\b", re.I)
_LANGS = {
    2: re.compile(r"\b(french|vff|vfq|vf2|truefrench|vostfr)\b", re.I),
    4: re.compile(r"\b(german|deutsch)\b", re.I),
    3: re.compile(r"\b(spanish|espanol|castellano|latino)\b", re.I),
    5: re.compile(r"\b(italian|ita)\b", re.I),
    8: re.compile(r"\b(japanese|jpn)\b", re.I),
    10: re.compile(r"\b(chinese|chs|cht|mandarin|cantonese)\b", re.I),
    21: re.compile(r"\b(korean|kor)\b", re.I),
    7: re.compile(r"\b(dutch|nl)\b", re.I),
    11: re.compile(r"\b(russian|rus)\b", re.I),
}
_MULTI = re.compile(r"\b(multi|dual[ ._-]?audio|dl)\b", re.I)
_3D = re.compile(r"\b(3d|sbs|half[ ._-]?ou|half[ ._-]?sbs|hsbs)\b", re.I)


@dataclass
class ParsedRelease:
    title: str
    clean_name: str = ""
    year: int | None = None
    resolution: int = 0
    source: str = "unknown"  # cam/telesync/telecine/workprint/dvd/tv/webdl/webrip/bluray/tvraw/blurayraw
    modifier: str = "none"  # none/remux/brdisk/rawhd/regional/screener
    season: int | None = None
    episodes: list[int] = field(default_factory=list)
    full_season: bool = False
    air_date: str | None = None
    group: str | None = None
    proper: bool = False
    revision: int = 1
    codec: str | None = None
    audio: str | None = None
    channels: str | None = None
    hdr: str | None = None
    edition: str | None = None
    languages: list[int] = field(default_factory=list)
    is_3d: bool = False

    # ---- quality names -------------------------------------------------
    def quality_name(self, kind: str = "movie") -> str:
        r = self.resolution
        res = f"{r}p" if r else ""
        if self.modifier == "brdisk":
            return "BR-DISK"
        if self.modifier == "rawhd" or self.source == "tvraw":
            return "Raw-HD"
        if self.modifier == "remux" or self.source == "blurayraw":
            r = r or 1080
            return f"Bluray-{r}p Remux" if kind == "tv" else f"Remux-{r}p"
        s = self.source
        if s == "bluray":
            return f"Bluray-{res or '1080p'}"
        if s in ("webdl", "webrip"):
            label = "WEBDL" if s == "webdl" else "WEBRip"
            return f"{label}-{res or '480p'}"
        if s == "tv":
            if r in (720, 1080, 2160):
                return f"HDTV-{res}"
            return "SDTV"
        if s == "dvd":
            return "DVD"
        if kind == "movie":
            if self.modifier == "screener":
                return "DVDSCR"
            if self.modifier == "regional":
                return "REGIONAL"
            return {"cam": "CAM", "telesync": "TELESYNC", "telecine": "TELECINE",
                    "workprint": "WORKPRINT"}.get(s, "Unknown")
        return "Unknown"

    def source_id(self, kind: str) -> int:
        if kind == "tv":
            s = self.source
            if self.modifier == "remux":
                s = "blurayraw"
            return SONARR_SOURCE.get(s, 0)
        return RADARR_SOURCE.get(self.source, 0)

    def modifier_id(self) -> int:
        return RADARR_MODIFIER.get(self.modifier, 0)

    def release_type(self) -> int:
        """Sonarr ReleaseType: 1 single episode, 2 multi-episode, 3 season pack."""
        if self.full_season:
            return 3
        if len(self.episodes) > 1:
            return 2
        return 1 if self.episodes else 0


def _first(patterns, text):
    for name, rx in patterns:
        if rx.search(text):
            return name
    return None


def parse(title: str) -> ParsedRelease:
    t = title.strip()
    norm = re.sub(r"[._]", " ", t)
    p = ParsedRelease(title=t)

    for res, rx in _RES:
        if rx.search(norm):
            p.resolution = res
            break

    # modifiers / sources (order matters)
    if _BRDISK.search(norm) and not _REMUX.search(norm) and not re.search(r"\b(x26[45]|h ?26[45] ?-?[a-z]*$)", norm, re.I):
        p.modifier = "brdisk"
        p.source = "bluray"
    elif _REMUX.search(norm):
        p.modifier = "remux"
        p.source = "bluray"
    elif _BLURAY.search(norm):
        p.source = "bluray"
    elif _WEBRIP.search(norm):
        p.source = "webrip"
    elif _WEBDL.search(norm):
        p.source = "webdl"
    elif _RAWHD.search(norm):
        p.source = "tv"
        p.modifier = "rawhd"
    elif _HDTV.search(norm):
        p.source = "tv"
    elif _SCR.search(norm):
        p.source = "dvd"
        p.modifier = "screener"
    elif _DVD.search(norm):
        p.source = "dvd"
    elif _CAM.search(norm):
        p.source = "cam"
    elif _TS.search(norm):
        p.source = "telesync"
    elif _TC.search(norm):
        p.source = "telecine"
    elif _WP.search(norm):
        p.source = "workprint"
    if p.source == "unknown" and p.resolution >= 720:
        # Most untagged HD usenet releases are WEB-DL
        p.source = "webdl"
    if p.source == "dvd" and not p.resolution:
        p.resolution = 480
    if p.source == "bluray" and not p.resolution and p.modifier != "brdisk":
        p.resolution = 1080 if "1080" in norm else 480

    p.proper = bool(_PROPER.search(norm))
    m = _REPACKN.search(norm)
    p.revision = int(m.group(1)) + 1 if m else (2 if p.proper else 1)

    # TV numbering
    m = _SXXEXX.search(norm)
    if m:
        p.season = int(m.group("s"))
        nums = [int(x) for x in re.findall(r"\d+", m.group("eps"))]
        if len(nums) == 2 and "-" in m.group("eps") and nums[1] > nums[0]:
            nums = list(range(nums[0], nums[1] + 1))
        p.episodes = nums
    else:
        m = _NXNN.search(norm)
        if m:
            p.season, p.episodes = int(m.group("s")), [int(m.group("e"))]
        else:
            m = _DAILY.search(norm)
            if m:
                p.air_date = f"{m.group('y')}-{m.group('m')}-{m.group('d')}"
            else:
                m = _SEASON_ONLY.search(norm)
                if m:
                    p.season = int(m.group("s") or m.group("s2"))
                    p.full_season = True

    # year: take the last plausible year that precedes quality tags
    years = [int(y) for y in _YEAR.findall(norm)]
    if years and not p.air_date:
        p.year = years[-1]

    m = _GROUP.search(t)
    if m:
        g = m.group("g")
        if not re.fullmatch(r"(dl|hd|\d+|x26[45]|h26[45])", g, re.I):
            p.group = g

    p.codec = _first(_CODECS, norm)
    p.audio = _first(_AUDIO, norm)
    m = _CHANNELS.search(norm)
    if m and p.audio:
        p.channels = f"{m.group(1)}.{m.group(2)}"
    p.hdr = _first(_HDR, norm)
    m = _EDITIONS.search(norm)
    if m:
        p.edition = re.sub(r"[._]", " ", m.group(1)).title()
    p.languages = [lid for lid, rx in _LANGS.items() if rx.search(norm)]
    if _MULTI.search(norm) and p.languages:
        p.languages.append(1)
    p.is_3d = bool(_3D.search(norm))

    # name portion before year / SxxExx / quality
    cut = len(norm)
    for rx in (_SXXEXX, _DAILY, _SEASON_ONLY, re.compile(r"\b(19|20)\d{2}\b"), _RES[0][1], _RES[1][1], _RES[2][1]):
        mm = rx.search(norm)
        if mm and mm.start() > 0:
            cut = min(cut, mm.start())
    p.clean_name = re.sub(r"\s+", " ", re.sub(r"[()\[\]]", " ", norm[:cut])).strip()
    return p


def normalize_title(s: str) -> str:
    """Lower-case, alphanumeric only — used to compare release names to media titles."""
    s = s.lower().replace("&", "and")
    s = re.sub(r"['’`]", "", s)
    s = re.sub(r"\b(the|a|an|and)\b", " ", s)
    return re.sub(r"[^a-z0-9]+", "", s)
