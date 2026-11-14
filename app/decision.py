"""Decides which releases are acceptable for a movie / episode set and ranks them,
following the same rules Radarr/Sonarr use with TRaSH quality profiles."""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from . import db, trash
from .parser import ParsedRelease, dual_audio, normalize_title, not_english, parse
from .services.newznab import Release


@dataclass
class Decision:
    release: Release
    parsed: ParsedRelease
    quality: str
    rank: int | None
    score: int
    custom_formats: list[str]
    accepted: bool = True
    reasons: list[str] = field(default_factory=list)
    episode_ids: list[int] = field(default_factory=list)
    dual_reason: str | None = None  # rejected as dual audio: allowed when nothing English-only is acceptable

    def other_reasons(self) -> list[str]:
        return [r for r in self.reasons if r != self.dual_reason]

    def reject(self, why: str):
        self.accepted = False
        self.reasons.append(why)

    def sort_key(self):
        return (self.rank if self.rank is not None else 999, -self.score,
                -(self.parsed.revision), -(self.release.size or 0))

    def to_dict(self) -> dict:
        r = self.release
        return {
            "title": r.title, "guid": r.guid, "link": r.link, "indexer": r.indexer, "indexer_id": r.indexer_id,
            "size": r.size, "age_days": round(r.age_days, 1) if r.age_days is not None else None,
            "grabs": r.grabs, "quality": self.quality, "score": self.score,
            "custom_formats": self.custom_formats, "accepted": self.accepted, "reasons": self.reasons,
            "episode_ids": self.episode_ids,
        }


def library_key(kind: str, tier: str) -> str:
    return f"{'movies' if kind == 'movie' else 'tv'}_{tier}"


def profile_for(kind: str, tier: str, apply_overrides: bool = True, item: dict | None = None) -> trash.Profile:
    """The TRaSH profile for a library, with the user's quality limits applied:
    which qualities are allowed (floor/ceiling), the upgrade-until cutoff,
    whether upgrades happen at all, and the custom-format score cutoff.

    `item` (a movies / series row) with its own `profile` uses that TRaSH profile instead; of the library's
    limits only "upgrades on/off" and the score cutoff carry over (the quality lists belong to the library's profile)."""
    s = db.get_settings()
    own = (item or {}).get("profile")
    key = own or s[f"profile_{library_key(kind, tier)}"]
    st = trash.ensure_loaded()
    if own and own not in st.profiles[trash._app(kind)]:
        key = s[f"profile_{library_key(kind, tier)}"]  # that profile is gone from TRaSH: back to the library's
        own = None
    p = st.build_profile(kind, key, s.get("trash_score_set") or "default")
    ov = (s.get("quality_overrides") or {}).get(library_key(kind, tier)) or {}
    if own:
        ov = {k: v for k, v in ov.items() if k in ("upgrades", "cutoff_score")}
    if not apply_overrides or not ov:
        return p
    import copy
    p.items = copy.deepcopy(p.items)
    if ov.get("allowed"):
        allowed = set(ov["allowed"])
        for it in p.items:
            it["allowed"] = it["name"] in allowed
    if ov.get("cutoff"):
        p.cutoff = ov["cutoff"]
    allowed_items = [it for it in p.items if it.get("allowed")]
    if allowed_items and not any(it["name"] == p.cutoff and it.get("allowed") for it in p.items) \
            and not any(p.cutoff in it.get("items", []) and it.get("allowed") for it in p.items):
        p.cutoff = allowed_items[0]["name"]  # cutoff must be an allowed quality: use the best allowed
    if ov.get("upgrades") is False:
        p.upgrade_allowed = False
    if ov.get("cutoff_score") not in (None, ""):
        p.cutoff_format_score = int(ov["cutoff_score"])
    return p


def _blocklisted(rel: Release) -> bool:
    return bool(db.one("SELECT 1 FROM blocklist WHERE release_title=? OR (guid IS NOT NULL AND guid=?)",
                       (rel.title, rel.guid)))


def _size_ok(d: Decision, profile: trash.Profile, runtime: int, n_eps: int = 1) -> None:
    lim = profile.quality_size.get(d.quality)
    if not lim or not runtime or not d.release.size:
        return
    mb_per_min = d.release.size / 1024 / 1024 / max(n_eps, 1) / runtime
    if mb_per_min < float(lim.get("min", 0)):
        d.reject(f"Too small ({mb_per_min:.1f} MB/min < {lim['min']})")
    mx = float(lim.get("max", 0) or 0)
    if mx and mx < 1000 and mb_per_min > mx:
        d.reject(f"Too large ({mb_per_min:.1f} MB/min > {mx})")


