"""Change tracker: what changed on the router, and when.

Keeps, per router, the last known state of a few tables and logs every change to them in changes.db:

- arp     one row per IP: new, gone, or MAC changed (a moved host, an IP clash or spoofing)
- mac     one row per MAC address (VLAN / bridge domain): new, gone, moved to another port
- route   direct and static routes, one row per prefix: added, removed, next hop changed.
          A full Internet table (about a million BGP routes) is never diffed route by route: it is
          counted per protocol (stats kind "routes") and a big jump or drop is logged instead.
- bgp, ospf, isis, ldp   one row per session: every time it goes down or comes back up,
          with the state, the reason when the router gives one, and how long it was down.
- peer    BGP routes received / active / advertised per peer, kept as numbers over time;
          a big drop or jump is logged (a peer filtering you, a route leak).
- policy  route-policy node match counters over time, to show what each rule is doing.

Two sources feed it, both through observe():
- a 24/7 SNMP poller (every minute) for every saved router with an SNMP login: ARP, BGP sessions
  and their route counts, OSPF / IS-IS / LDP sessions, the total route count;
- the app's own router session, whenever a page reads a view (ARP, BGP, ...) or the extra "track"
  view (per-protocol route counts, direct and static routes, MAC table, policy counters: these need
  the CLI, so they are checked while someone is signed in with SSH).
"""
import json
import sqlite3
import threading
import time

from snmp import SnmpClient, SnmpError, mac as fmt_mac, text

POLL = 60                     # seconds between SNMP polls
STAT_EVERY = 290              # store a number at most this often (about every 5 minutes)
KEEP_EVENTS = 180 * 86400
KEEP_STATS = 90 * 86400
GONE_AFTER = 3                # a table row must be missing this many checks in a row before it is "gone"
UP = {"Established", "Full", "Up", "Operational", "up", "full"}
SESSION_KINDS = ("bgp", "ospf", "isis", "ldp")
TABLE_KINDS = ("arp", "mac", "route")
RANGES = {"hour": 3600, "day": 86400, "week": 7 * 86400, "month": 30 * 86400, "quarter": 90 * 86400}

# SNMP
IFX_NAME = "1.3.6.1.2.1.31.1.1.1.1"
ARP_MAC = "1.3.6.1.2.1.4.22.1.2"
HW_BGP_PEER = "1.3.6.1.4.1.2011.5.25.177.1.1.2.1"    # .2 remote AS, .5 state
HW_BGP_ROUTES = "1.3.6.1.4.1.2011.5.25.177.1.1.3.1"  # .1 received, .2 active, .3 advertised
BGP4_PEER = "1.3.6.1.2.1.15.3.1"                     # .2 state, .14 last error (code, subcode)
OSPF_NBR_STATE = "1.3.6.1.2.1.14.10.1.6"
ISIS_ADJ_STATE = "1.3.6.1.2.1.138.1.6.1.1.2"
LDP_SESSION_STATE = "1.3.6.1.2.1.10.166.4.1.3.3.1.1"
ROUTE_NUMBER = ("1.3.6.1.2.1.4.24.6.0", "1.3.6.1.2.1.4.24.3.0")   # inetCidrRouteNumber, ipCidrRouteNumber
BGP_STATES = {1: "Idle", 2: "Connect", 3: "Active", 4: "OpenSent", 5: "OpenConfirm", 6: "Established"}
OSPF_STATES = {1: "Down", 2: "Attempt", 3: "Init", 4: "2-Way", 5: "ExStart", 6: "Exchange", 7: "Loading", 8: "Full"}
ISIS_STATES = {1: "Down", 2: "Init", 3: "Up", 4: "Failed"}
LDP_STATES = {1: "Nonexistent", 2: "Initialized", 3: "OpenRec", 4: "OpenSent", 5: "Operational"}
BGP_ERRORS = {1: "message header error", 2: "OPEN message error", 3: "UPDATE message error", 4: "hold timer expired",
              5: "state machine error", 6: "cease"}
