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
  rows (UNIQUE(tmdb_id, tier)), always sharing the title folder: one root per type, settings
  `root_movies` / `root_tv` (`library.versions/sibling/shares_folder`). Adding takes tier hd | uhd | both.
- **1080p vs 4K libraries** ("tier" hd/uhd) for movies and TV, each with its own root folder
  and TRaSH profile; per-library **quality limits** override (allowed qualities, upgrade-until
  cutoff, upgrades on/off, CF score cutoff) in setting `quality_overrides`.
- **Newznab indexers** + **NZBGet** (JSON-RPC); import with TRaSH naming, blocklist + re-search.
  **Built-in downloader** (default, `download_client=builtin`): NZBGet's official Linux release is baked
  into the image at `/opt/nzbget` (pinned version + sha256 in the Dockerfile). `services/downloader.py`
  renders `/config/nzbget/nzbget.conf` from its template + Omniarr settings (table `usenet_servers`,
  `download_dir` empty = template's `/downloads` mount if present, else `/data/usenet`, speed limit), runs `nzbget -s` on 127.0.0.1:16789 with a
  generated password, restarts it if it dies, `reload`s on settings changes. Grabs are refused (`search.NoSpace`, HTTP 507) unless NZBGet's
  free space covers 2x (queue + release) + `download_min_free_gb`; Import unpacks a nested RAR set (release folder inside the outer archive) with the bundled unrar (`importer.unpack_nested`); disk-full failures (FAILURE/MOVE, WARNING/SPACE, or
  near-zero free) are not blocklisted (`importer.space_failure`). Indexer HTTP goes through `newznab._request`:
  one request at a time per host, 1 s apart; a 429 pauses that host (`IndexerBusy`, Retry-After, "after midnight" = hourly retries, or 60 s doubling to
  15 min) and slows it further. Indexer `priority`: searches ask the lowest number first, backups only while nothing acceptable turned up (`newznab._fan_out(enough=…)`); RSS asks all; a grab skips releases on paused indexers. `search.HOLD` = NoSpace/IndexerBusy: stop grabbing, keep items wanted. API keys are
  redacted in errors and `db.log`. Old installs with an
  NZBGet password migrate to `external`.
- **Hallmark tab** (`app/hallmark.py`, `#/hallmark`): Christmas movies from this year on across TMDB's Hallmark providers (`metadata.discover_christmas`); with `hallmark_auto` on, job `hallmark` adds new ones every 6 h in `hallmark_tier` and they download once aired; added ids go in `hallmark_seen` so removed ones stay removed.
- **Christmas** (`movies.christmas`, setting `christmas_folder`: a full path = its own root (folder = title folder only), a name = subfolder of root_movies; changing it runs `library.relocate_christmas`): added from the Christmas/Hallmark tabs or the movie page switch (`library.set_christmas` moves the folder); NZBGet category `nzbget_category_christmas`; scanner looks one level into it. Christmas tab = `#/christmas` → `renderProvider(0)` → `metadata.discover_christmas_all` (keywords + US TV premieres, with services), Hide Hallmark.
- **English only** (setting `english_only`, default on): `parser.not_english` rejects releases whose tags after the title name another language or MULTI/Multi-Audio/Dual Audio/DL/dubbed.
- **Manual .nzb upload** (Activity → Upload NZB, `app/manual_nzb.py`, `POST /api/nzb`): Movie/TV choice, title + version
  guessed from the release name (or picked), episodes mapped from SxxEyy; goes through `search.grab(nzb=…)` and normal import.
- **Backup / restore** (`app/backup.py`, Settings → Backup): .zip = SQLite online-backup snapshot of omniarr.db +
  NZBGet queue + manifest; daily auto backup to /config/backups (keep 7); restore stops NZBGet, saves
  `before-restore-*.zip`, swaps the DB (migrations run), the scheduler pauses meanwhile (`backup.restoring`). Media not included.
- **Title pages** `#/title/{movie|tv}/{tmdb_id}` (`/api/info/…`, `metadata.movie_info/tv_info`, 6 h cache):
  cast, crew, release dates (cinema/digital/disc per `streaming_region`, else US), trailer, streaming.
  Movies are only searched once `library.available_date` (first digital/disc/TV-premiere date, else cinema + 90 d) passes;
  job `movie_refresh` re-reads dates daily for monitored movies without a file.
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
.venv/bin/pytest -q                       # 107 tests
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