# Best to worst, whatever a profile allows (TRaSH profiles list the qualities they don't allow in no useful order).
# Movie and TV names for the same thing share a rank.
_ORDER = [["Remux-2160p", "Bluray-2160p Remux"], ["Bluray-2160p"], ["WEBDL-2160p"], ["WEBRip-2160p"], ["HDTV-2160p"],
          ["Remux-1080p", "Bluray-1080p Remux"], ["Bluray-1080p"], ["WEBDL-1080p"], ["WEBRip-1080p"], ["HDTV-1080p"],
          ["Bluray-720p"], ["WEBDL-720p"], ["WEBRip-720p"], ["HDTV-720p"], ["Bluray-576p"], ["Bluray-480p"],
          ["WEBDL-480p"], ["WEBRip-480p"], ["DVD-R"], ["DVD"], ["SDTV"], ["DVDSCR"], ["REGIONAL"], ["TELECINE"],
          ["TELESYNC"], ["CAM"], ["WORKPRINT"]]
_GLOBAL = {name: i for i, names in enumerate(_ORDER) for name in names}


def quality_order(quality: str | None, profile: trash.Profile | None = None) -> int | None:
    """Overall rank of a quality (lower is better), or of a profile group such as "WEB 2160p" (its best member)."""
    if not quality:
        return None
    if quality in _GLOBAL:
        return _GLOBAL[quality]
    for item in (profile.items if profile else []):
        if item["name"] == quality:
            ranks = [_GLOBAL[q] for q in item.get("items", []) if q in _GLOBAL]
            return min(ranks) if ranks else None
    return None


def _upgrade_check(d: Decision, profile: trash.Profile, quality: str | None, score: int | None) -> None:
    """Existing file present: only accept real upgrades, until cutoff is reached."""
    if not quality:
        return
    if not (profile.upgrade_allowed and db.setting("upgrades_enabled")):
        d.reject("Already have a file (upgrades disabled)")
        return
    if profile.quality_rank(quality) is None:
        # The file is a quality this profile doesn't allow (e.g. a Remux kept in a "Bluray + WEB" library).
        # Only replace it with something better overall, and never once it's at or above the profile's cutoff.
        g_cur, g_cut, g_new = quality_order(quality), quality_order(profile.cutoff, profile), quality_order(d.quality)
        if g_cur is not None:
            if g_cut is not None and g_cur <= g_cut:
                d.reject(f"Existing {quality} is already as good as this library's goal ({profile.cutoff})")
                return
            if g_new is None or g_new >= g_cur:
                d.reject(f"Not better than the existing {quality}")
                return
    cur_rank = profile.quality_rank(quality)
    cur_rank = 999 if cur_rank is None else cur_rank
    cur_score = score or 0
    cutoff_rank = profile.cutoff_rank()
    quality_met = cur_rank <= cutoff_rank
    if quality_met and cur_score >= profile.cutoff_format_score:
        d.reject("Existing file already meets cutoff")
        return
    if d.rank is None:
        return
    if d.rank < cur_rank and not quality_met:
        return  # quality upgrade
    if d.rank <= cur_rank and d.score >= cur_score + profile.min_upgrade_format_score:
        return  # custom-format upgrade
    d.reject(f"Not an upgrade over existing {quality} ({cur_score})")


def evaluate(rel: Release, kind: str, profile: trash.Profile) -> Decision:
    p = parse(rel.title)
    quality = p.quality_name(kind)
    score, cfs = trash.score_release(profile, p)
    d = Decision(rel, p, quality, profile.quality_rank(quality), score, cfs)
    if d.rank is None:
        d.reject(f"Quality {quality} not allowed in {profile.name}")
    if score < profile.min_format_score:
        d.reject(f"Custom format score {score} below minimum {profile.min_format_score}")
    if _blocklisted(rel):
        d.reject("Blocklisted")
    if db.setting("english_only"):
        why = not_english(rel.title)
        if why:
            d.reject(f"Not English-only ('{why}')")
            if dual_audio(rel.title):
                d.dual_reason = d.reasons[-1]
    return d


def _allow_dual(out: list[Decision], covered: set[int] | None = None) -> None:
    """Accept dual-audio releases that fail nothing else, when no English-only release is acceptable
    (for TV: for the episodes no acceptable English-only release covers)."""
    for d in out:
        if d.accepted or not d.dual_reason or d.other_reasons():
            continue
        if covered is None or not covered.intersection(d.episode_ids):
            d.accepted = True
            d.reasons = []
            d.dual_reason = None


def _title_matches(parsed_name: str, *titles: str | None) -> bool:
    n = normalize_title(parsed_name)
    if not n:
        return False
    for t in titles:
        if t and normalize_title(t) == n:
            return True
    return False