CEASE = {1: "maximum prefixes reached", 2: "administrative shutdown", 3: "peer de-configured", 4: "administrative reset",
         5: "connection rejected", 6: "configuration change", 7: "connection collision", 8: "out of resources"}


def bgp_error(raw):
    """BGP4-MIB bgpPeerLastError (2 bytes: code, subcode) -> words, '' when none."""
    if isinstance(raw, (bytes, bytearray)) and len(raw) >= 2 and raw[0]:
        code, sub = raw[0], raw[1]
        why = BGP_ERRORS.get(code, f"error {code}")
        if code == 6 and sub in CEASE:
            why += f" ({CEASE[sub]})"
        return why
    return ""


def ip_from_index(idx):
    """Huawei BGP table index <inst>.<afi>.<safi>.<type>.<len>.<address bytes> -> the peer's address."""
    parts = str(idx).split(".")
    try:
        n = int(parts[4])
        b = [int(x) for x in parts[5:5 + n]]
    except (IndexError, ValueError):
        return str(idx)
    if n == 4 and len(b) == 4:
        return ".".join(map(str, b))
    if n == 16 and len(b) == 16:
        import ipaddress
        return str(ipaddress.IPv6Address(bytes(b)))
    return str(idx)


def dur(sec):
    sec = int(sec or 0)
    if sec < 90:
        return f"{sec} s"
    if sec < 5400:
        return f"{sec // 60} min"
    if sec < 172800:
        return f"{sec // 3600} h {sec // 60 % 60} min"
    return f"{sec // 86400} days {sec // 3600 % 24} h"


# ---------------------------------------------------------------------------------------------------------------
# turning views into tracked rows: {key: (value, info)}
def rows_arp(view):
    out = {}
    for e in (view or {}).get("entries", []):
        m = (e.get("mac") or "").lower()
        if not e.get("ip") or not m or m.startswith("incomplete") or (e.get("type") or "").startswith("I"):
            continue      # the router's own interface addresses are not hosts
        out[e["ip"]] = (m, e.get("interface") or "")
    return out


def rows_mac(entries):
    out = {}
    for e in entries or []:
        if e.get("mac"):
            out[f"{e['mac'].lower()} vlan {e.get('vlan') or '-'}"] = (e.get("port") or "", e.get("type") or "")
    return out


def rows_routes(routes):
    out = {}
    for r in routes or []:
        k = r.get("destination")
        if k:
            nh = " ".join(x for x in (r.get("next_hop"), r.get("interface")) if x)
            if k in out:                              # equal-cost paths: keep them all in one value
                nh = " | ".join(sorted(set(out[k][0].split(" | ") + [nh])))
            out[k] = (nh, r.get("protocol") or "")
    return out


def rows_sessions(peers, key, state="state"):
    return {key(p): (p.get(state) or "", p.get("why") or "") for p in peers or [] if key(p)}


