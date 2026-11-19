"""Device licence for copies of NE8000 Manager sold through the licence store (store/).

Off unless NE8000_LICENCE_KEY is set, so your own install keeps working with any number
of routers. When it is set (the store's install script and hosted servers set it):
- the app fetches a signed licence from NE8000_LICENCE_SERVER every 12 hours and keeps
  the last good one in <data dir>/licence.json, so a short outage of the store is fine;
- the licence is checked with the store's public key (NE8000_LICENCE_PUBKEY);
- the number of saved routers is limited to the devices paid for, and once the licence
  has expired the app no longer connects to routers until it is renewed.

A licence token is  NE8K1.<base64url JSON payload>.<base64url Ed25519 signature>.
The payload has: key, customer, brand (the name the buyer's copy shows), devices, expires
(unix time), issued.
"""
import base64
import json
import os
import threading
import time
import urllib.parse
import urllib.request

PREFIX = "NE8K1"
REFRESH = 12 * 3600


class LicenceError(Exception):
    pass


def _b64d(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def verify(token, pubkey_b64):
    """The licence payload if the token is signed by the store, else LicenceError."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    try:
        prefix, body, sig = (token or "").strip().split(".")
        if prefix != PREFIX:
            raise ValueError
        Ed25519PublicKey.from_public_bytes(_b64d(pubkey_b64)).verify(_b64d(sig), (prefix + "." + body).encode())
        payload = json.loads(_b64d(body))
    except (ValueError, InvalidSignature, TypeError):
        raise LicenceError("The licence is not valid for this server.")
    if not isinstance(payload.get("devices"), int) or not isinstance(payload.get("expires"), (int, float)):
        raise LicenceError("The licence is not valid for this server.")
    return payload


class Licence:
    def __init__(self, data_dir, env=os.environ):
        self.key = (env.get("NE8000_LICENCE_KEY") or "").strip()
        self.server = (env.get("NE8000_LICENCE_SERVER") or "").strip().rstrip("/")
        self.pubkey = (env.get("NE8000_LICENCE_PUBKEY") or "").strip()
        self.path = os.path.join(data_dir, "licence.json")
        self.lock = threading.Lock()
        self.token, self.payload, self.error, self.checked = "", None, "", 0
        self.on_refresh = None     # called after a good refresh (the app fetches the buyer's logo)
        if self.enabled and os.path.exists(self.path):
            try:
                with open(self.path) as f:
                    self._accept(json.load(f).get("token", ""), save=False)
            except (OSError, ValueError, LicenceError):
                pass

    @property
    def enabled(self):
        return bool(self.key)

    def _accept(self, token, save=True):
        payload = verify(token, self.pubkey)
        if payload.get("key") != self.key:
            raise LicenceError("The licence belongs to another key.")
        self.token, self.payload, self.error = token, payload, ""
        if save:
            from auth import _write_private
            _write_private(self.path, json.dumps({"token": token}))

    def refresh(self):
        """Fetch the current licence from the store (renewals and added devices show up here)."""
        if not self.enabled:
            return
        try:
            q = urllib.parse.urlencode({"key": self.key})
            with urllib.request.urlopen(f"{self.server}/api/licence?{q}", timeout=20) as resp:
                token = json.load(resp).get("token", "")
            with self.lock:
                self._accept(token)
            if self.on_refresh:
                self.on_refresh(self)
        except LicenceError as exc:
            self.error = str(exc)
        except Exception as exc:  # network, store down: keep the last good licence
            self.error = f"Could not reach the licence server: {exc}"[:200]
        self.checked = time.time()

    def start(self):
        if not self.enabled:
            return

        def loop():
            while True:
                self.refresh()
                time.sleep(REFRESH)
        threading.Thread(target=loop, daemon=True, name="licence").start()

    def status(self):
        if not self.enabled:
            return {"enabled": False}
        p = self.payload or {}
        return {"enabled": True, "valid": self.valid(), "devices": p.get("devices", 0), "expires": p.get("expires"),
                "customer": p.get("customer", ""), "error": self.error, "renew_url": self.server + "/account"}

    def valid(self):
        return bool(self.payload) and self.payload["expires"] > time.time()

    def check_connect(self):
        if self.enabled and not self.valid():
            raise LicenceError(f"Your NE8000 Manager licence has expired or could not be checked. "
                               f"Renew it at {self.server}/account.")

    def check_add(self, saved_count):
        if not self.enabled:
            return
        self.check_connect()
        if saved_count >= self.payload["devices"]:
            n = self.payload["devices"]
            raise LicenceError(f"Your licence covers {n} device{'s' if n != 1 else ''}. "
                               f"Add more devices at {self.server}/account.")


def fetch_logo(lic, brand):
    """Download the logo the buyer set in the store when it changed."""
    version = str((lic.payload or {}).get("logo") or "")
    if not (lic.enabled and brand.wants_store_logo(version)):
        return
    try:
        q = urllib.parse.urlencode({"key": lic.key})
        with urllib.request.urlopen(f"{lic.server}/api/brand-logo?{q}", timeout=20) as resp:
            brand.set_store_logo(resp.read(512 * 1024 + 1), version)
    except Exception:
        pass
