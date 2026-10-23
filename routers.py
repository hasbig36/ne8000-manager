"""Saved routers: the routers admins add in the app (Manager > Routers).

Stored in <data dir>/routers.json. Only the name, address, SSH port and an optional
default username are kept. Router passwords are never stored: each person types
their own when connecting.
"""
import json
import os
import re
import threading
import time
import uuid

from auth import AuthError, _write_private

HOST_RE = re.compile(r"^[A-Za-z0-9.:\-\[\]]{1,253}$")


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

    def list(self):
        return sorted(self.routers, key=lambda r: r["name"].lower())

    def get(self, rid):
        return next((r for r in self.routers if r["id"] == rid), None)

    def hosts(self):
        return [r["host"] for r in self.routers]

    def add(self, d):
        r = self._clean(d)
        with self.lock:
            if any(x["name"].lower() == r["name"].lower() for x in self.routers):
                raise AuthError("A router with that name already exists.")
            r.update(id=uuid.uuid4().hex[:12], created=time.time())
            self.routers.append(r)
            self._save()
        return r

    def update(self, rid, d):
        r = self._clean(d)
        with self.lock:
            cur = self.get(rid)
            if not cur:
                raise AuthError("No such router.")
            if any(x["id"] != rid and x["name"].lower() == r["name"].lower() for x in self.routers):
                raise AuthError("A router with that name already exists.")
            cur.update(r)
            self._save()
        return cur

    def delete(self, rid):
        with self.lock:
            cur = self.get(rid)
            if not cur:
                raise AuthError("No such router.")
            self.routers.remove(cur)
            self._save()
        return cur