class Tracker:
    def __init__(self, data_dir, saved):
        import os
        self.path = os.path.join(data_dir, "changes.db")
        self.saved = saved
        self.lock = threading.Lock()
        self.clients = {}
        self.status = {}              # router id -> {"time", "error"}
        self.stop = threading.Event()
        self.thread = None
        with self._db() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS state (rid TEXT, kind TEXT, key TEXT, value TEXT, info TEXT, since INTEGER,
                                                  miss INTEGER DEFAULT 0, down_at INTEGER, PRIMARY KEY (rid, kind, key));
                CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, rid TEXT, ts INTEGER, kind TEXT,
                                                   key TEXT, change TEXT, old TEXT, new TEXT, info TEXT);
                CREATE INDEX IF NOT EXISTS events_i ON events (rid, ts);
                CREATE TABLE IF NOT EXISTS stats (rid TEXT, kind TEXT, key TEXT, ts INTEGER, a REAL, b REAL, c REAL);
                CREATE INDEX IF NOT EXISTS stats_i ON stats (rid, kind, key, ts);
                CREATE TABLE IF NOT EXISTS seen (rid TEXT, kind TEXT, ts INTEGER, PRIMARY KEY (rid, kind));
            """)

    def _db(self):
        return sqlite3.connect(self.path, timeout=30)

    # ---- recording ------------------------------------------------------------------------------------------
    def observe(self, rid, kind, rows, now=None):
        """rows: the whole current table of this kind, {key: (value, info)}. Logs the differences."""
        if not rid:
            return []
        now = int(now or time.time())
        events = []
        with self.lock, self._db() as db:
            old = {k: (v, i, s, m, d) for k, v, i, s, m, d in
                   db.execute("SELECT key, value, info, since, miss, down_at FROM state WHERE rid=? AND kind=?", (rid, kind))}
            first = db.execute("SELECT 1 FROM seen WHERE rid=? AND kind=?", (rid, kind)).fetchone() is None
            if not first and not rows and len(old) > 3:
                return []            # an empty answer where there was a table: a failed read, not a wipe-out
            db.execute("INSERT OR REPLACE INTO seen VALUES (?,?,?)", (rid, kind, now))
            session = kind in SESSION_KINDS
            for k, (value, info) in rows.items():
                value, info = str(value), str(info or "")
                if k not in old:
                    db.execute("INSERT INTO state VALUES (?,?,?,?,?,?,0,?)", (rid, kind, k, value, info, now,
                                                                             None if (not session or value in UP) else now))
                    if not first:
                        events.append((kind, k, "added" if not session else ("up" if value in UP else "down"), "", value,
                                       info if not session else ("new session" + (f", {info}" if info else ""))))
                    continue
                ov, oi, since, miss, down_at = old[k]
                if session:
                    was_up, is_up = ov in UP, value in UP
                    if was_up and not is_up:
                        events.append((kind, k, "down", ov, value, info or ""))
                        down_at = now
                    elif is_up and not was_up:
                        events.append((kind, k, "up", ov, value, f"was down {dur(now - down_at)}" if down_at else ""))
                        down_at = None
                    elif miss >= GONE_AFTER:          # it had been reported gone and is back
                        events.append((kind, k, "back", "", value, ""))
                    if value != ov:
                        since = now
                elif value != ov or miss >= GONE_AFTER:
                    if miss >= GONE_AFTER:
                        events.append((kind, k, "back", "", value, info))
                    else:
                        events.append((kind, k, "changed", ov, value, info if info == oi else f"{oi} → {info}" if oi and info else info))
                    since = now
                db.execute("UPDATE state SET value=?, info=?, since=?, miss=0, down_at=? WHERE rid=? AND kind=? AND key=?",
                           (value, info, since, down_at, rid, kind, k))
            for k, (ov, oi, since, miss, down_at) in old.items():
                if k in rows:
                    continue
                miss += 1
                if miss == GONE_AFTER:
                    events.append((kind, k, "removed", ov, "", oi))
                db.execute("UPDATE state SET miss=? WHERE rid=? AND kind=? AND key=?", (miss, rid, kind, k))
                if miss > GONE_AFTER + 1440:          # gone for a day of checks: forget it
                    db.execute("DELETE FROM state WHERE rid=? AND kind=? AND key=?", (rid, kind, k))
            db.executemany("INSERT INTO events (rid, ts, kind, key, change, old, new, info) VALUES (?,?,?,?,?,?,?,?)",
                           [(rid, now) + e for e in events])
        return events

    def stat(self, rid, kind, key, a, b=None, c=None, now=None, label=None):
        """Store a number (or three) over time. Logs a big jump or drop between two samples."""
        if not rid or a is None:
            return
        now = int(now or time.time())
        with self.lock, self._db() as db:
            last = db.execute("SELECT ts, a, b, c FROM stats WHERE rid=? AND kind=? AND key=? ORDER BY ts DESC LIMIT 1",
                              (rid, kind, key)).fetchone()
            if last and now - last[0] < STAT_EVERY:
                return
            db.execute("INSERT INTO stats VALUES (?,?,?,?,?,?,?)", (rid, kind, key, now, a, b, c))
            if last and kind in ("peer", "routes"):
                note = jump(last[1], a, *((20000, 0.05) if kind == "routes" else (100, 0.2)))
                if note:
                    db.execute("INSERT INTO events (rid, ts, kind, key, change, old, new, info) VALUES (?,?,?,?,?,?,?,?)",
                               (rid, now, "peer" if kind == "peer" else "routes", label or key, note,
                                f"{int(last[1]):,}", f"{int(a):,}", "routes received" if kind == "peer" else "routes in table"))

    def mark_down_reason(self, rid, kind, key, why):
        """Add the router's reason to the latest 'down' event of a session (it may come from another read)."""
        with self.lock, self._db() as db:
            db.execute("UPDATE events SET info=? WHERE id=(SELECT id FROM events WHERE rid=? AND kind=? AND key=? AND change='down' "
                       "ORDER BY ts DESC LIMIT 1) AND (info='' OR info IS NULL)", (why, rid, kind, key))

    # ---- reading --------------------------------------------------------------------------------------------
    def events(self, rid, kinds=None, since=0, q="", limit=500, key=""):
        sql, args = "SELECT ts, kind, key, change, old, new, info FROM events WHERE rid=? AND ts>=?", [rid, int(since or 0)]
        if key:
            sql += " AND key=?"
            args.append(key)
        if kinds:
            sql += f" AND kind IN ({','.join('?' * len(kinds))})"
            args += list(kinds)
        if q:
            sql += " AND (key LIKE ? OR old LIKE ? OR new LIKE ? OR info LIKE ?)"
            args += [f"%{q}%"] * 4
        sql += " ORDER BY ts DESC, id DESC LIMIT ?"
        args.append(int(limit))
        with self._db() as db:
            return [dict(zip(("ts", "kind", "key", "change", "old", "new", "info"), r)) for r in db.execute(sql, args)]

    def counts(self, rid, since):
        with self._db() as db:
            return {k: n for k, n in db.execute("SELECT kind, COUNT(*) FROM events WHERE rid=? AND ts>=? GROUP BY kind", (rid, since))}

    def series(self, rid, kind, key, rng="day"):
        since = int(time.time()) - RANGES.get(rng, 86400)
        with self._db() as db:
            return [list(r) for r in db.execute("SELECT ts, a, b, c FROM stats WHERE rid=? AND kind=? AND key=? AND ts>=? ORDER BY ts",
                                                 (rid, kind, key, since))]

    def spark(self, rid, kind, rng="day", points=48):
        """{key: [[ts, a], ...]}: every key's first number, thinned to about `points` per key."""
        since = int(time.time()) - RANGES.get(rng, 86400)
        out = {}
        with self._db() as db:
            for k, ts, a in db.execute("SELECT key, ts, a FROM stats WHERE rid=? AND kind=? AND ts>=? ORDER BY ts", (rid, kind, since)):
                out.setdefault(k, []).append([ts, a])
        for k, v in out.items():
            if len(v) > points:
                step = len(v) / points
                out[k] = [v[int(i * step)] for i in range(points)] + [v[-1]]
        return out

    def latest(self, rid, kind, ago=0):
        """{key: (ts, a, b, c)}: the newest sample of every key, or the newest one at least `ago` seconds old."""
        cut = int(time.time()) - ago
        with self._db() as db:
            rows = db.execute("SELECT key, ts, a, b, c FROM stats s WHERE rid=? AND kind=? AND ts=(SELECT MAX(ts) FROM stats "
                              "WHERE rid=s.rid AND kind=s.kind AND key=s.key AND ts<=?)", (rid, kind, cut))
            return {k: (ts, a, b, c) for k, ts, a, b, c in rows}

    def tracked(self, rid):
        with self._db() as db:
            return {k: (n, ts) for k, n, ts in db.execute(
                "SELECT s.kind, (SELECT COUNT(*) FROM state WHERE rid=s.rid AND kind=s.kind AND miss<?), s.ts FROM seen s WHERE rid=?",
                (GONE_AFTER, rid))}

    def prune(self, now=None):
        now = now or time.time()
        with self.lock, self._db() as db:
            db.execute("DELETE FROM events WHERE ts<?", (now - KEEP_EVENTS,))
            db.execute("DELETE FROM stats WHERE ts<?", (now - KEEP_STATS,))

    # ---- 24/7 SNMP poller -----------------------------------------------------------------------------------
    def polled(self, rid):
        st = self.status.get(rid)
        return bool(st and not st.get("error") and time.time() - st["time"] < 3 * POLL)

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._loop, daemon=True, name="changes")
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
                except Exception as exc:
                    self.status[r["id"]] = {"time": t, "error": str(exc)[:200]}
            if t - last_prune > 3600:
                self.prune(t)
                last_prune = t
            self.stop.wait(max(5, POLL - (time.time() - t)))

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
            self.status.pop(rid, None)
            return
        t = time.time()
        try:
            snap = snmp_snapshot(c)
        except SnmpError as exc:
            self.status[rid] = {"time": t, "error": str(exc)[:200]}
            return
        self.record(rid, snap, now=t)
        self.status[rid] = {"time": t, "error": ""}

    def record(self, rid, snap, now=None):
        """snap: {"arp": rows, "bgp": [peer dicts], "ospf": rows, "isis": rows, "ldp": rows, "route_total": n}"""
        if "arp" in snap:
            self.observe(rid, "arp", snap["arp"], now)
        if "bgp" in snap:
            peers = snap["bgp"]
            self.observe(rid, "bgp", {p["peer"]: (p["state"], p.get("why") or admin_why(p["state"])) for p in peers}, now)
            for p in peers:
                if p.get("why") and p["state"] not in UP:
                    self.mark_down_reason(rid, "bgp", p["peer"], p["why"])
                self.stat(rid, "peer", p["peer"], p.get("prefixes"), p.get("active"), p.get("advertised"), now,
                          label=p["peer"] + (f" (AS{p['as']})" if p.get("as") else ""))
        for kind in ("ospf", "isis", "ldp"):
            if kind in snap:
                self.observe(rid, kind, snap[kind], now)
        if snap.get("route_total") is not None:
            self.stat(rid, "routes", "Total", snap["route_total"], now=now)


    def feed_view(self, rid, name, value):
        """A fresh view from the app's own router session. Tables the 24/7 poller already watches are
        left to it (two readers of one table would disagree on details and log noise)."""
        if not rid or not isinstance(value, dict):
            return
        polled = self.polled(rid)
        if name == "track":
            stats = value.get("route_stats")
            if stats:
                self.stat(rid, "routes", "Total", stats["total"])
                for proto, n in stats["protocols"].items():
                    if n or proto in ("DIRECT", "STATIC", "BGP"):
                        self.stat(rid, "routes", proto, n)
            if "routes" in value and (value["routes"] or stats):
                self.observe(rid, "route", rows_routes(value["routes"]))
            if "mac" in value:
                self.observe(rid, "mac", rows_mac(value["mac"]))
            peers = value.get("bgp") or []
            if peers and not polled:
                self.record(rid, {"bgp": peers})
            for p in peers:
                if polled:
                    self.stat(rid, "peer", p["peer"], p.get("prefixes"), p.get("active"), p.get("advertised"),
                              label=p["peer"] + (f" (AS{p['as']})" if p.get("as") else ""))
                if p.get("why") and p["state"] != "Established":
                    self.mark_down_reason(rid, "bgp", p["peer"], p["why"])
            for pol in value.get("policies") or []:
                for n in pol["nodes"]:
                    if n["matched"] is not None:
                        self.stat(rid, "policy", f"{pol['name']} {n['node']}", n["matched"])
            return
        if polled and name in ("arp", "bgp", "ospf", "isis", "mpls"):
            return
        snap = session_rows(name, value)
        if "bgp" in snap:
            self.observe(rid, "bgp", {p["peer"]: (p["state"], p["why"]) for p in snap["bgp"]})
            for p in snap["bgp"]:
                if "active" not in p:     # the CLI summary has no active/advertised: the "track" read stores those
                    continue
                self.stat(rid, "peer", p["peer"], p.get("prefixes"), p.get("active"), p.get("advertised"),
                          label=p["peer"] + (f" (AS{p['as']})" if p.get("as") else ""))
        for kind in ("arp", "ospf", "isis", "ldp"):
            if kind in snap and (snap[kind] or kind == "arp"):
                self.observe(rid, kind, snap[kind])


