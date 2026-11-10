#!/bin/sh
# Run as the Unraid-style PUID/PGID (default nobody:users = 99:100).
set -e
umask "${UMASK:-002}"
mkdir -p "${OMNIARR_CONFIG:-/config}"
if [ "$(id -u)" = "0" ]; then
  chown -R "${PUID}:${PGID}" "${OMNIARR_CONFIG:-/config}" 2>/dev/null || true
  exec setpriv --reuid="${PUID}" --regid="${PGID}" --clear-groups \
    uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8790}" --proxy-headers
fi
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8790}" --proxy-headers
