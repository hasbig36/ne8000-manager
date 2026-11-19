"""Licence signing. The store keeps an Ed25519 private key in <data dir>/licence_signing.key
(created on first start); copies of the app check licences with the matching public key."""
import base64
import hashlib
import json
import os
import time

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

PREFIX = "NE8K1"
GRACE_DAYS = int(os.environ.get("STORE_GRACE_DAYS", "7"))


def _b64(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


class Signer:
    def __init__(self, data_dir):
        path = os.path.join(data_dir, "licence_signing.key")
        if os.path.exists(path):
            with open(path, "rb") as f:
                self.key = serialization.load_pem_private_key(f.read(), None)
        else:
            self.key = Ed25519PrivateKey.generate()
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(self.key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                               serialization.NoEncryption()))

    @property
    def public_b64(self):
        raw = self.key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        return _b64(raw)

    def token(self, lic, customer_name):
        """Signed licence for a licence row. Valid until paid_until plus the grace days."""
        expires = lic["paid_until"] + GRACE_DAYS * 86400 if lic["status"] == "active" else 0
        payload = {"key": lic["key"], "customer": customer_name, "brand": lic.get("brand") or customer_name,
                   "logo": hashlib.sha256(lic["logo"]).hexdigest()[:16] if lic.get("logo") else "", "devices": int(lic["devices"]),
                   "expires": int(expires), "issued": int(time.time())}
        body = PREFIX + "." + _b64(json.dumps(payload, separators=(",", ":")).encode())
        return body + "." + _b64(self.key.sign(body.encode()))