def jump(old, new, floor, pct=0.2):
    """A change worth logging between two samples of a count."""
    if old is None or new is None:
        return ""
    d = new - old
    if abs(d) < floor:
        return ""
    if old and d < 0 and -d / old >= pct:
        return "dropped"
    if d > 0 and (not old or d / old >= pct):
        return "jumped"
    return ""


def snmp_snapshot(c):
    """One poll over SNMP. Tables that the router does not have come back empty and are left out."""
    snap = {}
    names = {i: text(v) for i, v in c.walk(IFX_NAME, 5000).items()}
    arp = {}
    for idx, m in c.walk(ARP_MAC, 50000).items():
        ifidx, _, ip = idx.partition(".")
        mm = fmt_mac(m)
        if ip and mm:
            arp[ip] = (mm.lower(), names.get(ifidx, ifidx))
    snap["arp"] = arp
    peers = []
    try:
        state = c.walk(HW_BGP_PEER + ".5", 2000)
    except SnmpError:
        state = {}
    if state:
        asn = c.walk(HW_BGP_PEER + ".2", 2000)
        rcv, act, adv = (c.walk(HW_BGP_ROUTES + col, 2000) for col in (".1", ".2", ".3"))
        for idx, st in state.items():
            peers.append({"peer": ip_from_index(idx), "as": str(asn.get(idx, "")), "state": BGP_STATES.get(st, str(st)),
                          "prefixes": rcv.get(idx), "active": act.get(idx), "advertised": adv.get(idx)})
    else:
        for ip, st in c.walk(BGP4_PEER + ".2", 2000).items():
            peers.append({"peer": ip, "state": BGP_STATES.get(st, str(st))})
    if peers:
        try:
            errs = c.walk(BGP4_PEER + ".14", 2000)
        except SnmpError:
            errs = {}
        for p in peers:
            p["why"] = bgp_error(errs.get(p["peer"]))
        snap["bgp"] = peers
    for kind, oid, states in (("ospf", OSPF_NBR_STATE, OSPF_STATES), ("isis", ISIS_ADJ_STATE, ISIS_STATES), ("ldp", LDP_SESSION_STATE, LDP_STATES)):
        try:
            rows = c.walk(oid, 2000)
        except SnmpError:
            continue
        if rows:
            snap[kind] = {(idx if kind != "ospf" else ".".join(idx.split(".")[:4])): (states.get(v, str(v)), "") for idx, v in rows.items()}
    for oid in ROUTE_NUMBER:
        try:
            n = c.get(oid).get(oid)
        except SnmpError:
            continue
        if isinstance(n, int):
            snap["route_total"] = n
            break
    return snap


def session_rows(name, view):
    """The app's own views -> tracked rows, for routers without the 24/7 poller (or demo)."""
    if name == "arp":
        return {"arp": rows_arp(view)}
    if name == "bgp":
        return {"bgp": [dict(p, why=admin_why(p["state"])) for p in view.get("peers", [])]}
    if name == "ospf":
        return {"ospf": rows_sessions(view.get("peers"), lambda p: p.get("neighbor") and f"{p['neighbor']} on {p.get('interface', '')}")}
    if name == "isis":
        return {"isis": rows_sessions(view.get("peers"), lambda p: p.get("system_id") and f"{p['system_id']} on {p.get('interface', '')}")}
    if name == "mpls":
        return {"ldp": rows_sessions(view.get("ldp"), lambda p: p.get("peer"))}
    return {}


def admin_why(state):
    return "shut down on the router (peer ignore / admin)" if "admin" in str(state).lower() else ""


def json_safe(v):
    return json.loads(json.dumps(v, default=str))
