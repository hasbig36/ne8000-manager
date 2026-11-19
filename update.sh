#!/bin/sh
# Update to the latest version and restart:  sudo ./update.sh
# Your data (users, plans, billing settings, audit log) in ./data and your .env are kept, and ./data is
# backed up first. A git checkout pulls from git; a licensed copy downloads the latest release from the
# licence store with its key. With --if-newer (used by the nightly job) it does nothing when up to date.
set -e
cd "$(dirname "$0")"
current=$(cat VERSION 2>/dev/null || echo 0)

if [ -d .git ]; then
  [ "$1" = "--if-newer" ] && { git fetch -q; [ "$(git rev-parse HEAD)" = "$(git rev-parse '@{u}')" ] && exit 0; }
  cp -a data "data.backup-$(date +%Y%m%d-%H%M%S)" 2>/dev/null || true
  git pull --ff-only
else
  KEY=$(sed -n 's/^NE8000_LICENCE_KEY=//p' .env)
  SERVER=$(sed -n 's/^NE8000_LICENCE_SERVER=//p' .env)
  [ -n "$KEY" ] && [ -n "$SERVER" ] || { echo "No licence key in .env: cannot download updates."; exit 1; }
  latest=$(curl -fsS "$SERVER/api/version" | sed -n 's/.*"version": *"\([^"]*\)".*/\1/p')
  [ -n "$latest" ] || { echo "Could not reach the licence server."; exit 1; }
  if [ "$latest" = "$current" ]; then
    [ "$1" = "--if-newer" ] && exit 0
    echo "Already on the latest version ($current). Reinstalling it."
  fi
  curl -fsS -H "X-Licence-Key: $KEY" "$SERVER/api/download" -o /tmp/yubo-update.tar.gz \
    || { echo "Download refused: is the licence active? Renew at $SERVER/account"; exit 1; }
  cp -a data "data.backup-$(date +%Y%m%d-%H%M%S)" 2>/dev/null || true
  tar xzf /tmp/yubo-update.tar.gz && rm -f /tmp/yubo-update.tar.gz
  # keep the 5 newest backups
  ls -1d data.backup-* 2>/dev/null | sort | head -n -5 | xargs -r rm -rf
fi

if grep -q "^NE8000_DOMAIN=" .env && grep -q "^NE8000_BEHIND_PROXY=1" .env && docker compose ps caddy -q 2>/dev/null | grep -q .; then
  docker compose --profile https up -d --build
else
  docker compose up -d --build
fi
docker image prune -f >/dev/null
echo "Updated from $current to $(cat VERSION 2>/dev/null)"