# ----------------------------------------------------------------- movies --
def decide_movie(movie: dict, releases: list[Release], dual_fallback: bool = True) -> list[Decision]:
    """dual_fallback: a dual-audio release may be grabbed when nothing English-only is acceptable (searches;
    RSS only sees a few releases, so it waits for English-only ones)."""
    profile = profile_for("movie", movie["tier"], item=movie)
    out = []
    for rel in releases:
        d = evaluate(rel, "movie", profile)
        p = d.parsed
        if p.season is not None and p.episodes:
            d.reject("Looks like a TV episode")
        if not _title_matches(p.clean_name, movie["title"], movie.get("original_title")) and not (
                rel.imdb_id and movie.get("imdb_id") and rel.imdb_id.lstrip("t") == movie["imdb_id"].lstrip("t")):
            d.reject(f"Title mismatch ('{p.clean_name}')")
        if p.year and movie.get("year") and abs(p.year - movie["year"]) > 1:
            d.reject(f"Year mismatch ({p.year})")
        _size_ok(d, profile, movie.get("runtime") or 0)
        _upgrade_check(d, profile, movie.get("file_quality"), movie.get("file_score"))
        out.append(d)
    if dual_fallback and not any(d.accepted for d in out):
        _allow_dual(out)
    return sorted(out, key=lambda x: (not x.accepted, x.sort_key()))


# --------------------------------------------------------------- episodes --
def decide_episodes(series: dict, episodes: list[dict], releases: list[Release],
                    dual_fallback: bool = True) -> list[Decision]:
    """`episodes` = the episodes we're looking for (wanted). Releases are mapped
    onto them; a release covering any non-wanted / already-good episode is
    only accepted if every covered episode benefits."""
    profile = profile_for("tv", series["tier"], item=series)
    all_eps = db.q("SELECT * FROM episodes WHERE series_id=?", (series["id"],))
    by_se = {(e["season"], e["episode"]): e for e in all_eps}
    by_date = {e["air_date"]: e for e in all_eps if e.get("air_date")}
    by_abs = {e["absolute"]: e for e in all_eps if e.get("absolute")}
    wanted_ids = {e["id"] for e in episodes}
    from .naming import strip_year
    out = []
    for rel in releases:
        d = evaluate(rel, "tv", profile)
        p = d.parsed
        if not (_title_matches(p.clean_name, series["title"], strip_year(series["title"]),
                               f"{strip_year(series['title'])} {series.get('year') or ''}")
                or (rel.tvdb_id and rel.tvdb_id == series["tvdb_id"])):
            d.reject(f"Series mismatch ('{p.clean_name}')")
        covered: list[dict] = []
        if p.full_season and p.season is not None:
            covered = [e for e in all_eps if e["season"] == p.season]
            aired = [e for e in covered if e.get("air_date")]
            if len(aired) < len(covered):
                d.reject("Season pack for a season that hasn't finished airing")
        elif p.season is not None and p.episodes:
            covered = [by_se[(p.season, n)] for n in p.episodes if (p.season, n) in by_se]
        elif p.air_date and p.air_date in by_date:
            covered = [by_date[p.air_date]]
        elif series.get("series_type") == "anime":
            import re
            m = re.search(r"(?:^|[ ._-])(\d{2,4})(?:v\d)?(?:[ ._-]|$)", p.title)
            if m and int(m.group(1)) in by_abs:
                covered = [by_abs[int(m.group(1))]]
        if not covered:
            d.reject("Could not map release to any episode")
        elif not wanted_ids.intersection(e["id"] for e in covered):
            d.reject("Doesn't contain a wanted episode")
        else:
            d.episode_ids = [e["id"] for e in covered]
            for e in covered:
                if e["file_quality"]:
                    _upgrade_check(d, profile, e["file_quality"], e["file_score"])
                    if d.other_reasons():
                        break
            _size_ok(d, profile, series.get("runtime") or 0, len(covered))
        out.append(d)
    if dual_fallback:
        _allow_dual(out, {eid for d in out if d.accepted for eid in d.episode_ids})
    return sorted(out, key=lambda x: (not x.accepted, x.sort_key()))


def active_download_for(kind: str, item_id: int) -> dict | None:
    if kind == "movie":
        return db.one("SELECT * FROM downloads WHERE kind='movie' AND movie_id=? AND status IN ('queued','downloading','importing')", (item_id,))
    for dl in db.q("SELECT * FROM downloads WHERE kind='tv' AND status IN ('queued','downloading','importing')"):
        if item_id in json.loads(dl["episode_ids"] or "[]"):
            return dl
    return None
