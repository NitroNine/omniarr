"""TRaSH Guides integration: sync JSON from GitHub, evaluate custom formats,
build quality profiles, size limits and naming formats."""
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field

import httpx
import regex

from .. import db
from ..parser import ParsedRelease

REPO = "TRaSH-Guides/Guides"
SUBDIRS = ("cf", "cf-groups", "quality-profiles", "quality-size", "naming")
APPS = ("radarr", "sonarr")


def trash_dir() -> str:
    return os.path.join(db.CONFIG_DIR, "trash")


def _app(kind: str) -> str:
    return "radarr" if kind == "movie" else "sonarr"


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------
async def sync(branch: str | None = None) -> dict:
    """Download docs/json/{radarr,sonarr}/... from the TRaSH Guides repo.

    Uses one GitHub tree API call to list files, then fetches each JSON
    from raw.githubusercontent.com (no API rate-limit there).
    """
    branch = branch or db.setting("trash_branch") or "master"
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "omniarr"}
    if os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    async with httpx.AsyncClient(timeout=60, follow_redirects=True, headers=headers) as client:
        r = await client.get(f"https://api.github.com/repos/{REPO}/git/trees/{branch}?recursive=1")
        r.raise_for_status()
        tree = r.json()["tree"]
        wanted = [t["path"] for t in tree if t["type"] == "blob" and t["path"].endswith(".json")
                  and any(t["path"].startswith(f"docs/json/{a}/{s}/") for a in APPS for s in SUBDIRS)]
        sem = asyncio.Semaphore(16)
        out = trash_dir() + ".new"
        if os.path.isdir(out):
            import shutil
            shutil.rmtree(out)

        async def fetch(path: str):
            async with sem:
                for attempt in range(3):
                    try:
                        rr = await client.get(f"https://raw.githubusercontent.com/{REPO}/{branch}/{path}")
                        rr.raise_for_status()
                        break
                    except httpx.HTTPError:
                        if attempt == 2:
                            raise
                        await asyncio.sleep(1 + attempt)
                dest = os.path.join(out, path.removeprefix("docs/json/"))
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with open(dest, "wb") as f:
                    f.write(rr.content)

        await asyncio.gather(*(fetch(p) for p in wanted))

    import shutil
    if os.path.isdir(trash_dir()):
        shutil.rmtree(trash_dir())
    os.replace(out, trash_dir())
    STORE.reload()
    db.ex("INSERT INTO settings(key,value) VALUES('_trash_synced', datetime('now')) "
          "ON CONFLICT(key) DO UPDATE SET value=excluded.value")
    db.log(f"TRaSH Guides synced ({len(wanted)} files)")
    return {"files": len(wanted)}


# ---------------------------------------------------------------------------
# Data store
# ---------------------------------------------------------------------------
@dataclass
class CustomFormat:
    trash_id: str
    name: str
    specs: list[dict]
    scores: dict[str, int]
    include_when_renaming: bool = False
    _compiled: dict = field(default_factory=dict, repr=False)

    def score(self, score_set: str) -> int:
        return int(self.scores.get(score_set, self.scores.get("default", 0)))


@dataclass
class Profile:
    key: str
    name: str
    kind: str
    items: list[dict]  # ordered best -> worst
    cutoff: str
    upgrade_allowed: bool
    min_format_score: int
    cutoff_format_score: int
    min_upgrade_format_score: int
    format_scores: dict[str, int]  # trash_id -> score
    quality_size: dict[str, dict]  # quality -> {min, preferred, max} (MB / minute)
    score_set: str = "default"

    def quality_rank(self, quality: str) -> int | None:
        """Lower is better. None = not allowed."""
        for i, item in enumerate(self.items):
            if item["name"] == quality or quality in item.get("items", []):
                return i if item.get("allowed") else None
        return None

    def cutoff_rank(self) -> int:
        r = next((i for i, it in enumerate(self.items) if it["name"] == self.cutoff or self.cutoff in it.get("items", [])), 0)
        return r


