"""Branding: the product name and logo shown in the app (top bar, sign-in page, tab title).

The product is Yubo. A copy sold through the licence store shows the buyer's own company name and
logo instead, so their staff see their brand. Where the name comes from, first match wins:
1. what an admin saved under Manager > Branding (<data dir>/branding.json),
2. NE8000_BRAND_NAME in the environment,
3. the company name in the licence (copies sold through the store),
4. "Yubo".
The logo is a PNG, JPEG or WebP (no SVG: it could carry script) of up to 512 KB, saved as
<data dir>/brand_logo. A licensed copy also fetches the logo the buyer set in the store (checkout or billing account),
unless an admin uploaded or removed a logo in the app.
"""
import base64
import json
import os

from auth import AuthError, _write_private

DEFAULT_NAME = "Yubo"
MAX_LOGO = 512 * 1024
TYPES = {b"\x89PNG\r\n\x1a\n": "image/png", b"\xff\xd8\xff": "image/jpeg", b"RIFF": "image/webp"}


def logo_type(data):
    for magic, mime in TYPES.items():
        if data.startswith(magic):
            if mime == "image/webp" and data[8:12] != b"WEBP":
                continue
            return mime
    return ""


class Branding:
    def __init__(self, data_dir, licence=None, env=os.environ):
        self.path = os.path.join(data_dir, "branding.json")
        self.logo_path = os.path.join(data_dir, "brand_logo")
        self.env_name = (env.get("NE8000_BRAND_NAME") or "").strip()
        self.licence = licence
        self.saved = {}
        if os.path.exists(self.path):
            try:
                with open(self.path) as f:
                    self.saved = json.load(f)
            except (OSError, ValueError):
                self.saved = {}

    @property
    def name(self):
        lic = (getattr(self.licence, "payload", None) or {}).get("brand") or ""
        return self.saved.get("name") or self.env_name or lic or DEFAULT_NAME

    def logo(self):
        """(bytes, mime) or (None, '')."""
        try:
            with open(self.logo_path, "rb") as f:
                data = f.read(MAX_LOGO + 1)
        except OSError:
            return None, ""
        mime = logo_type(data)
        return (data, mime) if mime and len(data) <= MAX_LOGO else (None, "")

    def public(self):
        return {"name": self.name, "logo": self.logo()[0] is not None, "product": DEFAULT_NAME}

    def save(self, d):
        name = str(d.get("name") or "").strip()
        if len(name) > 40 or any(ord(c) < 32 for c in name):
            raise AuthError("The name can be up to 40 characters.")
        removed = bool(d.get("remove_logo")) or (self.saved.get("logo_removed") and not d.get("logo"))
        from_store = "" if (d.get("logo") or removed) else self.saved.get("logo_from_store", "")
        self.saved = {k: v for k, v in (("name", name), ("logo_removed", removed), ("logo_from_store", from_store)) if v}
        _write_private(self.path, json.dumps(self.saved))
        if d.get("remove_logo"):
            try:
                os.remove(self.logo_path)
            except FileNotFoundError:
                pass
        elif d.get("logo"):
            self.set_logo(base64.b64decode(str(d["logo"]).split(",")[-1], validate=False))
        return self.public()

    def wants_store_logo(self, version):
        """The logo the buyer set in the store is new, and the admin has not uploaded or removed one in the app."""
        if self.saved.get("logo_removed") or not version:
            return False
        own = os.path.exists(self.logo_path) and not self.saved.get("logo_from_store")
        return not own and self.saved.get("logo_from_store") != version

    def set_store_logo(self, data, version):
        self.set_logo(data)
        self.saved["logo_from_store"] = version
        _write_private(self.path, json.dumps(self.saved))

    def set_logo(self, data):
        if len(data) > MAX_LOGO:
            raise AuthError("The logo can be up to 512 KB.")
        if not logo_type(data):
            raise AuthError("The logo must be a PNG, JPEG or WebP picture.")
        tmp = self.logo_path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, self.logo_path)
