# Omniarr — notes for Claude

Self-hosted app combining Sonarr + Radarr for the owner's Unraid server. Python 3.12,
FastAPI, SQLite, vanilla-JS UI (no build step). Started in a Claude Code web session and
moved to local; this file carries the context over.

## What it does
- Movies + TV from **TMDB** only (TheTVDB support was removed 2026-11-10; shows still store
  `tvdb_id` from TMDB's external ids for indexer searches and `{tvdb-…}` folder names).
- **TRaSH Guides** synced from GitHub (`app/trash/`): quality profiles, custom formats
  (.NET regexes via the `regex` module), scores, size limits, naming formats.
- **Versions:** each `movies`/`series` row is one version ("tier" hd/uhd); a title can have both
  rows (UNIQUE(tmdb_id, tier)), sharing a folder when the hd/uhd root folders are the same
  (`library.versions/sibling/shares_folder`). Adding takes tier hd | uhd | both.
- **1080p vs 4K libraries** ("tier" hd/uhd) for movies and TV, each with its own root folder
  and TRaSH profile; per-library **quality limits** override (allowed qualities, upgrade-until
  cutoff, upgrades on/off, CF score cutoff) in setting `quality_overrides`.
- **Newznab indexers** + **NZBGet** (JSON-RPC); import with TRaSH naming, blocklist + re-search.
- **Library import** of existing folders (`app/scanner.py`), **collections/universes**
  (TMDB collections and keywords, `app/collections_.py`), **Streaming tab** (TMDB watch providers),
  **user accounts + requests** with admin approval / per-user auto-approve and 4K permission
  (`app/auth.py`, `app/requests_.py`), light/dark theme.

## Layout
`app/main.py` API + scheduler + auth middleware (non-admin allow-list in `auth.USER_ROUTES`),
`parser.py` release names, `decision.py` accept/rank/upgrade, `search.py`, `importer.py`,
`library.py`, `naming.py`, `services/` (metadata, newznab, nzbget, mediainfo),
`static/` UI. DB schema + migrations in `app/db.py` (`_migrate`).

## Working on it
```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt pytest
.venv/bin/pytest -q                       # 32 tests, all passing at hand-over
OMNIARR_CONFIG=./config .venv/bin/uvicorn app.main:app --reload --port 8790
```
Tests fake TMDB/indexers/NZBGet; none of the external services had been exercised
for real yet, and the Docker image has never been built (the web sandbox couldn't).

## Where things were left (open items)
1. **GitHub:** public repo `NitroNine/omniarr`; pushes to `main` test and publish
   `ghcr.io/nitronine/omniarr:latest` (amd64 + arm64) via `.github/workflows/docker.yml`.
2. Commits are authored as NitroNine (GitHub noreply email); history was rewritten once
   before the first push.
3. On Unraid: add the repo under Docker → Template Repositories, Add Container → omniarr.
   If it was installed earlier with `install-unraid.sh`, switch by editing the container's
   Repository to the GHCR image (data in /mnt/user/appdata/omniarr is kept).
4. First real run will need watching: TMDB TV endpoints, NZBGet, indexers, Docker build.

## Owner preferences learned so far
- Wants things to install/update "normally" on Unraid (template + registry image).
- Doesn't want paid dependencies (hence TMDB for TV).
- Uses Usenet + NZBGet only.
