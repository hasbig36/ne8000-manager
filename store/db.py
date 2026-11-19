"""SQLite storage for the store: customers, licences, orders (invoices), payments, servers."""
import json
import os
import secrets
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
  id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, name TEXT NOT NULL, company TEXT, phone TEXT,
  country TEXT, password_hash TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS licences (
  id INTEGER PRIMARY KEY, key TEXT UNIQUE NOT NULL, customer_id INTEGER NOT NULL REFERENCES customers(id),
  devices INTEGER NOT NULL, device_list TEXT NOT NULL, hosting TEXT NOT NULL, server_slug TEXT,
  hosted_monthly REAL NOT NULL DEFAULT 0, status TEXT NOT NULL, paid_until REAL NOT NULL DEFAULT 0, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS orders (
  id INTEGER PRIMARY KEY, ref TEXT UNIQUE NOT NULL, customer_id INTEGER NOT NULL REFERENCES customers(id),
  licence_id INTEGER NOT NULL REFERENCES licences(id), kind TEXT NOT NULL, description TEXT NOT NULL,
  devices_added INTEGER NOT NULL DEFAULT 0, amount_usd REAL NOT NULL, amount_kes INTEGER NOT NULL,
  status TEXT NOT NULL, method TEXT, created REAL NOT NULL, paid REAL);
CREATE TABLE IF NOT EXISTS payments (
  id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES orders(id), method TEXT NOT NULL,
  status TEXT NOT NULL, amount REAL NOT NULL, currency TEXT NOT NULL, phone TEXT, checkout_id TEXT UNIQUE,
  receipt TEXT, note TEXT, created REAL NOT NULL, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS servers (
  id INTEGER PRIMARY KEY, licence_id INTEGER NOT NULL REFERENCES licences(id), droplet_id TEXT, region TEXT,
  slug TEXT, ip TEXT, url TEXT, status TEXT NOT NULL, admin_password TEXT, callback_token TEXT,
  log TEXT NOT NULL DEFAULT '', created REAL NOT NULL, updated REAL NOT NULL);
"""


class DB:
    def __init__(self, path):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(licences)")}
        if "brand" not in cols:           # the buyer's own name and logo, shown in their copy
            self.conn.execute("ALTER TABLE licences ADD COLUMN brand TEXT")
            self.conn.execute("ALTER TABLE licences ADD COLUMN logo BLOB")
        self.lock = threading.RLock()
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def q(self, sql, *args):
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql, *args):
        r = self.q(sql, *args)
        return r[0] if r else None

    def run(self, sql, *args):
        with self.lock:
            return self.conn.execute(sql, args).lastrowid

    def tx(self):
        """with db.tx(): ... runs the statements in one transaction."""
        db = self

        class _Tx:
            def __enter__(self):
                db.lock.acquire()
                db.conn.execute("BEGIN IMMEDIATE")

            def __exit__(self, et, ev, tb):
                try:
                    db.conn.execute("ROLLBACK" if et else "COMMIT")
                finally:
                    db.lock.release()
        return _Tx()


def new_key():
    """Licence key the customer types: NE8K-XXXXX-XXXXX-XXXXX-XXXXX (no 0/O/1/I)."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "NE8K-" + "-".join("".join(secrets.choice(alphabet) for _ in range(5)) for _ in range(4))


def new_ref():
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "INV" + "".join(secrets.choice(alphabet) for _ in range(7))


def now():
    return time.time()


def dumps(v):
    return json.dumps(v, separators=(",", ":"))
