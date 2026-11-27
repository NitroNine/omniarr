# syntax=docker/dockerfile:1.7

# Music: deemix (GPL-3.0, https://github.com/bambanah/deemix), the Deezer downloader, built from a pinned tag.
# Only its command-line tool is kept: one self-contained JavaScript bundle that Omniarr runs once per album.
# Built on the build machine's own platform (it's plain JavaScript); Node.js itself comes from the target's image.
FROM --platform=$BUILDPLATFORM node:24-bookworm-slim AS deemix
ARG DEEMIX_REF=deemix-webui@4.8.0
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --branch "${DEEMIX_REF}" https://github.com/bambanah/deemix /src
WORKDIR /src
RUN corepack enable \
 && pnpm install --frozen-lockfile --ignore-scripts --filter "deemix-cli..." \
 && pnpm --filter "deemix-cli..." run build \
 && mkdir -p /out \
 && cp packages/cli/dist/main.cjs /out/deemix.cjs \
 && cp LICENSE.txt /out/LICENSE.txt \
 && cd /out && node deemix.cjs --version

FROM node:24-bookworm-slim AS node

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    OMNIARR_CONFIG=/config \
    PUID=99 \
    PGID=100 \
    UMASK=002 \
    PORT=8790

# MediaInfo (what Sonarr/Radarr use) reads the real HDR type, audio codec and languages for TRaSH naming
# tokens; a few MB, where ffmpeg was ~450 MB
RUN apt-get update \
 && apt-get install -y --no-install-recommends mediainfo tini qbittorrent-nox \
 && rm -rf /var/lib/apt/lists/*

# Built-in downloader: NZBGet (GPL-2.0, https://github.com/nzbgetcom/nzbget), run by Omniarr
# on localhost only. The official Linux release bundles unrar, 7-Zip and par2 support.
# Downloaded, checked, installed and deleted in one step so the installer doesn't stay in a layer.
ARG NZBGET_VERSION=26.3
ARG NZBGET_SHA256=6a7f0519cf7deeb59cc1457c87d6efc7f28ef3dd17ed5ddc3be6a698a6093391
RUN case "$(dpkg --print-architecture)" in \
      amd64) arch=x86_64 ;; arm64) arch=aarch64 ;; armhf) arch=armhf ;; \
      *) echo "unsupported architecture" >&2; exit 1 ;; esac \
 && python -c "import urllib.request,sys; urllib.request.urlretrieve(sys.argv[1], '/tmp/nzbget.run')" \
      "https://github.com/nzbgetcom/nzbget/releases/download/v${NZBGET_VERSION}/nzbget-${NZBGET_VERSION}-bin-linux.run" \
 && echo "${NZBGET_SHA256}  /tmp/nzbget.run" | sha256sum -c - \
 && sh /tmp/nzbget.run --destdir /opt/nzbget --arch "$arch" --silent \
 && rm -f /tmp/nzbget.run \
 && /opt/nzbget/nzbget -v

# Node.js + the deemix bundle (Music); Omniarr runs it with DEEMIX_DATA_DIR=/config/deemix
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=deemix /out /opt/deemix
RUN node --version && cd /tmp && node /opt/deemix/deemix.cjs --version

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

VOLUME ["/config"]
EXPOSE 8790 6882 6882/udp
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8790\")}/health',timeout=4)"
ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
