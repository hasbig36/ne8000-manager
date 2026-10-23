#!/bin/sh
# Pull the latest version and restart:  sudo ./update.sh
# Your data (users, plans, billing settings, audit log) in ./data and your .env are kept.
set -e
cd "$(dirname "$0")"
cp -a data "data.backup-$(date +%Y%m%d-%H%M%S)" 2>/dev/null || true
git pull --ff-only
if grep -q "^NE8000_DOMAIN=" .env && grep -q "^NE8000_BEHIND_PROXY=1" .env && docker compose ps caddy -q 2>/dev/null | grep -q .; then
  docker compose --profile https up -d --build
else
  docker compose up -d --build
fi
docker image prune -f >/dev/null
echo "Updated to $(git log -1 --format='%h %s')"
