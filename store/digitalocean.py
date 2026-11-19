"""Hosted installs on DigitalOcean.

Creates a droplet whose cloud-init downloads the app from this store, installs it with the
customer's licence and a generated admin password, turns on HTTPS at <ip>.sslip.io and then
tells the store it is ready, so the store can email the login details.

Settings: DO_TOKEN (a DigitalOcean API token with write access), DO_REGION (default fra1),
DO_SSH_KEYS (optional comma-separated key IDs or fingerprints so you can log in to servers),
DO_PROJECT_ID (optional, to file servers under a DigitalOcean project).
"""
import json
import os
import shlex
import urllib.request

API = "https://api.digitalocean.com/v2"


class DOError(Exception):
    pass


class DigitalOcean:
    def __init__(self, env=os.environ):
        self.token = env.get("DO_TOKEN", "")
        self.region = env.get("DO_REGION", "fra1")
        self.ssh_keys = [k.strip() for k in env.get("DO_SSH_KEYS", "").split(",") if k.strip()]
        self.project = env.get("DO_PROJECT_ID", "")
        self._prices = None

    @property
    def configured(self):
        return bool(self.token)

    def _http(self, method, path, body=None):
        req = urllib.request.Request(API + path, method=method, data=json.dumps(body).encode() if body is not None else None)
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            try:
                msg = json.load(exc).get("message", "")
            except Exception:
                msg = ""
            raise DOError(f"DigitalOcean error {exc.code}: {msg or exc.reason}")
        except OSError as exc:
            raise DOError(f"Could not reach DigitalOcean: {exc}")

    def prices(self):
        """{size slug: monthly USD} from the DigitalOcean API, cached. {} when not set up."""
        if not self.configured:
            return {}
        if self._prices is None:
            try:
                sizes = self._http("GET", "/sizes?per_page=200").get("sizes", [])
                self._prices = {s["slug"]: float(s["price_monthly"]) for s in sizes}
            except DOError:
                return {}
        return self._prices

    def create(self, name, slug, user_data, tags=()):
        if not self.configured:
            raise DOError("DigitalOcean is not set up on this store yet.")
        body = {"name": name, "region": self.region, "size": slug, "image": "ubuntu-24-04-x64",
                "user_data": user_data, "tags": ["ne8000-manager", *tags], "monitoring": True, "ipv6": True}
        if self.ssh_keys:
            body["ssh_keys"] = self.ssh_keys
        d = self._http("POST", "/droplets", body)["droplet"]
        if self.project:
            try:
                self._http("POST", f"/projects/{self.project}/resources", {"resources": [f"do:droplet:{d['id']}"]})
            except DOError:
                pass
        return str(d["id"])

    def ip(self, droplet_id):
        d = self._http("GET", f"/droplets/{droplet_id}")["droplet"]
        for n in d.get("networks", {}).get("v4", []):
            if n.get("type") == "public":
                return n["ip_address"]
        return ""


def cloud_init(store_url, licence_key, pubkey, admin_password, callback_url):
    """Shell script run as root on the new droplet's first boot. The key, password and public key
    only hold letters, digits, - and _ (checked here), so they are safe inside the script."""
    import re
    for v in (licence_key, pubkey, admin_password):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", v):
            raise DOError("Unexpected characters in install settings.")
    for u in (store_url, callback_url):
        if not re.fullmatch(r"https?://[A-Za-z0-9.:/_-]+", u):
            raise DOError("Unexpected characters in the store URL.")
    return "\n".join([
        "#!/bin/bash",
        "set -euo pipefail",
        "exec > /var/log/ne8000-install.log 2>&1",
        "IP=$(curl -s http://169.254.169.254/metadata/v1/interfaces/public/0/ipv4/address)",
        'DOMAIN="$(echo "$IP" | tr . -).sslip.io"',
        "mkdir -p /opt/ne8000 && cd /opt/ne8000",
        f'curl -fsS -H "X-Licence-Key: {licence_key}" "{store_url}/api/download" -o app.tar.gz',
        "tar xzf app.tar.gz && rm app.tar.gz",
        "cp .env.example .env",
        f'sed -i "s/^NE8000_ADMIN_PASSWORD=.*/NE8000_ADMIN_PASSWORD={admin_password}/" .env',
        'sed -i "s/^NE8000_ALLOW_DEMO=.*/NE8000_ALLOW_DEMO=0/" .env',
        'sed -i "s/^NE8000_DOMAIN=.*/NE8000_DOMAIN=$DOMAIN/" .env',
        f'echo "NE8000_LICENCE_KEY={licence_key}" >> .env',
        f'echo "NE8000_LICENCE_SERVER={store_url}" >> .env',
        f'echo "NE8000_LICENCE_PUBKEY={pubkey}" >> .env',
        "chmod 600 .env",
        "ufw allow 22/tcp || true; ufw allow 80/tcp || true; ufw allow 443/tcp || true",
        'DOMAIN="$DOMAIN" sh ./install.sh',
        "echo '30 3 * * * root sh /opt/ne8000/update.sh --if-newer >> /var/log/yubo-update.log 2>&1' > /etc/cron.d/yubo-update",
        'for i in $(seq 1 60); do curl -fsS "https://$DOMAIN/healthz" >/dev/null 2>&1 && break; sleep 10; done',
        'curl -fsS -X POST -H "Content-Type: application/json" '
        f'-d "{{\\"ip\\":\\"$IP\\",\\"url\\":\\"https://$DOMAIN\\"}}" "{callback_url}"',
        "",
    ])
