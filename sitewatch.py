"""Major sites: how Google, YouTube, Facebook and the country's other most visited sites are reached.

Every 1 to 5 minutes (a setting, 5 by default) this server pings each site (5 pings); every 5 minutes, or when the
delay jumps, it also traces the path to it. The server's own traffic
goes out through the router, so the path shows which way the network sends traffic to that site (the
upstream, peering point or cache) and the ping shows the delay customers get. It keeps a day of samples
and logs when the delay jumps or the path changes. Nothing here talks to the router.

The trace needs no root: a UDP probe per hop with a rising TTL, the reply read from the socket's error
queue (IP_RECVERR), like the tracepath tool.
"""
import json
import os
import select
import socket
import sqlite3
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import health

INTERVAL = 300          # default time between pings; the setting is 60..300 s
TRACE_EVERY = 300       # full traces at most this often (and when the delay jumps)
KEEP = 2 * 86400
MAX_HOPS = 18
DEFAULT = [
    {"name": "Google", "host": "google.com"}, {"name": "YouTube", "host": "youtube.com"}, {"name": "Facebook", "host": "facebook.com"},
    {"name": "WhatsApp", "host": "web.whatsapp.com"}, {"name": "Instagram", "host": "instagram.com"}, {"name": "TikTok", "host": "tiktok.com"},
    {"name": "X (Twitter)", "host": "x.com"}, {"name": "Wikipedia", "host": "wikipedia.org"}, {"name": "ChatGPT", "host": "chatgpt.com"},
    {"name": "Netflix", "host": "netflix.com"}, {"name": "Safaricom", "host": "safaricom.co.ke"}, {"name": "eCitizen", "host": "ecitizen.go.ke"},
]
IP_RECVERR = getattr(socket, "IP_RECVERR", 11)
SO_EE_ORIGIN_ICMP = 2


def tracepath(ip, max_hops=MAX_HOPS, timeout=1.0):
    """[{hop, ip, ms}] towards ip: one UDP probe per TTL; stops at the target or after 4 silent hops in a row."""
    hops, quiet = [], 0
    for ttl in range(1, max_hops + 1):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        hop = ms = None
        done = False
        try:
            s.setsockopt(socket.SOL_IP, IP_RECVERR, 1)
            s.setsockopt(socket.SOL_IP, socket.IP_TTL, ttl)
            s.setblocking(False)
            t0 = time.time()
            s.sendto(b"\0" * 24, (ip, 33434 + ttl))
            end = t0 + timeout
            while True:
                left = end - time.time()
                if left <= 0:
                    break
                r, _, _ = select.select([s], [], [], left)
                if not r:
                    break
                try:
                    _, anc, _, _ = s.recvmsg(256, 512, socket.MSG_ERRQUEUE)
                except (BlockingIOError, InterruptedError):
                    try:
                        s.recv(256)      # an answer from the target itself
                        hop, ms, done = ip, (time.time() - t0) * 1000, True
                    except OSError:
                        pass
                    break
                for lvl, typ, cd in anc:
                    if lvl == socket.SOL_IP and typ == IP_RECVERR and len(cd) >= 24:
                        _err, origin, itype, _code = struct.unpack("=IBBB", cd[:7])
                        hop = socket.inet_ntoa(cd[20:24])
                        ms = (time.time() - t0) * 1000
                        if origin == SO_EE_ORIGIN_ICMP and itype == 3 or hop == ip:
                            done = True
                break
        except OSError:
            pass
        finally:
            s.close()
        hops.append({"hop": ttl, "ip": hop, "ms": round(ms, 1) if ms is not None else None})
        quiet = 0 if hop else quiet + 1
        if done or quiet >= 4:
            break
    while hops and not hops[-1]["ip"]:
        hops.pop()
    return hops


def _public(ip):
    try:
        a = [int(x) for x in ip.split(".")]
    except (AttributeError, ValueError):
        return False
    return not (a[0] in (10, 127, 0) or a[0] == 172 and 16 <= a[1] < 32 or a[0] == 192 and a[1] == 168 or a[0] == 100 and 64 <= a[1] < 128
                or a[0] == 169 and a[1] == 254)


