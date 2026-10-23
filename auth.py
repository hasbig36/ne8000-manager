"""App logins for hosted use: users, roles, login throttling and the audit log.

Users live in <data dir>/users.json with salted password hashes (never plain text).
Roles:
  admin     everything, including managing app users
  operator  everything except managing app users
  viewer    read only: can connect and look, but cannot change the router, billing or plans
"""
import json
import os
import secrets
import threading
import time

from werkzeug.security import check_password_hash, generate_password_hash

ROLES = ("admin", "operator", "viewer")


class AuthError(Exception):
    pass


def _write_private(path, text):
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def secret_key(data_dir):
    """A session key that survives restarts, so users stay logged in."""
    env = os.environ.get("NE8000_SECRET_KEY")
    if env:
        return env
    path = os.path.join(data_dir, "secret_key")
    if os.path.exists(path):
        with open(path) as f:
            return f.read().strip()
    key = secrets.token_hex(32)
    _write_private(path, key)
    return key


class UserStore:
    def __init__(self, data_dir):
        self.path = os.path.join(data_dir, "users.json")
        self.lock = threading.Lock()
        self.users = {}
        if os.path.exists(self.path):
            with open(self.path) as f:
                self.users = json.load(f).get("users", {})

    def _save(self):
        _write_private(self.path, json.dumps({"users": self.users}, indent=1))

    @staticmethod
    def _check_name(name):
        name = (name or "").strip()
        if not (1 <= len(name) <= 40) or not all(c.isalnum() or c in "._-@" for c in name):
            raise AuthError("Usernames use letters, digits and . _ - @ (up to 40).")
        return name

    @staticmethod
    def _check_password(pw):
        if len(pw or "") < 10:
            raise AuthError("Passwords need at least 10 characters.")
        return pw

    def list(self):
        return [{"username": u, "role": d["role"], "created": d.get("created"), "last_login": d.get("last_login")}
                for u, d in sorted(self.users.items())]

    def add(self, name, password, role):
        name = self._check_name(name)
        if role not in ROLES:
            raise AuthError("Unknown role.")
        with self.lock:
            if name in self.users:
                raise AuthError("That user already exists.")
            self.users[name] = {"hash": generate_password_hash(self._check_password(password)), "role": role, "created": time.time()}
            self._save()

    def update(self, name, password=None, role=None):
        with self.lock:
            u = self.users.get(name)
            if not u:
                raise AuthError("No such user.")
            if role:
                if role not in ROLES:
                    raise AuthError("Unknown role.")
                if u["role"] == "admin" and role != "admin" and self._admins() == 1:
                    raise AuthError("Keep at least one admin.")
                u["role"] = role
            if password:
                u["hash"] = generate_password_hash(self._check_password(password))
                u["pw_changed"] = time.time()
            self._save()

    def delete(self, name):
        with self.lock:
            u = self.users.get(name)
            if not u:
                raise AuthError("No such user.")
            if u["role"] == "admin" and self._admins() == 1:
                raise AuthError("Keep at least one admin.")
            del self.users[name]
            self._save()

    def _admins(self):
        return sum(1 for u in self.users.values() if u["role"] == "admin")

    _DUMMY = generate_password_hash("not-a-real-password")

    def verify(self, name, password):
        u = self.users.get((name or "").strip())
        if not u:
            check_password_hash(self._DUMMY, password or "")  # same time for unknown users: no username probing
            return None
        if not check_password_hash(u["hash"], password or ""):
            return None
        with self.lock:
            u["last_login"] = time.time()
            self._save()
        return {"username": name.strip(), "role": u["role"], "pw_changed": u.get("pw_changed", u.get("created", 0))}

    def get(self, name):
        u = self.users.get(name)
        return {"username": name, "role": u["role"], "pw_changed": u.get("pw_changed", u.get("created", 0))} if u else None

    def ensure_admin(self):
        """First start: create the admin from NE8000_ADMIN_USER / NE8000_ADMIN_PASSWORD,
        or with a random password that is printed once to the console / container log."""
        if self.users:
            return None
        name = os.environ.get("NE8000_ADMIN_USER") or "admin"
        pw = os.environ.get("NE8000_ADMIN_PASSWORD")
        generated = not pw
        if generated:
            pw = secrets.token_urlsafe(12)
        self.add(name, pw, "admin")
        return (name, pw) if generated else (name, None)


class Throttle:
    """Locks a username or client address for a while after repeated failed logins."""

    def __init__(self, limit=5, window=300, lock_for=300):
        self.limit, self.window, self.lock_for = limit, window, lock_for
        self.fails, self.locked, self.lock = {}, {}, threading.Lock()

    def check(self, *keys):
        now = time.time()
        for k in keys:
            until = self.locked.get(k, 0)
            if until > now:
                raise AuthError(f"Too many failed logins. Try again in {int(until - now) // 60 + 1} min.")

    def fail(self, *keys):
        now = time.time()
        with self.lock:
            for k in keys:
                lst = [t for t in self.fails.get(k, []) if now - t < self.window] + [now]
                self.fails[k] = lst
                if len(lst) >= self.limit:
                    self.locked[k] = now + self.lock_for
                    self.fails[k] = []

    def ok(self, *keys):
        with self.lock:
            for k in keys:
                self.fails.pop(k, None)


class Audit:
    """One JSON line per event: logins, router connections and every change."""

    def __init__(self, data_dir):
        self.path = os.path.join(data_dir, "audit.log")
        self.lock = threading.Lock()

    def log(self, user, event, **detail):
        line = json.dumps({"at": time.time(), "user": user, "event": event, **detail})
        with self.lock:
            with open(self.path, "a") as f:
                f.write(line + "\n")

    def tail(self, n=300):
        if not os.path.exists(self.path):
            return []
        with open(self.path) as f:
            lines = f.readlines()[-n:]
        out = []
        for ln in reversed(lines):
            try:
                out.append(json.loads(ln))
            except ValueError:
                pass
        return out
