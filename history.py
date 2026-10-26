"""Long-term interface traffic history (like Cacti/LibreNMS graphs).

A background thread polls every saved router that has an SNMP login, every 5 minutes, whether
or not anyone is connected, and keeps:
  - 5-minute rates for 8 days (the 1 day and 1 week graphs)
  - hourly average and peak for 400 days (the 1 month and 1 year graphs)
in history.db (SQLite) in the data folder. About 50 interfaces for a year is well under 100 MB.
"""
import math
import os
import sqlite3
import threading
import time

from snmp import SnmpClient, SnmpError, text

INTERVAL = 300
KEEP_5MIN = 8 * 86400
KEEP_HOURLY = 400 * 86400
IFX = "1.3.6.1.2.1.31.1.1.1"
RANGES = {"day": 86400, "week": 7 * 86400, "month": 30 * 86400, "year": 365 * 86400}


class History:
    def __init__(self, data_dir, saved):
        self.path = os.path.join(data_dir, "history.db")
        self.saved = saved                  # routers.RouterStore
        self.clients = {}                   # router id -> (settings key, SnmpClient)
        self.counters = {}                  # (router id, ifIndex) -> (t, in octets, out octets)
        self.status = {}                    # router id -> {"time", "error", "interfaces"}
        self.stop = threading.Event()
        self.thread = None
        with self._db() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS s5 (rid TEXT, ifname TEXT, ts INTEGER, rx REAL, tx REAL);
                CREATE INDEX IF NOT EXISTS s5_i ON s5 (rid, ifname, ts);
                CREATE TABLE IF NOT EXISTS h1 (rid TEXT, ifname TEXT, ts INTEGER, rx_avg REAL, tx_avg REAL,
                                               rx_max REAL, tx_max REAL, n INTEGER, PRIMARY KEY (rid, ifname, ts));
            """)

    def _db(self):
        db = sqlite3.connect(self.path, timeout=30)
        return db

    # ---- collector ------------------------------------------------------------
    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._loop, daemon=True, name="history")
        self.thread.start()

    def _loop(self):
        last_prune = 0
        while not self.stop.is_set():
            t = time.time()
            for r in self.saved.list():
                if self.stop.is_set():
                    break
                try:
                    self.poll(r["id"])
                except Exception as exc:   # keep going for the other routers
                    self.status[r["id"]] = {"time": t, "error": str(exc)[:200], "interfaces": 0}
            if t - last_prune > 3600:
                self.prune(t)
                last_prune = t
            self.stop.wait(max(5, INTERVAL - (time.time() - t)))

    def _client(self, rid):
        full = self.saved.get(rid)
        snmp = (full or {}).get("snmp")
        if not snmp:
            old = self.clients.pop(rid, None)
            if old:
                old[1].close()
            return None
        key = (full["host"], tuple(sorted(snmp.items())))
        cur = self.clients.get(rid)
        if cur and cur[0] == key:
            return cur[1]
        if cur:
            cur[1].close()
        c = SnmpClient(full["host"], user=snmp["user"], auth_proto=snmp["auth_proto"], auth_key=snmp["auth_key"],
                       priv_proto=snmp["priv_proto"], priv_key=snmp["priv_key"], port=snmp.get("port", 161), timeout=5)
        self.clients[rid] = (key, c)
        return c

    def poll(self, rid):
        c = self._client(rid)
        if c is None:
            return
        t = time.time()
        try:
            names = c.walk(IFX + ".1", 5000)
            ins, outs = c.walk(IFX + ".6", 5000), c.walk(IFX + ".10", 5000)
        except SnmpError as exc:
            self.status[rid] = {"time": t, "error": str(exc)[:200], "interfaces": 0}
            return
        rows = []
        for idx, name in names.items():
            i, o = ins.get(idx), outs.get(idx)
            if not isinstance(i, int) or not isinstance(o, int):
                continue
            prev = self.counters.get((rid, idx))
            self.counters[(rid, idx)] = (t, i, o)
            if not prev or t - prev[0] < 30 or t - prev[0] > 3 * INTERVAL:
                continue
            di, do = i - prev[1], o - prev[2]
            if di < 0 or do < 0:          # counter reset (reboot, card swap)
                continue
            dt = t - prev[0]
            rows.append((text(name), di * 8 / dt, do * 8 / dt))
        if rows:
            self.store(rid, int(t), rows)
        self.status[rid] = {"time": t, "error": "", "interfaces": len(rows)}

    def store(self, rid, ts, rows):
        hour = ts - ts % 3600
        with self._db() as db:
            db.executemany("INSERT INTO s5 VALUES (?,?,?,?,?)", [(rid, n, ts, rx, tx) for n, rx, tx in rows])
            for n, rx, tx in rows:
                db.execute("""INSERT INTO h1 VALUES (?,?,?,?,?,?,?,1)
                              ON CONFLICT (rid, ifname, ts) DO UPDATE SET
                                rx_avg = (rx_avg * n + excluded.rx_avg) / (n + 1), tx_avg = (tx_avg * n + excluded.tx_avg) / (n + 1),
                                rx_max = max(rx_max, excluded.rx_max), tx_max = max(tx_max, excluded.tx_max), n = n + 1""",
                           (rid, n, hour, rx, tx, rx, tx))

    def prune(self, now):
        with self._db() as db:
            db.execute("DELETE FROM s5 WHERE ts < ?", (now - KEEP_5MIN,))
            db.execute("DELETE FROM h1 WHERE ts < ?", (now - KEEP_HOURLY,))

    # ---- queries ---------------------------------------------------------------
    def series(self, rid, ifname, rng):
        """[[t, rx_bps, tx_bps, rx_peak, tx_peak], ...] for 'day', 'week', 'month' or 'year'."""
        span = RANGES[rng]
        since = time.time() - span
        with self._db() as db:
            if rng == "day":
                q = "SELECT ts, rx, tx, rx, tx FROM s5 WHERE rid=? AND ifname=? AND ts>? ORDER BY ts"
                args = (rid, ifname, since)
            elif rng == "week":       # 30-minute buckets
                q = """SELECT ts - ts % 1800 AS b, avg(rx), avg(tx), max(rx), max(tx) FROM s5
                       WHERE rid=? AND ifname=? AND ts>? GROUP BY b ORDER BY b"""
                args = (rid, ifname, since)
            elif rng == "month":
                q = "SELECT ts, rx_avg, tx_avg, rx_max, tx_max FROM h1 WHERE rid=? AND ifname=? AND ts>? ORDER BY ts"
                args = (rid, ifname, since)
            else:                     # daily buckets
                q = """SELECT ts - ts % 86400 AS b, sum(rx_avg * n) / sum(n), sum(tx_avg * n) / sum(n), max(rx_max), max(tx_max)
                       FROM h1 WHERE rid=? AND ifname=? AND ts>? GROUP BY b ORDER BY b"""
                args = (rid, ifname, since)
            return [[int(r[0]), round(r[1]), round(r[2]), round(r[3]), round(r[4])] for r in db.execute(q, args)]

    def close(self):
        self.stop.set()
        for _, c in self.clients.values():
            c.close()


def synthetic(base, rng, seed=0):
    """Made-up history for demo mode: a daily curve around the interface's usual rate."""
    span, now = RANGES[rng], time.time()
    step = {"day": 300, "week": 1800, "month": 3600, "year": 86400}[rng]
    out = []
    t = now - span - (now % step)
    while t <= now:
        hour = (t % 86400) / 3600
        day = 0.55 + 0.45 * math.sin((hour - 14) / 24 * 2 * math.pi + math.pi / 2)
        growth = 0.8 + 0.2 * (1 - (now - t) / RANGES["year"])
        wob = 0.9 + 0.1 * math.sin(t / 977 + seed) * math.sin(t / 4111 + seed * 3)
        rx = base * day * growth * wob
        tx = rx * (0.18 + 0.04 * math.sin(t / 7200 + seed))
        peak = 1.25 if step > 300 else 1.0
        out.append([int(t), round(rx), round(tx), round(rx * peak), round(tx * peak)])
        t += step
    return out
