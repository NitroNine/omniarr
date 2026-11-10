# Omniarr

Movies and TV in one self-hosted app, built for Unraid:

- **Metadata:** movies come from **The Movie Database (TMDB)** and TV shows from **TheTVDB (v4 API)**.
- **Quality, custom formats, scores, size limits and naming** are all imported from **[TRaSH Guides](https://trash-guides.info)**. Omniarr pulls TRaSH's JSON straight from GitHub and re-syncs it daily.
- **Two libraries per media type:** 1080p-and-lower and 4K, each with its own root folder and TRaSH profile. You can move an item between them at any time, and its files move too.
- **Search:** works with any **Newznab** Usenet indexer (NZBGeek, DrunkenSlug, NZBFinder, …). It does RSS sync, backlog search, season packs and upgrades up to the cutoff.
- **Collections and universes:** add a whole movie collection (e.g. *The Lord of the Rings Collection*) or a universe (e.g. the MCU, through its TMDB keyword) in one go. New movies announced later are added automatically.
- **Streaming tab:** browse Netflix, Apple TV+, Prime Video, Disney+, Max, Hulu, Paramount+, Peacock and more. Click a service's logo to see the movies and TV shows it offers, and add them with one click.
- **Library import:** brings in media that's already in your root folders. Omniarr matches each folder on TMDB/TheTVDB and works out the quality and TRaSH score of each file already there, so upgrades work straight away. Renaming is optional.
- **Quality limits per library:** choose which qualities are allowed (your maximum and minimum), the quality to *upgrade until*, and whether upgrades happen at all.
- **Downloads:** sent to **NZBGet**. When a download finishes, Omniarr renames it with the TRaSH naming scheme and moves it (or hardlinks/copies it) into the right root folder. Failed downloads are blocklisted and searched again.

## How the 1080p / 4K split works

| Library | Root folder (default) | TRaSH profile (default) |
|---|---|---|
| Movies, 1080p and lower | `/data/media/movies` | HD Bluray + WEB |
| Movies, 4K | `/data/media/movies-4k` | UHD Bluray + WEB |
| TV, 1080p and lower | `/data/media/tv` | WEB-1080p |
| TV, 4K | `/data/media/tv-4k` | WEB-2160p |

You choose the library when you add a title. The profile decides which releases are allowed: for example, the 1080p library never grabs a 2160p release. All four profiles can be changed to any TRaSH profile (Remux, SQP, Anime, French, German, …) under **Settings → Profiles**.

## Install on Unraid

### 1. Get the image

**Option A — GitHub (recommended).** Push this folder to a GitHub repository. The included workflow (`.github/workflows/docker.yml`) runs the tests, then publishes `ghcr.io/<you>/<repo>:latest` for amd64 and arm64. If the package is private, make it public in GitHub's package settings, or log Unraid in to GHCR.

**Option B — build on the server.** Copy the folder to e.g. `/mnt/user/appdata/omniarr-src`, open the Unraid terminal and run:

```bash
cd /mnt/user/appdata/omniarr-src && docker build -t omniarr:latest .
```

### 2. Add the container

Copy `unraid/omniarr.xml` to `/boot/config/plugins/dockerMan/templates-user/my-omniarr.xml`. Then go to **Docker → Add Container** and pick the `omniarr` template. If you used Option A, set **Repository** to your `ghcr.io/...` image.

Or use `docker-compose.yml` (Compose Manager plugin).

| Setting | Value |
|---|---|
| Port | `8790` |
| `/config` | `/mnt/user/appdata/omniarr` |
| `/data` | `/mnt/user/data` (one share holding both `usenet/` downloads and `media/`) |
| `PUID` / `PGID` | `99` / `100` |
| `OMNIARR_PASSWORD` | optional; turns on a login for the web UI |

**Paths:** follow TRaSH's folder layout. Give NZBGet the same `/data` mapping and set its `DestDir` to `/data/usenet/completed`. Then moves are instant, and the `hardlink` import mode works. If NZBGet reports a different path than Omniarr sees, set the remote path mapping under **Settings → NZBGet**.

### 3. First-run setup (web UI at `http://<unraid-ip>:8790`)

1. **Settings → Profiles → "Sync TRaSH Guides now"** (afterwards it re-syncs automatically).
2. **Settings → Metadata:** enter your TMDB API key (v3 key or v4 read token) and your TheTVDB v4 API key (plus a PIN if your key needs one).
3. **Settings → NZBGet:** URL, username and password, then **Test**. Create the `movies` and `tv` categories in NZBGet, or change the names here.
4. **Settings → Indexers:** add each Usenet indexer (URL + API key). It's tested as soon as you add it.
5. **Settings → Root folders:** check the four paths.
6. **Add:** search TMDB or TheTVDB, pick **1080p** or **4K**, and click Add.

## Useful tools

- **Settings → Profiles → "Test a release name"** shows how a release title is parsed, which TRaSH custom formats match, and its score. Handy for working out why something was or wasn't grabbed.
- **Interactive search** on any movie, season or episode lists every result with its score and rejection reasons. You can grab any of them by hand.
- **Activity** shows the queue, history, an event log and the blocklist, plus buttons to run RSS sync or a backlog search now.

## Streaming tab

The **Streaming** tab shows a logo for every streaming service available in your country. The big ones come first; **Show all** reveals the rest, and the search box finds a specific service. The data comes from TMDB's "where to watch" listings, which are supplied by JustWatch.

Click a service to browse what it offers:
- **Movies / TV shows** tabs, sorted by most popular, newest, top rated or A–Z, with **Load more** to page through everything.
- **Originals only** (TV): limits the list to the service's own shows, e.g. Apple TV+ or Netflix originals.
- **Add to:** pick the 1080p or 4K library and, for TV, which episodes to monitor. Then click **+ Add** on any poster. Titles already in your library are marked **In library**, and clicking them opens their page.

Set your country under **Settings → Metadata → Streaming region**: US, GB, CA, AU, DE, etc.

## Collections and universes

There are three ways to add a collection:
- **From a movie search:** when you search for a movie that's part of a TMDB collection, its result shows **"Part of … — add whole collection"**.
- **Search collections directly:** on **Add**, set the search type to **Movie collection / universe**. This finds TMDB collections and TMDB keywords. Universes like the MCU aren't TMDB collections, so search for their keyword instead, e.g. "marvel cinematic universe".
- **From a movie's page:** movies in the library show a **Part of …** link.

The collection window lists every movie with its release date:
- Tick the movies you want and choose the **1080p** or **4K** library.
- Optionally tick **Add future movies automatically** and **Search now**.
- Movies already in your library are marked as such.
- Upcoming movies are added straight away and searched for once they're released.

**Movies → Collections** shows each collection's progress (e.g. 2/3 downloaded). From there you can:
- change which library newly found movies go to,
- turn automatic adding on or off,
- check for new movies now (this also happens daily),
- stop tracking a collection. Its movies stay in your library.

Movies you untick are remembered and never added automatically later.

## Importing media you already have

Go to **Import**, choose Movies or TV shows, and click **Scan root folders**. Omniarr lists every folder in your 1080p and 4K roots that isn't in the library yet, plus loose movie files sitting directly in a root. Each one is matched:

1. **IDs in the folder name** win: `{tmdb-603}`, `[tmdbid-603]`, `{tvdb-81189}`, `[tvdbid-81189]`, `{imdb-tt0133093}`. These are marked "ID match".
2. Otherwise Omniarr searches by **title + year** read from the folder name (e.g. `The Matrix (1999)` or `The.Matrix.1999.1080p.BluRay-GRP`). Matches are marked "exact" or "guess". Use **Change** to fix any guess before importing.

Then choose the import options:
- **Rename files to TRaSH naming:** off by default, so your files stay exactly where they are.
- **Monitor** (TV shows): *All episodes*, *Only episodes I have*, *Future episodes*, or *None*.
- **Search for missing & upgrades now.**

Folder names are always kept. A loose movie file gets its own TRaSH-named folder.

Quality is read from the file name, falling back to the folder name, and then to ffprobe for the resolution. Every file is scored with the library's TRaSH custom formats.

Each movie and show page also has **Rescan folder** (optionally renaming the files) for changes you've made on disk.

## Quality limits and upgrades

**Settings → Quality limits** fine-tunes each of the four libraries on top of its TRaSH profile:

- **Allowed qualities:** listed best first. Untick anything you never want; this sets your maximum and minimum.
- **Upgrade until:** the cutoff quality.
- **Upgrades:** turn them off to keep the first file Omniarr grabs.
- **Custom-format score cutoff:** once the cutoff quality is reached, Omniarr keeps upgrading to higher-scoring TRaSH releases until this score. TRaSH's default is 10000, which means "keep improving". Set it to **0** to stop as soon as the quality is reached.

Example: allow *Bluray-1080p* and *WEB 1080p*, set **Upgrade until** to *Bluray-1080p*, and set the score cutoff to 0.
1. If only a WEB-DL exists, Omniarr grabs it right away.
2. RSS sync and the backlog search keep looking.
3. When a 1080p Bluray appears, it replaces the WEB-DL.
4. After that, nothing more is downloaded for that title.

**Reset to TRaSH defaults** removes your changes.

## How releases are chosen

1. Parse the release title: resolution, source, remux/disc, season/episode, year and group.
2. Reject it if the title or year doesn't match, the quality isn't allowed in the profile, it's blocklisted, the custom-format score is below the profile minimum, or its size is outside TRaSH's MB-per-minute limits.
3. Score the remaining releases with the TRaSH custom formats in the profile. That includes the default CF groups for that profile (e.g. *Unwanted Formats*: BR-DISK, LQ, x265 HD, …). Matching follows Radarr/Sonarr's rules, uses TRaSH's .NET-style regexes, and uses the score set you picked.
4. Rank by quality position in the profile, then score, then revision, then size.
5. If a file already exists, only accept a real upgrade, and stop once both the cutoff quality and the score cutoff are met. Your **Quality limits** settings override TRaSH's values here.

## Development

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt pytest
.venv/bin/pytest -q
OMNIARR_CONFIG=./config .venv/bin/uvicorn app.main:app --reload --port 8790
```

Any setting can also be supplied as an environment variable named `OMNIARR_<SETTING_NAME>`, e.g. `OMNIARR_TMDB_API_KEY`. Set `GITHUB_TOKEN` if you hit GitHub API rate limits while syncing TRaSH.

### Layout

```
app/
  main.py          REST API, scheduler, auth
  parser.py        release-title parser
  trash/           TRaSH sync, custom-format engine, profile builder
  naming.py        Radarr/Sonarr naming-token renderer
  decision.py      accept/reject + ranking + upgrade logic
  search.py        indexer search, RSS sync, grabbing
  importer.py      NZBGet tracking, import, rename, blocklist
  library.py       add/refresh movies & series, root folders, 1080p/4K moves
  services/metadata.py also covers streaming providers (TMDB watch providers / discover)
  collections_.py  TMDB collections / keyword universes, auto-adding new entries
  scanner.py       library import: scan root folders, match, detect existing files
  services/        TMDB, TheTVDB, Newznab, NZBGet, ffprobe
  static/          web UI (no build step)
tests/             unit + end-to-end tests (fake indexer / NZBGet)
unraid/omniarr.xml Unraid template
```

## Known limitations

- Usenet + NZBGet only (no torrents, no SABnzbd yet).
- Library import doesn't watch the disk. Use **Rescan folder** (or import again) after changing files by hand.
- TRaSH `IndexerFlagSpecification` custom formats (freeleech, etc.) never match, since they're torrent-oriented. Language CFs are inferred from release titles.
