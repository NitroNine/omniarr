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
  A title version can pick its own TRaSH profile (`movies.profile` / `series.profile`, title page Quality menu,
  `decision.profile_for(..., item=row)`); then only the library's upgrades on/off and score cutoff apply.
- **Newznab indexers** + **NZBGet** (JSON-RPC); import with TRaSH naming, blocklist + re-search.
  **Built-in downloader** (default, `download_client=builtin`): NZBGet's official Linux release is baked
  into the image at `/opt/nzbget` (pinned version + sha256 in the Dockerfile). Weekly workflow `nzbget-update.yml` (Mon 09:00 UTC + manual) bumps the pin to the latest stable nzbgetcom release, commits as github-actions[bot] and starts `docker.yml`; Unraid's nightly container auto-update installs it. Weekly `image-refresh.yml` (Mon 10:00 UTC + manual) builds fresh with no cache and compares Python, all Debian packages (MediaInfo, tini, security fixes), pip packages and NZBGet with the published :latest; any difference starts `docker.yml`. Stays on Python 3.12 and inside requirements.txt's ranges. `services/downloader.py`
  renders `/config/nzbget/nzbget.conf` from its template + Omniarr settings (table `usenet_servers`,
  `download_dir` empty = template's `/downloads` mount if present, else `/data/usenet`, speed limit), runs `nzbget -s` on 127.0.0.1:16789 with a
  generated password, restarts it if it dies, `reload`s on settings changes. Grabs are refused (`search.NoSpace`, HTTP 507) unless NZBGet's
  free space covers 2x (queue + release) + `download_min_free_gb`; Import unpacks a nested RAR set (release folder inside the outer archive) with the bundled unrar (`importer.unpack_nested`); disk-full failures (FAILURE/MOVE, WARNING/SPACE, or
  near-zero free) are not blocklisted (`importer.space_failure`). Indexer HTTP goes through `newznab._request`:
  one request at a time per host, 1 s apart; a 429 pauses that host (`IndexerBusy`, Retry-After, "after midnight" = hourly retries, or 60 s doubling to
  15 min) and slows it further. Indexer `priority`: searches ask the lowest number first, backups only while nothing acceptable turned up (`newznab._fan_out(enough=…)`); RSS asks all; a grab skips releases on paused indexers. `search.HOLD` = NoSpace/IndexerBusy: stop grabbing, keep items wanted. API keys are
  redacted in errors and `db.log`. Old installs with an
  NZBGet password migrate to `external`.
- **Hallmark tab** (`app/hallmark.py`, `#/hallmark`): Christmas movies from this year on: Hallmark Channel's Countdown to Christmas schedule from Wikipedia (`services/wikipedia.py`, MediaWiki API, "List of Hallmark Channel Original Movies (2020s)", matched to TMDB by title + year by `metadata.hallmark_schedule`; unmatched ones show as "Not on TMDB yet"), TMDB's Hallmark providers (`metadata.discover_christmas`) plus TMDB's Hallmark production companies (`metadata.hallmark_productions`) and Christmas-tab movies whose US release note names a Hallmark channel (`metadata.hallmark_premieres`); these catch upcoming premieres that aren't on Hallmark+ yet; the tab shows Coming up / Aired; with `hallmark_auto` on, job `hallmark` adds new ones every 6 h in `hallmark_tier` and they download once aired; added ids go in `hallmark_seen` so removed ones stay removed.
- **Christmas** (`movies.christmas`, setting `christmas_folder`: a full path = its own root (folder = title folder only), a name = subfolder of root_movies; changing it runs `library.relocate_christmas`): added from the Christmas/Hallmark tabs or the movie page switch (`library.set_christmas` moves the folder); NZBGet category `nzbget_category_christmas`; scanner looks one level into it. Christmas tab = `#/christmas` → `renderProvider(0)` → `metadata.discover_christmas_all` (keywords + US TV premieres, with services), Hide Hallmark.
- **English only** (setting `english_only`, default on): `parser.not_english` rejects releases whose tags after the title name another language or MULTI/Multi-Audio/Dual Audio/DL/dubbed. Exception: a dual/multi-audio release (`parser.dual_audio`, not dubbed) is accepted in movie searches (not TV) when no English-only release is acceptable; not from RSS or for the backup-indexer check (`dual_fallback=False`).
- **Manual .nzb upload** (Activity → Upload NZB, `app/manual_nzb.py`, `POST /api/nzb`): Movie/TV choice, title + version
  guessed from the release name (or picked), episodes mapped from SxxEyy; goes through `search.grab(nzb=…)` and normal import.
- **Backup / restore** (`app/backup.py`, Settings → Backup): .zip = SQLite online-backup snapshot of omniarr.db +
  NZBGet queue + manifest; daily auto backup to /config/backups (keep 7); restore stops NZBGet, saves
  `before-restore-*.zip`, swaps the DB (migrations run), the scheduler pauses meanwhile (`backup.restoring`). Media not included.
- **Title pages** `#/title/{movie|tv}/{tmdb_id}` (`/api/info/…`, `metadata.movie_info/tv_info`, 6 h cache):
  cast, crew, release dates (cinema/digital/disc per `streaming_region`, else US), trailer, streaming.
  The backlog search waits for `library.available_date` (first digital/disc/TV-premiere date, else cinema + 90 d);
  adding a title searches right away whatever the dates (owner's wish), and RSS watches every monitored movie;
  job `movie_refresh` re-reads dates daily for monitored movies without a file.
- **Add page → New releases** (`/api/new/{movie|tv}`, `metadata.new_releases`, 6 h cache), most popular first, English only while `english_only`, + Add per card. Movies menu: Streaming (digital release in the last 120 d and now on a subscription service, optional service filter), In cinemas (last 60 d), 4K disc (physical releases of the last 90 d whose TMDB release note says 4K/UHD, `metadata.disc4k_date`, plus 4K discs from Blu-ray.com's New Releases RSS feed, read every 6 h by job `bluray` (`services/bluray.py`, table `bluray_releases`, matched to TMDB by title + year; Blu-ray.com's date wins). Blu-ray.com forbids crawling its pages: only ever read the RSS feed. One page, newest first). Streaming and In cinemas: films first released in the past year only. TV tab: shows first aired in the last 60 d (no news/soap/talk), optional service filter (`provider`).
- **Audiobooks** (2026-11-24, Brendan's request: "add Spokarr", option 1 = built in, not Spokarr's container;
  `app/audiobooks.py`, `services/audible.py`, `#/audiobooks`, nav "Audiobooks"): Audible public catalog (setting
  `audible_region`, default us; no account), table `audiobooks` (one row per ASIN, `folder` relative to the root =
  in library), `downloads.kind='audiobook'` + `audiobook_id`, indexer column `audio_categories` (default 3030),
  NZBGet category `nzbget_category_audiobooks`. Release scoring ported from Spokarr (MIT, credited in README):
  title words ≥75 %, narrator +25, author +5, language ±, drama −30, abridged −25, MB/min <0.15 −30; ok = ≥50 and
  right language. Grab best ok release (`search_book`), import moves audio + pictures (CD folders kept) to
  `Author/Series/Book NN - Title` or `Author/Title` (Brendan's folder style), cover from Audible if none. Root
  `root_audiobooks`, empty = first existing of /data/media/audiobooks … /data/Audiobooks (Unraid: /data/Audiobooks =
  /mnt/user/media/Audiobooks, which Theremin Web serves since Audiobookshelf was removed 2026-11-24; Theremin
  scans it every 5 min and joins MP3 books into .m4b). Books already in the folder are matched by ASIN in the
  folder name or the title (`library_index`, `_folder_key`). Backlog search includes wanted books (5 per run).
  Requests: kind `audiobook`, `source` = ASIN, target_id 0. Genre menu (2026-11-24, Brendan): 10 genres (`audible.GENRES`), each store's category id looked up by name (`categories_num_levels=2`, cached 24 h); `discover(genre)` = that category's best sellers; Audible CA has no adult Action & Adventure → `FALLBACK` Literature & Fiction + keywords "action adventure". Choice remembered per browser (localStorage `omniarr-ab-genre`). Tests `tests/test_audiobooks.py`.
- **Music** (2026-11-27, Brendan's request: "add Deemix", built in + artists & new releases; `app/music.py`,
  `services/deezer.py` (public API, no key; `check_arl` for Settings → Music), `services/deemix.py`, `#/music`, admin-only):
  the image builds bambanah/deemix's CLI (pinned `DEEMIX_REF`, one 2 MB bundle at /opt/deemix/deemix.cjs) + Node.js;
  Omniarr runs `node deemix.cjs --path <root>/<Letter> --bitrate flac <album url>` with DEEMIX_DATA_DIR=/config/deemix
  (config.json + login.json written each run from settings `deezer_arl` (secret), `music_quality`). Tables
  `music_artists` (monitored = grab new releases), `music_albums` (wanted, folder, last_try); `downloads.music_album_id`
  rows show in Activity (importer skips them). One album at a time (job `music`, 30 s); complete = audio files ≥ Deezer
  track count, else retry after 6 h. Job `music_refresh`: each artist every 12 h, new releases of `music_types`
  (default album,ep), one edition per base title. Owned albums = `find_album` over `root_music` (empty = `/music` mount
  if present; Tower: /music = /mnt/nvme2tb/Navidrome, Brendan's FLAC library served by Theremin, 2026-11-27) + sibling format folders (Compressed, opus files), letter folders.
  Brendan's old Deemix container (/mnt/user/appdata/Deemix) isn't used by Omniarr.
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
.venv/bin/pytest -q                       # 167 tests
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
- Searches use Usenet + NZBGet only. Torrents only by hand (2026-11-24, Brendan): built-in qBittorrent (`qbittorrent-nox` from Debian, `services/qbittorrent.py`, Web API on 127.0.0.1:16790, profile `/config/qbittorrent`, saves to `/data/torrents`, port 6882), linked to a title by a `downloads` row with `torrent_hash` (`torrents.py`); finished ones import by hardlink (`importer.TRANSFER_MODE`) and keep seeding.
- After each movie/episode import the media server scans that title's folder (`services/mediaserver.py`, Settings → Media server; 2026-11-24, Brendan's request: Silo only scans nightly at 02:00). Silo: `POST /api/v2/scan {library_id, path}` with `Authorization: Bearer sa_…`; Jellyfin/Emby: `/Library/Media/Updated`.
