"""Saved routers: the routers admins add in the app (Manager > Routers).

Stored in <data dir>/routers.json (readable only by the app). Kept: the name, address,
SSH port, an optional default username and, optionally, a read-only SNMP login so the
app can connect over SNMP without anyone typing a password. Router SSH passwords are
never stored: each person types their own when they need SSH.
"""
import json
import os
import re
import threading
import time
import uuid

from auth import AuthError, _write_private

HOST_RE = re.compile(r"^[A-Za-z0-9.:\-\[\]]{1,253}$")
SNMP_AUTH = ("sha256", "sha384", "sha512", "sha224", "sha", "md5")
SNMP_PRIV = ("aes128", "aes192", "aes256", "des")


class RouterStore:
    def __init__(self, data_dir):
        self.path = os.path.join(data_dir, "routers.json")
        self.lock = threading.Lock()
        self.routers = []
        if os.path.exists(self.path):
            with open(self.path) as f:
                self.routers = json.load(f).get("routers", [])

    def _save(self):
        _write_private(self.path, json.dumps({"routers": self.routers}, indent=1))

    @staticmethod
    def _clean(d):
        name = (d.get("name") or "").strip()
        host = (d.get("host") or "").strip()
        username = (d.get("username") or "").strip()
        notes = (d.get("notes") or "").strip()
        if not 1 <= len(name) <= 60 or any(ord(c) < 32 for c in name):
            raise AuthError("Give the router a name (up to 60 characters).")
        if not HOST_RE.match(host):
            raise AuthError("Enter a valid management IP or hostname.")
        try:
            port = int(d.get("port") or 22)
        except (TypeError, ValueError):
            raise AuthError("Invalid SSH port.")
        if not 1 <= port <= 65535:
            raise AuthError("Invalid SSH port.")
        if len(username) > 64 or any(ord(c) < 33 for c in username):
            raise AuthError("Invalid username.")
        if len(notes) > 200 or any(ord(c) < 32 for c in notes):
            raise AuthError("Notes can be up to 200 characters on one line.")
        return {"name": name, "host": host, "port": port, "username": username, "notes": notes}

    @staticmethod
    def _clean_snmp(d, old):
        """SNMPv3 login from the form. Empty passwords keep the stored ones; snmp_user empty turns SNMP off."""
        user = (d.get("snmp_user") or "").strip()
        if not user:
            return None
        if len(user) > 32 or any(ord(c) < 33 for c in user):
            raise AuthError("Invalid SNMP user name.")
        auth_proto = (d.get("snmp_auth_proto") or "sha256").lower()
        priv_proto = (d.get("snmp_priv_proto") or "aes128").lower()
        if auth_proto not in SNMP_AUTH or priv_proto not in SNMP_PRIV:
            raise AuthError("Unsupported SNMP authentication or privacy protocol.")
        same_user = bool(old) and old.get("user") == user
        auth_key = d.get("snmp_auth_key") or (old.get("auth_key") if same_user else "")
        priv_key = d.get("snmp_priv_key") or (old.get("priv_key") if same_user else "")
        for k in (auth_key, priv_key):
            if not 8 <= len(k or "") <= 64 or any(ord(c) < 32 for c in k):
                raise AuthError("SNMP passwords must be 8 to 64 characters.")
        try:
            port = int(d.get("snmp_port") or 161)
        except (TypeError, ValueError):
            raise AuthError("Invalid SNMP port.")
        if not 1 <= port <= 65535:
            raise AuthError("Invalid SNMP port.")
        return {"user": user, "auth_proto": auth_proto, "auth_key": auth_key, "priv_proto": priv_proto,
                "priv_key": priv_key, "port": port}

    @staticmethod
    def public(r):
        """What browsers see: never the SNMP passwords."""
        out = {k: v for k, v in r.items() if k != "snmp"}
        s = r.get("snmp")
        out["snmp"] = {"user": s["user"], "auth_proto": s["auth_proto"], "priv_proto": s["priv_proto"], "port": s["port"]} if s else None
        return out

    def list(self):
        return [self.public(r) for r in sorted(self.routers, key=lambda r: r["name"].lower())]

    def get(self, rid):
        return next((r for r in self.routers if r["id"] == rid), None)

    def hosts(self):
        return [r["host"] for r in self.routers]

    def add(self, d):
        r = self._clean(d)
        r["snmp"] = self._clean_snmp(d, None)
        with self.lock:
            if any(x["name"].lower() == r["name"].lower() for x in self.routers):
                raise AuthError("A router with that name already exists.")
            r.update(id=uuid.uuid4().hex[:12], created=time.time())
            self.routers.append(r)
            self._save()
        return self.public(r)

    def update(self, rid, d):
        r = self._clean(d)
        with self.lock:
            cur = self.get(rid)
            if not cur:
                raise AuthError("No such router.")
            if any(x["id"] != rid and x["name"].lower() == r["name"].lower() for x in self.routers):
                raise AuthError("A router with that name already exists.")
            r["snmp"] = self._clean_snmp(d, cur.get("snmp"))
            cur.update(r)
            self._save()
        return self.public(cur)

    def delete(self, rid):
        with self.lock:
            cur = self.get(rid)
            if not cur:
                raise AuthError("No such router.")
            self.routers.remove(cur)
            self._save()
        return cur
