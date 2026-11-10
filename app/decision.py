"""Decides which releases are acceptable for a movie / episode set and ranks them,
following the same rules Radarr/Sonarr use with TRaSH quality profiles."""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from . import db, trash
from .parser import ParsedRelease, normalize_title, parse
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


def profile_for(kind: str, tier: str) -> trash.Profile:
    s = db.get_settings()
    key = s[f"profile_{'movies' if kind == 'movie' else 'tv'}_{tier}"]
    return trash.ensure_loaded().build_profile(kind, key, s.get("trash_score_set") or "default")


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


def _upgrade_check(d: Decision, profile: trash.Profile, quality: str | None, score: int | None) -> None:
    """Existing file present: only accept real upgrades, until cutoff is reached."""
    if not quality:
        return
    if not (profile.upgrade_allowed and db.setting("upgrades_enabled")):
        d.reject("Already have a file (upgrades disabled)")
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
    return d


def _title_matches(parsed_name: str, *titles: str | None) -> bool:
    n = normalize_title(parsed_name)
    if not n:
        return False
    for t in titles:
        if t and normalize_title(t) == n:
            return True
    return False


# ----------------------------------------------------------------- movies --
def decide_movie(movie: dict, releases: list[Release]) -> list[Decision]:
    profile = profile_for("movie", movie["tier"])
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
    return sorted(out, key=lambda x: (not x.accepted, x.sort_key()))


# --------------------------------------------------------------- episodes --
def decide_episodes(series: dict, episodes: list[dict], releases: list[Release]) -> list[Decision]:
    """`episodes` = the episodes we're looking for (wanted). Releases are mapped
    onto them; a release covering any non-wanted / already-good episode is
    only accepted if every covered episode benefits."""
    profile = profile_for("tv", series["tier"])
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
                    if not d.accepted:
                        break
            _size_ok(d, profile, series.get("runtime") or 0, len(covered))
        out.append(d)
    return sorted(out, key=lambda x: (not x.accepted, x.sort_key()))


def active_download_for(kind: str, item_id: int) -> dict | None:
    if kind == "movie":
        return db.one("SELECT * FROM downloads WHERE kind='movie' AND movie_id=? AND status IN ('queued','downloading','importing')", (item_id,))
    for dl in db.q("SELECT * FROM downloads WHERE kind='tv' AND status IN ('queued','downloading','importing')"):
        if item_id in json.loads(dl["episode_ids"] or "[]"):
            return dl
    return None