class Store:
    def __init__(self):
        self.cfs: dict[str, dict[str, CustomFormat]] = {"radarr": {}, "sonarr": {}}
        self.groups: dict[str, list[dict]] = {"radarr": [], "sonarr": []}
        self.profiles: dict[str, dict[str, dict]] = {"radarr": {}, "sonarr": {}}
        self.sizes: dict[str, dict[str, dict]] = {"radarr": {}, "sonarr": {}}
        self.naming: dict[str, dict] = {"radarr": {}, "sonarr": {}}
        self.loaded = False

    def reload(self) -> None:
        base = trash_dir()
        for app in APPS:
            self.cfs[app], self.groups[app], self.profiles[app], self.sizes[app] = {}, [], {}, {}
            for sub, fn in (("cf", self._load_cf), ("cf-groups", self._load_group),
                            ("quality-profiles", self._load_profile), ("quality-size", self._load_size)):
                d = os.path.join(base, app, sub)
                if not os.path.isdir(d):
                    continue
                for f in sorted(os.listdir(d)):
                    if f.endswith(".json"):
                        try:
                            with open(os.path.join(d, f), encoding="utf-8") as fh:
                                fn(app, f[:-5], json.load(fh))
                        except (json.JSONDecodeError, KeyError) as e:
                            db.log(f"TRaSH: skipping {app}/{sub}/{f}: {e}", "warn")
            nd = os.path.join(base, app, "naming")
            if os.path.isdir(nd):
                for f in os.listdir(nd):
                    with open(os.path.join(nd, f), encoding="utf-8") as fh:
                        self.naming[app] = json.load(fh)
        self._add_remux_only()
        self.loaded = any(self.cfs[a] for a in APPS)

    # Omniarr's own profiles: TRaSH's "Remux + WEB" with only the Remux allowed (same scores, sizes, cutoff)
    REMUX_ONLY = {"remux-only-2160p": ("remux-web-2160p", "Remux-2160p", "Remux only 2160p"),
                  "remux-only-1080p": ("remux-web-1080p", "Remux-1080p", "Remux only 1080p")}

    def _add_remux_only(self) -> None:
        import copy
        for key, (base, quality, name) in self.REMUX_ONLY.items():
            src = self.profiles["radarr"].get(base)
            if not src or not any(it["name"] == quality for it in src.get("items", [])):
                continue
            d = copy.deepcopy(src)
            for it in d["items"]:
                it["allowed"] = it["name"] == quality
            d.update(_key=key, name=name, cutoff=quality,
                     trash_description=f"Only {quality} (Omniarr's own: TRaSH's {src['name']} without the WEB releases).")
            self.profiles["radarr"][key] = d

    def _load_cf(self, app, _stem, d):
        self.cfs[app][d["trash_id"]] = CustomFormat(
            d["trash_id"], d["name"], d.get("specifications", []), d.get("trash_scores", {}),
            bool(d.get("includeCustomFormatWhenRenaming")))

    def _load_group(self, app, _stem, d):
        self.groups[app].append(d)

    def _load_profile(self, app, stem, d):
        d["_key"] = stem
        self.profiles[app][stem] = d

    def _load_size(self, app, _stem, d):
        self.sizes[app][d["trash_id"]] = d

    # -- profile assembly --------------------------------------------------
    def list_profiles(self, kind: str) -> list[dict]:
        return [{"key": k, "name": v["name"], "description": v.get("trash_description", "")}
                for k, v in sorted(self.profiles[_app(kind)].items())]

    def build_profile(self, kind: str, key: str, score_set: str = "default") -> Profile:
        app = _app(kind)
        p = self.profiles[app].get(key)
        if not p:
            raise KeyError(f"TRaSH profile '{key}' not found for {app}. Sync TRaSH Guides first.")
        trash_ids: set[str] = set(p.get("formatItems", {}).values())
        # CF groups marked default that include this profile (unwanted formats, HQ groups, ...)
        for g in self.groups[app]:
            inc = g.get("quality_profiles", {}).get("include", {})
            if p["trash_id"] in inc.values() and str(g.get("default", "false")).lower() == "true":
                for cf in g.get("custom_formats", []):
                    if cf.get("required") or cf.get("default"):
                        trash_ids.add(cf["trash_id"])
        ss = p.get("trash_score_set") or score_set
        scores = {}
        for tid in trash_ids:
            cf = self.cfs[app].get(tid)
            if cf:
                scores[tid] = cf.score(ss)
        size_id = None
        qd = p.get("trash_quality_definition")
        if isinstance(qd, dict) and qd:
            size_id = next(iter(qd.values()))
        sizes = self.sizes[app].get(size_id) if size_id else None
        if sizes is None:
            want = "movie" if kind == "movie" else "series"
            sizes = next((s for s in self.sizes[app].values() if s.get("type") == want), {"qualities": []})
        return Profile(
            key=key, name=p["name"], kind=kind, items=p["items"], cutoff=p.get("cutoff", ""),
            upgrade_allowed=bool(p.get("upgradeAllowed", True)),
            min_format_score=int(p.get("minFormatScore", 0)),
            cutoff_format_score=int(p.get("cutoffFormatScore", 10000)),
            min_upgrade_format_score=int(p.get("minUpgradeFormatScore", 1)),
            format_scores=scores,
            quality_size={q["quality"]: q for q in sizes.get("qualities", [])},
            score_set=ss,
        )

    def naming_format(self, kind: str, section: str, key: str) -> str | None:
        n = self.naming[_app(kind)]
        if kind == "tv" and section in ("standard", "daily", "anime"):
            sec = n.get("episodes", {}).get(section, {})
        else:
            sec = n.get(section, {})
        return sec.get(key) or sec.get("default") or sec.get("standard")