def clean_host(host):
    """A host name or IPv4 address from what was typed (a pasted link is cut down to its host); ValueError if it is not one."""
    host = str(host or "").strip().lower()[:200]
    host = host.replace("https://", "").replace("http://", "").split("/")[0].split("?")[0][:120]
    if not host or not all(c.isalnum() or c in ".-" for c in host) or host.startswith(("-", ".")):
        raise ValueError(f"Not a host name or address: {host or '(empty)'}")
    return host


def path_key(hops):
    """The part of the path that says which way traffic leaves: the first three hops after the router."""
    return ">".join([h["ip"] for h in hops if h.get("ip") and h.get("hop", 0) > 1][:3])


class SiteWatch:
    def __init__(self, data_dir):
        self.path = os.path.join(data_dir, "sites.db")
        self.cpath = os.path.join(data_dir, "sites.json")
        self.spath = os.path.join(data_dir, "sites_cfg.json")
        self.wake = threading.Event()   # "check now"
        self.last_at = self.next_at = None
        self.trace_at = 0
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.thread = None
        self.last = {}          # host -> latest result
        self.running = False
        self.searching = threading.Semaphore(3)
        with self._db() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS s (host TEXT, ts INTEGER, ip TEXT, avg REAL, min REAL, loss REAL, path TEXT, hops TEXT);
                CREATE INDEX IF NOT EXISTS s_i ON s (host, ts);
                CREATE TABLE IF NOT EXISTS ev (host TEXT, ts INTEGER, kind TEXT, text TEXT);
            """)

    def _db(self):
        return sqlite3.connect(self.path, timeout=30)

    def sites(self):
        try:
            with open(self.cpath) as f:
                rows = json.load(f)
            return rows if isinstance(rows, list) else DEFAULT
        except (OSError, ValueError):
            return DEFAULT

    def set_sites(self, rows):
        out, seen = [], set()
        for r in rows[:40]:
            name, host = str(r.get("name", "")).strip()[:40], clean_host(r.get("host", ""))
            if host in seen:
                raise ValueError(f"{host} is in the list twice")
            seen.add(host)
            out.append({"name": name or host, "host": host})
        tmp = self.cpath + ".tmp"
        with open(tmp, "w") as f:
            json.dump(out, f, indent=1)
        os.replace(tmp, self.cpath)
        return out

    def interval(self):
        try:
            with open(self.spath) as f:
                v = int(json.load(f).get("interval", INTERVAL))
            return v if 60 <= v <= 300 else INTERVAL
        except (OSError, ValueError, AttributeError, TypeError):
            return INTERVAL

    def set_interval(self, sec):
        try:
            sec = int(sec)
        except (TypeError, ValueError):
            raise ValueError("The interval is a number of seconds")
        if not 60 <= sec <= 300:
            raise ValueError("Check every 1 to 5 minutes")
        tmp = self.spath + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"interval": sec}, f)
        os.replace(tmp, self.spath)
        if self.last_at:
            self.next_at = min(self.next_at or 0, self.last_at + sec) or self.last_at + sec
        self.wake.set()             # the loop picks up the new interval
        return sec

    def check_now(self):
        """Run a full round (with traces) now; at most once every 30 s."""
        if self.running or (self.last_at and time.time() - self.last_at < 30):
            return False
        self.trace_at = 0
        self.next_at = time.time()
        self.wake.set()
        return True

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._loop, daemon=True, name="sites")
        self.thread.start()

    def _loop(self):
        self.next_at = time.time() + 30
        while not self.stop.is_set():
            left = self.next_at - time.time()
            if left > 0:
                self.wake.wait(left)
                self.wake.clear()
                continue                    # the interval may have changed: work out the time again
            try:
                self.check_all(trace=time.time() - self.trace_at >= TRACE_EVERY - 10)
            except Exception as exc:        # keep the loop alive
                print(f"major sites: {exc}", flush=True)
            self.next_at = (self.last_at or time.time()) + self.interval()

    def check_all(self, trace=True):
        self.running = True
        t = time.time()
        try:
            with ThreadPoolExecutor(6) as ex:
                res = list(ex.map(lambda s: self.check(s, trace=trace), self.sites()))
        finally:
            self.running = False
            self.last_at = t
        if trace:
            self.trace_at = t
        now = int(time.time())
        with self._db() as db:
            db.execute("DELETE FROM s WHERE ts < ?", (now - KEEP,))
            db.execute("DELETE FROM ev WHERE ts < ?", (now - 7 * 86400,))
        return res

    def check(self, site, save=True, trace=True):
        host, now = site["host"], int(time.time())
        out = {"name": site["name"], "host": host, "ts": now, "ip": "", "avg": None, "min": None, "loss": None, "hops": [], "error": ""}
        try:
            out["ip"] = socket.getaddrinfo(host, None, socket.AF_INET)[0][4][0]
        except (OSError, IndexError) as exc:
            out["error"] = f"cannot resolve: {exc}"
        if out["ip"]:
            try:
                p = health.icmp_ping(out["ip"], count=5)
                out.update(avg=p["avg"], min=p["min"], loss=p["loss"])
            except OSError as exc:
                out["error"] = f"ping: {exc}"
            prev = self.last.get(host) or {}
            jumped = prev.get("avg") is not None and out["avg"] is not None and out["avg"] > prev["avg"] * 1.5 + 15
            if trace or jumped or not prev.get("hops") or prev.get("ip") != out["ip"]:
                try:
                    out["hops"] = tracepath(out["ip"])
                    out["traced"] = now
                except OSError as exc:
                    out["error"] = out["error"] or f"trace: {exc}"
            else:                       # between traces: the path from the last one
                out["hops"], out["traced"] = prev["hops"], prev.get("traced")
        if save:
            self.record(out)
        return out

    def lookup(self, host):
        """Check one site now, on request (a search): not saved unless it is on the list."""
        host = clean_host(host)
        known = next((s for s in self.sites() if s["host"] == host), None)
        if not self.searching.acquire(timeout=60):
            raise ValueError("Another site is being checked; try again in a moment")
        try:
            return self.check(known or {"name": host, "host": host}, save=False)
        finally:
            self.searching.release()

    def record(self, out):
        host, now = out["host"], out["ts"]
        key = path_key(out["hops"])
        with self._db() as db:
            prev = db.execute("SELECT path, avg FROM s WHERE host=? ORDER BY ts DESC LIMIT 1", (host,)).fetchone()
            base = [r[0] for r in db.execute("SELECT avg FROM s WHERE host=? AND ts>? AND avg IS NOT NULL", (host, now - 86400))]
            if prev and prev[0] and key and prev[0] != key:
                db.execute("INSERT INTO ev VALUES (?,?,?,?)", (host, now, "path", f"path changed: {prev[0]} → {key}"))
            normal = sorted(base)[len(base) // 2] if len(base) >= 6 else None
            if normal is not None and out["avg"] is not None and out["avg"] > normal * 1.5 + 15:
                if not (prev and prev[1] is not None and prev[1] > normal * 1.5 + 15):
                    db.execute("INSERT INTO ev VALUES (?,?,?,?)", (host, now, "latency", f"delay up: {out['avg']:.0f} ms, normally {normal:.0f} ms"))
            db.execute("INSERT INTO s VALUES (?,?,?,?,?,?,?,?)", (host, now, out["ip"], out["avg"], out["min"], out["loss"], key, json.dumps(out["hops"])))
        with self.lock:
            self.last[host] = out

    def report(self):
        now = int(time.time())
        rows = []
        with self._db() as db:
            for site in self.sites():
                h = site["host"]
                ss = db.execute("SELECT ts, ip, avg, min, loss, path, hops FROM s WHERE host=? AND ts>? ORDER BY ts", (h, now - 86400)).fetchall()
                ev = db.execute("SELECT ts, kind, text FROM ev WHERE host=? ORDER BY ts DESC LIMIT 20", (h,)).fetchall()
                last = ss[-1] if ss else None
                avgs = sorted(x[2] for x in ss if x[2] is not None)
                rows.append({"name": site["name"], "host": h, "ip": last[1] if last else "", "ts": last[0] if last else None,
                             "avg": last[2] if last else None, "min": last[3] if last else None, "loss": last[4] if last else None,
                             "normal": avgs[len(avgs) // 2] if len(avgs) >= 6 else None, "path": last[5] if last else "",
                             "hops": json.loads(last[6]) if last and last[6] else [], "series": [[x[0], x[2], x[4]] for x in ss],
                             "events": [{"ts": e[0], "kind": e[1], "text": e[2]} for e in ev],
                             "error": (self.last.get(h) or {}).get("error", ""), "traced": (self.last.get(h) or {}).get("traced")})
        return {"sites": rows, "interval": self.interval(), "trace_every": TRACE_EVERY, "running": self.running, "now": now,
                "last_at": self.last_at and int(self.last_at), "next_at": self.next_at and int(self.next_at), "trace_at": self.trace_at and int(self.trace_at) or None}


def demo_lookup(host):
    """A made-up result for a site searched in demo mode."""
    import random
    host = clean_host(host)
    ms = 4 + (sum(map(ord, host)) % 7) * 30 + random.uniform(0, 3)
    hops = [{"hop": 1, "ip": "102.216.116.1", "ms": 0.6}, {"hop": 2, "ip": "172.16.0.0", "ms": 1.4}, {"hop": 3, "ip": "41.215.0.1", "ms": 2.9},
            {"hop": 4, "ip": "198.51.100.%d" % (len(host) % 200), "ms": round(ms * 0.9, 1)}]
    return {"name": host, "host": host, "ts": int(time.time()), "ip": "203.0.113.%d" % (len(host) % 250), "avg": round(ms, 1), "min": round(ms * 0.95, 1),
            "loss": 0, "hops": hops, "error": ""}


def demo_report(sites):
    """Made-up results for demo mode, through the demo router's upstreams."""
    import random
    now = int(time.time())
    rows = []
    for i, s in enumerate(sites):
        local = s["host"].endswith(".ke")
        base = 3 + i % 3 if local else [18, 21, 160, 19, 162, 24, 158, 155, 170, 22][i % 10]
        if s["name"] == "Safaricom":     # a Kenyan site reached through Europe: shows the warning
            base = 160
        series = [[now - k * 300, round(base * random.uniform(0.95, 1.15) + (40 if s["name"] == "Facebook" and k < 4 else 0), 1), 0] for k in range(288, -1, -1)]
        up = ["10.10.1.2", "102.68.76.194"] if i % 2 == 0 else ["172.16.0.0", "41.215.0.1"]
        hops = [{"hop": 1, "ip": "102.216.116.1", "ms": 0.6}, {"hop": 2, "ip": up[0], "ms": 1.4}, {"hop": 3, "ip": up[1], "ms": 2.9},
                {"hop": 4, "ip": "196.223.21.%d" % (10 + i), "ms": round(base * 0.8, 1)}]
        ev = [{"ts": now - 900, "kind": "latency", "text": f"delay up: {series[-1][1]:.0f} ms, normally {base} ms"}] if s["name"] == "Facebook" else []
        if s["name"] == "TikTok":
            ev.append({"ts": now - 5400, "kind": "path", "text": "path changed: 10.10.1.2>102.68.76.194 → 172.16.0.0>41.215.0.1"})
        rows.append({"name": s["name"], "host": s["host"], "ip": "203.0.113.%d" % (10 + i), "ts": now, "avg": series[-1][1], "min": round(series[-1][1] * 0.9, 1),
                     "loss": 0 if s["name"] != "X (Twitter)" else 20, "normal": base, "path": path_key(hops), "hops": hops, "series": series, "events": ev, "error": "", "traced": now - 40})
    return {"sites": rows, "interval": INTERVAL, "trace_every": TRACE_EVERY, "running": False, "demo": True, "now": now,
            "last_at": now - 40, "next_at": now + INTERVAL - 40, "trace_at": now - 40}
