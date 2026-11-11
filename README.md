# Omniarr

Movies and TV in one self-hosted app, built for Unraid:

- **Metadata:** movies and TV come from **The Movie Database (TMDB)**, which needs one free API key (see [Metadata](#metadata-tmdb)).
- **Quality, custom formats, scores, size limits and naming** are all imported from **[TRaSH Guides](https://trash-guides.info)**. Omniarr pulls TRaSH's JSON straight from GitHub and re-syncs it daily.
- **1080p and 4K versions, side by side:** each movie or show can have a 1080p version, a 4K version, or both. Both live in the title's one folder, and each has its own TRaSH profile, searches and upgrades.
- **Search:** works with any **Newznab** Usenet indexer (NZBGeek, DrunkenSlug, NZBFinder, …). It does RSS sync, backlog search, season packs and upgrades up to the cutoff.
- **Collections and universes:** add a whole movie collection (e.g. *The Lord of the Rings Collection*) or a universe (e.g. the MCU, through its TMDB keyword) in one go. New movies announced later are added automatically.
- **User accounts and requests:** family and friends get their own logins and can request movies, shows and collections. An admin approves each request, or turns on auto-approve for users they trust.
- **Streaming tab:** browse Netflix, Apple TV+, Prime Video, Disney+, Max, Hulu, Paramount+, Peacock and more. Click a service's logo to see the movies and TV shows it offers, and add them with one click.
- **Library import:** brings in media that's already in your movies and TV folders. Omniarr matches each folder on TMDB and works out the quality and TRaSH score of each file already there, so upgrades work straight away. Renaming is optional.
- **Quality limits per library:** choose which qualities are allowed (your maximum and minimum), the quality to *upgrade until*, and whether upgrades happen at all.
- **Downloads:** a **built-in downloader** (NZBGet, shipped inside the image and run by Omniarr — no second container). Add your Usenet provider under **Settings → Downloads**. A separate NZBGet still works if you prefer. When a download finishes, Omniarr renames it with the TRaSH naming scheme and moves it (or hardlinks/copies it) into the title's folder. Failed downloads are blocklisted and searched again.

## 1080p and 4K versions

Omniarr has one **movies folder** and one **TV folder** (defaults `/data/media/movies` and `/data/media/tv`, set under **Settings → Root folders**, where **Browse…** lets you pick them). Every title gets its own folder inside.

When you add a title you choose **1080p**, **4K** or **1080p + 4K**. Each version is searched for, downloaded and upgraded on its own, with its own TRaSH profile:

| Version | TRaSH profile (default) |
|---|---|
| Movies, 1080p and lower | HD Bluray + WEB |
| Movies, 4K | UHD Bluray + WEB |
| TV, 1080p and lower | WEB-1080p |
| TV, 4K | WEB-2160p |

For example, the 1080p version never grabs a 2160p release. All four profiles can be changed to any TRaSH profile (Remux, SQP, Anime, French, German, …) under **Settings → Profiles**. You can add the missing version later from the title's page (**+ Add 4K version**), and remove one version without touching the other.

Both versions' files sit together in the title folder:

```
Dune Part Two (2024) {tmdb-693134}/
  Dune Part Two (2024) {tmdb-693134} - [Bluray-1080p][DTS-HD MA 5.1][x264]-GROUP.mkv
  Dune Part Two (2024) {tmdb-693134} - [Remux-2160p][TrueHD Atmos 7.1][DV HDR10][HEVC]-GROUP.mkv
```

Plex, Jellyfin and Emby show these as two versions of one movie and pick the right one for each screen. TRaSH naming puts the quality in the file name, so the two files never collide. A rescan gives 2160p files to the 4K version and everything else to the 1080p version.

## Install on Unraid

### Recommended: from GitHub (normal Unraid updates)

Every change pushed to this repository is tested and built automatically into the image `ghcr.io/nitronine/omniarr:latest`. Unraid installs and updates it like any other app.

1. In the Unraid web UI, open the **Docker** tab and scroll to the bottom. Under **Template Repositories**, add:
   ```
   https://github.com/NitroNine/omniarr
   ```
   then click **Save**.
2. Click **Add Container**, open the **Template** dropdown, and pick **omniarr** (listed under this repository).
3. Check the two paths, then click **Apply**:
   - **Config:** `/mnt/user/appdata/omniarr`
   - **Data:** your share holding both downloads and media, e.g. `/mnt/user/data`
4. Open the WebUI (port 8790) and create your admin account.

**Updating:** the Docker tab shows **update ready** when there's a new version. Click it and choose **Apply update**, or use **Check for Updates** / **Update All**. To update automatically, the *Auto Update Applications* plugin from Community Applications works with Omniarr too.

**Already installed with the install script?** Switch over without losing anything:
1. On the **Docker** tab, click the omniarr icon, then **Edit**.
2. Change **Repository** to `ghcr.io/nitronine/omniarr:latest` and click **Apply**.

Your settings, library and accounts stay in `/mnt/user/appdata/omniarr`. You can delete the `omniarr-src` folder afterwards.

### Alternative: build on the server (install script)



1. Copy the `omniarr` folder to your server, e.g. into the `appdata` share, so it ends up at `/mnt/user/appdata/omniarr-src`.
2. Open **Terminal** in the Unraid web UI (the `>_` icon, top right) and run:

```bash
cd /mnt/user/appdata/omniarr-src && bash install-unraid.sh
```

The script:
- builds the image,
- installs the Unraid template, so the container appears on the Docker tab with **Edit** and **WebUI** working,
- starts the container on port 8790 with `/mnt/user/appdata/omniarr` as `/config` and `/mnt/user/data` as `/data`,
- prints the address to open.

Re-run it to update to a newer copy of the code. Your settings and accounts are kept. To use a different media share or port:

```bash
DATA_SHARE=/mnt/user/media PORT=8791 bash install-unraid.sh
```

### Manual install

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
| `/downloads` | `/mnt/user/data/usenet` — where the built-in downloader saves files (optional; changeable any time in the container's Edit screen) |
| `PUID` / `PGID` | `99` / `100` |
| `OMNIARR_PASSWORD` | optional; creates the admin account on first start (otherwise the web UI asks you to create one) |

**Paths:** follow TRaSH's folder layout. The built-in downloader saves to the **Downloads** path from the container settings (`/downloads`; without one, `/data/usenet`). Keep it on the same share as your media, so moves into the library are instant and the `hardlink` import mode works. Using a separate NZBGet instead? Give it the same `/data` mapping, or set the remote path mapping under **Settings → Downloads**.

**Reverse proxy (Caddy, Nginx, SWAG…):** point it at `http://<unraid-ip>:8790`. Omniarr reads the
visitor's address from `X-Forwarded-For` (for wrong-password throttling) and makes its login cookie
HTTPS-only when `X-Forwarded-Proto` is `https`, trusting those headers only from local-network proxies.
Caddy example: `omniarr.example.duckdns.org { reverse_proxy 192.168.1.10:8790 }`.

### First-run setup (web UI at `http://<unraid-ip>:8790`)

1. **Settings → Profiles → "Sync TRaSH Guides now"** (afterwards it re-syncs automatically).
2. **Settings → Metadata:** enter your free TMDB API key (v3 key or v4 read token). Get one at themoviedb.org → Settings → API.
3. **Settings → Downloads:** **Add server** with your Usenet provider's address, port, SSL, username, password and connection count, then **Test**. Moving from a separate NZBGet? **Copy servers from my old NZBGet** brings its servers over; then you can remove that container.
4. **Settings → Indexers:** add each Usenet indexer (URL + API key). It's tested as soon as you add it.
5. **Settings → Root folders:** pick your movies folder and TV folder.
6. **Add:** search for a movie or show, pick **1080p** or **4K**, and click Add.

## Light and dark mode

Use the switch in the top-right corner: **Auto** follows your device's setting, or pick **Light** or **Dark**. Each browser remembers its own choice.

## Useful tools

- **Settings → Profiles → "Test a release name"** shows how a release title is parsed, which TRaSH custom formats match, and its score. Handy for working out why something was or wasn't grabbed.
- **Interactive search** on any movie, season or episode lists every result with its score and rejection reasons. You can grab any of them by hand.
- **Activity** shows the queue, history, an event log and the blocklist, plus buttons to run RSS sync or a backlog search now.

## Metadata: TMDB

Sonarr and Radarr don't ask for keys because their projects run their own metadata servers. Omniarr talks to **TMDB** directly: one free key covers movies, TV, collections and the Streaming tab.

TMDB also stores each show's TheTVDB and IMDb numbers. Omniarr uses those for indexer searches (TheTVDB number → IMDb number → show title) and for folder names like `{tvdb-81189}`, so no TheTVDB account is needed.

## Users and requests

The first time you open Omniarr, it asks you to create the **admin** account. If you set `OMNIARR_PASSWORD` in the Unraid template, that becomes the admin login instead.

Add more people under **Settings → Users**:

| | Admin | User |
|---|---|---|
| Browse Movies, TV, Collections, Streaming | ✓ | ✓ |
| Add titles | Directly | As a **request** |
| Settings, Import, Activity/downloads, delete, search | ✓ | ✗ |
| See file paths on disk | ✓ | ✗ |

Per-user options, which only an admin can change:
- **Auto-approve requests:** the user's requests are added straight away.
- **Can request 4K:** without it, users only see the 1080p library when requesting.
- **Enabled:** turning it off logs the user out everywhere.
- **Set password.**

**Requests tab:** users see their own requests. Each one moves through **Waiting for approval → Approved — downloading (2/10) → Available**, or **Declined** with the admin's reason.

Admins see everyone's requests, with a red badge in the menu showing how many are waiting. They can:
- **Approve**, and switch the request between 1080p and 4K when doing so,
- **Decline**, with an optional reason shown to the user,
- clear old requests from the list.

Everyone can change their own password under **Account**. Passwords are stored hashed (PBKDF2), and repeated failed logins are slowed down. If you put Omniarr behind an HTTPS reverse proxy, set `OMNIARR_SECURE_COOKIES=true`.

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

Go to **Import**, choose Movies or TV shows, and click **Scan root folders**. Omniarr lists every folder in your movies or TV folder that isn't in the library yet, plus loose movie files sitting directly in the movies folder. When a folder holds both 1080p and 4K files, both versions are added. Each one is matched:

1. **IDs in the folder name** win: `{tmdb-603}`, `[tmdbid-603]`, `{tvdb-81189}`, `[tvdbid-81189]`, `{imdb-tt0133093}`. These are marked "ID match".
2. Otherwise Omniarr searches by **title + year** read from the folder name (e.g. `The Matrix (1999)` or `The.Matrix.1999.1080p.BluRay-GRP`). Matches are marked "exact" or "guess". Use **Change** to fix any guess before importing.

Then choose the import options:
- **Rename files to TRaSH naming:** off by default, so your files stay exactly where they are.
- **Monitor** (TV shows): *All episodes*, *Only episodes I have*, *Future episodes*, or *None*.
- **Search for missing & upgrades now.**

Folder names are always kept. A loose movie file gets its own TRaSH-named folder.

Quality is read from the file name, falling back to the folder name, and then to MediaInfo (the file itself) for the resolution. Every file is scored with the library's TRaSH custom formats.

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
  library.py       add/refresh movies & series, 1080p/4K versions, folders
  services/metadata.py also covers streaming providers (TMDB watch providers / discover)
  auth.py          accounts, password hashing, sessions, what users may access
  requests_.py     user requests, approval / auto-approval
  collections_.py  TMDB collections / keyword universes, auto-adding new entries
  scanner.py       library import: scan root folders, match, detect existing files
  services/        TMDB, Newznab, NZBGet, MediaInfo
  static/          web UI (no build step)
tests/             unit + end-to-end tests (fake indexer / NZBGet)
unraid/omniarr.xml Unraid template
```

## Known limitations

- Usenet only (no torrents). Downloads use the built-in NZBGet or a separate NZBGet; no SABnzbd.
- Library import doesn't watch the disk. Use **Rescan folder** (or import again) after changing files by hand.
- TRaSH `IndexerFlagSpecification` custom formats (freeleech, etc.) never match, since they're torrent-oriented. Language CFs are inferred from release titles.

## Third-party software

The Docker image includes [NZBGet](https://github.com/nzbgetcom/nzbget) (GPL-2.0, source at that link)
as the built-in downloader, unmodified, with the unrar and 7-Zip binaries from its official release
(their licences are in `/opt/nzbget` inside the image). Omniarr runs it as a separate program.
