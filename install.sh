#!/bin/sh
# One-command install on Ubuntu/Debian:  sudo ./install.sh
# Installs Docker if missing, creates .env, builds and starts NE8000 Manager on 127.0.0.1:8080.
# Optional HTTPS:  sudo DOMAIN=ne8000.example.com ./install.sh   (needs DNS pointing here and ports 80/443 open)
set -e
cd "$(dirname "$0")"
if ! command -v docker >/dev/null 2>&1; then
  echo "Installing Docker..."
  curl -fsSL https://get.docker.com | sh
fi
if [ ! -f .env ]; then
  cp .env.example .env
  PW=$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 16)
  sed -i "s/^NE8000_ADMIN_PASSWORD=.*/NE8000_ADMIN_PASSWORD=$PW/" .env
  sed -i "s/^NE8000_ALLOW_DEMO=.*/NE8000_ALLOW_DEMO=0/" .env
  if [ -n "$DOMAIN" ]; then
    sed -i "s/^NE8000_DOMAIN=.*/NE8000_DOMAIN=$DOMAIN/" .env
  else
    sed -i "s/^NE8000_BEHIND_PROXY=.*/NE8000_BEHIND_PROXY=0/" .env
    echo "NE8000_BIND=0.0.0.0" >> .env
  fi
  chmod 600 .env
  echo "Created .env (admin password: $PW). Change it after the first sign-in."
fi
mkdir -p data && chown 1000 data
if [ -n "$DOMAIN" ]; then
  docker compose --profile https up -d --build
  echo "Open https://$DOMAIN"
else
  docker compose up -d --build
  IP=$(hostname -I | awk '{print $1}')
  echo "Open http://$IP:8080 from your network."
  echo "Only your LAN / VPN should reach port 8080, for example:  sudo ufw allow from 10.0.0.0/8 to any port 8080"
fi
