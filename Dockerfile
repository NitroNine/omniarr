# syntax=docker/dockerfile:1.7
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    OMNIARR_CONFIG=/config \
    PUID=99 \
    PGID=100 \
    UMASK=002 \
    PORT=8790

# ffprobe gives real media info (HDR type, audio codec...) for TRaSH naming tokens
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg tini \
 && rm -rf /var/lib/apt/lists/*

# Built-in downloader: NZBGet (GPL-2.0, https://github.com/nzbgetcom/nzbget), run by Omniarr
# on localhost only. The official Linux release bundles unrar, 7-Zip and par2 support.
ARG NZBGET_VERSION=26.3
ADD --checksum=sha256:6a7f0519cf7deeb59cc1457c87d6efc7f28ef3dd17ed5ddc3be6a698a6093391 \
    https://github.com/nzbgetcom/nzbget/releases/download/v${NZBGET_VERSION}/nzbget-${NZBGET_VERSION}-bin-linux.run /tmp/nzbget.run
RUN case "$(dpkg --print-architecture)" in \
      amd64) arch=x86_64 ;; arm64) arch=aarch64 ;; armhf) arch=armhf ;; \
      *) echo "unsupported architecture" >&2; exit 1 ;; esac \
 && sh /tmp/nzbget.run --destdir /opt/nzbget --arch "$arch" --silent \
 && rm -f /tmp/nzbget.run \
 && /opt/nzbget/nzbget -v

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

VOLUME ["/config"]
EXPOSE 8790
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8790\")}/health',timeout=4)"
ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
