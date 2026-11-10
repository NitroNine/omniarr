#!/bin/bash
# Omniarr installer for Unraid.
#
# Run from the Unraid terminal (or SSH) inside the extracted omniarr folder:
#   bash install-unraid.sh
# Re-run it any time to update to a newer copy of the code; your settings,
# library and accounts in /mnt/user/appdata/omniarr are kept.
#
# Optional overrides (put them before the command), e.g.:
#   DATA_SHARE=/mnt/user/media PORT=8791 bash install-unraid.sh
set -euo pipefail

NAME="${NAME:-omniarr}"
PORT="${PORT:-8790}"
APPDATA="${APPDATA:-/mnt/user/appdata/omniarr}"
DATA_SHARE="${DATA_SHARE:-/mnt/user/data}"
PUID="${PUID:-99}"
PGID="${PGID:-100}"
TZ_VALUE="${TZ:-$(cat /etc/timezone 2>/dev/null || echo UTC)}"
SRC="$(cd "$(dirname "$0")" && pwd)"
TEMPLATE_DIR="/boot/config/plugins/dockerMan/templates-user"

say()  { printf '\n\033[1;34m==>\033[0m %s\n' "$*"; }
fail() { printf '\n\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

command -v docker >/dev/null || fail "Docker isn't available. Enable it under Settings -> Docker in the Unraid web UI."
docker info >/dev/null 2>&1 || fail "Docker isn't running. Start it under Settings -> Docker."
[ -f "$SRC/Dockerfile" ] || fail "Run this script from inside the extracted omniarr folder."

if [ ! -d "$DATA_SHARE" ]; then
  echo
  echo "The data share $DATA_SHARE doesn't exist."
  echo "Omniarr needs ONE share that holds both your downloads and your media"
  echo "(TRaSH Guides layout), e.g. /mnt/user/data/usenet and /mnt/user/data/media."
  echo
  echo "Either create that share in the Unraid web UI (Shares -> Add Share -> 'data'),"
  echo "or re-run pointing at your existing share, e.g.:"
  echo "  DATA_SHARE=/mnt/user/media bash install-unraid.sh"
  exit 1
fi

say "Building the Omniarr image (first time takes a few minutes)…"
echo "(Tip: installing from the GitHub template instead gives you normal Unraid updates — see the README.)"
docker build --pull -t "$NAME:latest" "$SRC"

say "Preparing $APPDATA"
mkdir -p "$APPDATA"
chown "$PUID:$PGID" "$APPDATA" 2>/dev/null || true

if [ -d "$TEMPLATE_DIR" ]; then
  say "Installing the Unraid template (so 'Edit' works on the Docker tab)"
  sed -e "s#<Repository>.*</Repository>#<Repository>$NAME:latest</Repository>#" \
      -e "s#>/mnt/user/data</Config>#>$DATA_SHARE</Config>#" \
      -e "s#Default=\"/mnt/user/data\"#Default=\"$DATA_SHARE\"#" \
      -e "s#>8790</Config>#>$PORT</Config>#" \
      "$SRC/unraid/omniarr.xml" > "$TEMPLATE_DIR/my-$NAME.xml"
fi

if docker ps -a --format '{{.Names}}' | grep -qx "$NAME"; then
  say "Replacing the existing $NAME container (your data is kept)"
  docker rm -f "$NAME" >/dev/null
fi

mkdir -p "$DATA_SHARE/usenet" && chown 99:100 "$DATA_SHARE/usenet" 2>/dev/null || true

say "Starting $NAME"
docker run -d \
  --name "$NAME" \
  --restart unless-stopped \
  -p "$PORT:8790" \
  -e PUID="$PUID" -e PGID="$PGID" -e UMASK=002 -e TZ="$TZ_VALUE" \
  -v "$APPDATA:/config" \
  -v "$DATA_SHARE:/data" \
  -v "$DATA_SHARE/usenet:/downloads" \
  -l net.unraid.docker.managed=dockerman \
  -l "net.unraid.docker.webui=http://[IP]:[PORT:$PORT]/" \
  "$NAME:latest" >/dev/null

say "Waiting for it to come up…"
for _ in $(seq 1 30); do
  if docker exec "$NAME" python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8790/health',timeout=2)" >/dev/null 2>&1; then
    IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
    echo
    echo "Omniarr is running:  http://${IP:-<your-unraid-ip>}:$PORT"
    echo
    echo "Next: open it, create your admin account, then go to Settings and"
    echo "  1. Profiles      -> Sync TRaSH Guides now"
    echo "  2. Metadata      -> your free TMDB API key (themoviedb.org -> Settings -> API)"
    echo "  3. NZBGet        -> URL / login, then Test"
    echo "  4. Indexers      -> add your Usenet indexers"
    echo "  5. Root folders  -> check the paths (inside the container, $DATA_SHARE is /data)"
    exit 0
  fi
  sleep 2
done
echo
docker logs --tail 40 "$NAME" || true
fail "Omniarr didn't start. The log above should say why."