STORE = Store()


def ensure_loaded() -> Store:
    if not STORE.loaded:
        STORE.reload()
    return STORE


# ---------------------------------------------------------------------------
# Custom format evaluation (mirrors Radarr/Sonarr matching rules)
# ---------------------------------------------------------------------------
def _spec_matches(spec: dict, rel: ParsedRelease, kind: str, cache: dict, indexer_flags: int = 0) -> bool:
    impl = spec["implementation"]
    val = spec.get("fields", {}).get("value")
    if impl in ("ReleaseTitleSpecification", "ReleaseGroupSpecification"):
        key = val  # cache by pattern text (object ids get reused after a TRaSH re-sync)
        rx = cache.get(key)
        if rx is None:
            try:
                rx = regex.compile(val, regex.IGNORECASE | regex.V0)
            except regex.error:
                rx = False
            cache[key] = rx
        if rx is False:
            return False
        target = rel.title if impl == "ReleaseTitleSpecification" else (rel.group or "")
        try:
            return bool(rx.search(target, timeout=0.2))
        except TimeoutError:
            return False
    if impl == "ResolutionSpecification":
        return rel.resolution == int(val)
    if impl == "SourceSpecification":
        return rel.source_id(kind) == int(val)
    if impl == "QualityModifierSpecification":
        return rel.modifier_id() == int(val)
    if impl == "ReleaseTypeSpecification":
        return rel.release_type() == int(val)
    if impl == "LanguageSpecification":
        v = int(val)
        langs = rel.languages or [-2]  # untagged release => assume original language
        if spec.get("fields", {}).get("exceptLanguage"):
            return v not in langs
        if v == -2:  # "Original"
            return langs == [-2] or 1 in langs
        return v in langs
    if impl == "IndexerFlagSpecification":
        return bool(indexer_flags & int(val))
    if impl == "SizeSpecification":
        return False
    return False


_regex_cache: dict = {}


def cf_matches(cf: CustomFormat, rel: ParsedRelease, kind: str, indexer_flags: int = 0) -> bool:
    """Radarr semantics: every *required* spec must match; for each
    implementation type that has non-required specs, at least one must match.
    `negate` inverts an individual spec's result."""
    if not cf.specs:
        return False
    by_impl: dict[str, list[bool]] = {}
    for spec in cf.specs:
        hit = _spec_matches(spec, rel, kind, _regex_cache, indexer_flags)
        if spec.get("negate"):
            hit = not hit
        if spec.get("required"):
            if not hit:
                return False
        else:
            by_impl.setdefault(spec["implementation"], []).append(hit)
    return all(any(v) for v in by_impl.values())


def score_release(profile: Profile, rel: ParsedRelease, indexer_flags: int = 0) -> tuple[int, list[str]]:
    store = ensure_loaded()
    app = _app(profile.kind)
    total, names = 0, []
    for tid, sc in profile.format_scores.items():
        cf = store.cfs[app].get(tid)
        if cf and cf_matches(cf, rel, profile.kind, indexer_flags):
            total += sc
            names.append(cf.name)
    return total, names


def all_matching_cfs(kind: str, rel: ParsedRelease) -> list[CustomFormat]:
    store = ensure_loaded()
    return [cf for cf in store.cfs[_app(kind)].values() if cf_matches(cf, rel, kind)]
