"""Traffic per IP address and per subnet, from the router's NetStream (NetFlow v5 / v9 / IPFIX) export.

The router counts traffic per interface only, so the addresses on one port (a primary and its "sub"
addresses) share one set of counters. With NetStream on, the router samples packets and sends flow
records here (UDP, default port 2055). Each flow is credited to the subnet it belongs to:
  - to the subnet (download, shown as Tx like an interface facing a customer): destination address in
    the subnet, or the flow's next hop is in the subnet (a block routed to the customer behind it);
  - from the subnet (upload, Rx): source address in the subnet, or a source address seen before as a
    download destination behind that subnet.
Rates are stored every 5 minutes in the same history database as the interface graphs, under the
name "flow:<subnet>", so the day/week/month/year graphs work the same way. The last hour is kept in
memory per minute for the live graph, with the busiest addresses of each subnet.

Packets are accepted only from routers saved under Manager > Routers (their management address or any
address the router has, e.g. the loopback NetStream exports from). Everything else is dropped.
"""
import collections
import ipaddress
import socket
import struct
import threading
import time

from snmp import SnmpError, text

IFX = "1.3.6.1.2.1.31.1.1.1"
IPADDR = "1.3.6.1.2.1.4.20.1"
BUCKET = 60            # live graph resolution (seconds)
STORE_EVERY = 300      # history resolution, as the interface graphs
LEARN_MAX = 200000     # remembered addresses behind customer subnets


def _plen(mask):
    return sum(bin(int(x)).count("1") for x in mask.split(".")) if mask else 32


class FlowCollector:
    def __init__(self, saved, hist, default_sampling=1):
        self.saved, self.hist = saved, hist          # routers.RouterStore, history.History (its SNMP clients and database)
        self.default_sampling = max(1, int(default_sampling or 1))
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.sock = None
        self.port = None
        self.routers = {}       # rid -> {"exporters": set(ip), "subnets": [(network, ifname)], "time", "error"}
        self.exporter_rid = {}  # exporter ip -> rid
        self.templates = {}     # (exporter, domain, template id) -> (kind, fields, length or None)
        self.sampling = {}      # exporter ip -> 1:N learned from the export (options records or the v5 header)
        self.learned = collections.OrderedDict()   # (rid, address) -> subnet key, for blocks routed behind a subnet
        self.acc = {}           # (rid, subnet) -> [rx bytes, tx bytes] since the last 5-minute store
        self.live = {}          # (rid, subnet) -> deque of [minute, rx bytes, tx bytes]
        self.hosts = {}         # (rid, subnet) -> {address: [rx bytes, tx bytes]} for the current and previous 5 minutes
        self.hosts_prev = {}
        self.stats = {"packets": 0, "flows": 0, "dropped": 0, "unknown_template": 0, "unmatched": 0, "last": None, "errors": 0,
                      "exporters": {}}
        self.last_store = time.time()

    # ---- running ---------------------------------------------------------------
    def start(self, port=2055, bind="0.0.0.0"):
        self.port = port
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
        self.sock.bind((bind, port))
        self.sock.settimeout(1.0)
        threading.Thread(target=self._recv_loop, daemon=True, name="netflow").start()
        threading.Thread(target=self._house_loop, daemon=True, name="netflow-house").start()

    def _recv_loop(self):
        while not self.stop.is_set():
            try:
                data, (src, _) = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                if self.stop.is_set():
                    return
                continue
            try:
                self.handle(src, data, time.time())
            except Exception:
                self.stats["errors"] += 1

    def _house_loop(self):
        last_refresh = 0
        while not self.stop.is_set():
            t = time.time()
            if t - last_refresh > 300:
                for r in self.saved.list():
                    try:
                        self.refresh(r["id"])
                    except Exception as exc:
                        self.routers.setdefault(r["id"], {"exporters": set(), "subnets": []}).update(time=t, error=str(exc)[:200])
                last_refresh = t
            if t - self.last_store >= STORE_EVERY:
                self.store(t)
            self.stop.wait(10)

    def close(self):
        self.stop.set()
        if self.sock:
            self.sock.close()

    # ---- what the router has: its addresses (exporter allowlist) and subnets ----
    def refresh(self, rid):
        full = self.saved.get(rid) or {}
        c = self.hist._client(rid)
        if c is None:
            return
        try:
            names = c.walk(IFX + ".1", 5000)
            ifidx, masks = c.walk(IPADDR + ".2", 5000), c.walk(IPADDR + ".3", 5000)
        except SnmpError as exc:
            self.routers.setdefault(rid, {"exporters": set(), "subnets": []}).update(time=time.time(), error=str(exc)[:200])
            return
        self.set_router(rid, full.get("host", ""), [(ip, text(names.get(str(idx), names.get(idx, ""))) or str(idx), _plen(masks.get(ip)))
                                                    for ip, idx in ifidx.items()])

    def set_router(self, rid, host, addrs):
        """addrs: [(ip, ifname, prefix length)]. Loopbacks and host routes are not subnets."""
        exporters, subnets = {host} if host else set(), {}
        for ip, ifname, plen in addrs:
            exporters.add(ip)
            if plen >= 32 or ifname.lower().startswith(("loopback", "null")):
                continue
            try:
                n = ipaddress.ip_network(f"{ip}/{plen}", strict=False)
            except ValueError:
                continue
            subnets[str(n)] = (n, ifname)
        with self.lock:
            self.routers[rid] = {"exporters": exporters, "time": time.time(), "error": "",
                                 "subnets": sorted(((n, ifn) for n, ifn in subnets.values()), key=lambda x: -x[0].prefixlen)}
            self.exporter_rid = {e: r for r, v in self.routers.items() for e in v["exporters"]}

    def subnets(self, rid):
        return [{"subnet": str(n), "interface": ifn} for n, ifn in (self.routers.get(rid) or {}).get("subnets", [])]

    # ---- decoding --------------------------------------------------------------
    def handle(self, src, data, now):
        rid = self.exporter_rid.get(src)
        if rid is None:
            self.stats["dropped"] += 1
            return
        if len(data) < 4:
            return
        version = struct.unpack("!H", data[:2])[0]
        if version == 5:
            flows = self._v5(src, data)
        elif version == 9:
            flows = self._v9(src, data)
        elif version == 10:
            flows = self._ipfix(src, data)
        else:
            self.stats["dropped"] += 1
            return
        self.stats["packets"] += 1
        self.stats["last"] = now
        ex = self.stats["exporters"].setdefault(src, {"packets": 0, "flows": 0, "version": version, "rid": rid})
        ex["packets"] += 1
        ex["version"], ex["last"] = version, now
        for f in flows:
            self.account(rid, src, f, now)
            ex["flows"] += 1

    def _v5(self, src, data):
        count = struct.unpack("!H", data[2:4])[0]
        samp = struct.unpack("!H", data[22:24])[0] & 0x3FFF
        if samp:
            self.sampling[src] = samp
        out = []
        for i in range(count):
            o = 24 + i * 48
            if o + 48 > len(data):
                break
            r = data[o:o + 48]
            out.append({"src": socket.inet_ntoa(r[0:4]), "dst": socket.inet_ntoa(r[4:8]), "nh": socket.inet_ntoa(r[8:12]),
                        "bytes": struct.unpack("!I", r[20:24])[0]})
        return out

    def _v9(self, src, data):
        domain = struct.unpack("!I", data[16:20])[0]
        o, out = 20, []
        while o + 4 <= len(data):
            fsid, flen = struct.unpack("!HH", data[o:o + 4])
            if flen < 4:
                break
            body = data[o + 4:o + flen]
            if fsid == 0:
                p = 0
                while p + 4 <= len(body):
                    tid, cnt = struct.unpack("!HH", body[p:p + 4])
                    fields = [struct.unpack("!HH", body[p + 4 + i * 4:p + 8 + i * 4]) for i in range(cnt)]
                    if not tid:
                        break
                    self.templates[(src, domain, tid)] = ("data", [(t, ln, 0) for t, ln in fields])
                    p += 4 + cnt * 4
            elif fsid == 1:
                p = 0
                while p + 6 <= len(body):
                    tid, slen, olen = struct.unpack("!HHH", body[p:p + 6])
                    if not tid:
                        break
                    fields = [struct.unpack("!HH", body[p + 6 + i * 4:p + 10 + i * 4]) for i in range((slen + olen) // 4)]
                    self.templates[(src, domain, tid)] = ("options", [(t if i >= slen // 4 else -1, ln, 0) for i, (t, ln) in enumerate(fields)])
                    p += 6 + slen + olen
            elif fsid >= 256:
                out += self._records(src, domain, fsid, body)
            o += flen
        return out

    def _ipfix(self, src, data):
        domain = struct.unpack("!I", data[12:16])[0]
        o, out = 16, []
        end = min(len(data), struct.unpack("!H", data[2:4])[0])
        while o + 4 <= end:
            sid, slen = struct.unpack("!HH", data[o:o + 4])
            if slen < 4:
                break
            body = data[o + 4:o + slen]
            if sid in (2, 3):
                p = 0
                while p + 4 <= len(body):
                    tid, cnt = struct.unpack("!HH", body[p:p + 4])
                    p += 4
                    if not tid:
                        break
                    scope = 0
                    if sid == 3:
                        scope = struct.unpack("!H", body[p:p + 2])[0]
                        p += 2
                    fields = []
                    for i in range(cnt):
                        t, ln = struct.unpack("!HH", body[p:p + 4])
                        p += 4
                        ent = 0
                        if t & 0x8000:
                            ent = struct.unpack("!I", body[p:p + 4])[0]
                            p += 4
                            t &= 0x7FFF
                        fields.append((-1 if i < scope else t, ln, ent))
                    self.templates[(src, domain, tid)] = ("options" if sid == 3 else "data", fields)
            elif sid >= 256:
                out += self._records(src, domain, sid, body)
            o += slen
        return out

    def _records(self, src, domain, tid, body):
        tpl = self.templates.get((src, domain, tid))
        if not tpl:
            self.stats["unknown_template"] += 1
            return []
        kind, fields = tpl
        fixed = all(ln != 0xFFFF for _, ln, _ in fields)
        size = sum(ln for _, ln, _ in fields) if fixed else None
        out, p = [], 0
        while p < len(body):
            if fixed and (size == 0 or p + size > len(body)):
                break
            rec = {}
            try:
                for t, ln, ent in fields:
                    if ln == 0xFFFF:
                        ln = body[p]
                        p += 1
                        if ln == 255:
                            ln = struct.unpack("!H", body[p:p + 2])[0]
                            p += 2
                    v = body[p:p + ln]
                    p += ln
                    if len(v) < ln:
                        raise IndexError
                    if ent or t < 0:
                        continue
                    rec[t] = v
            except (IndexError, struct.error):
                break
            if kind == "options":
                self._options(src, rec)
            else:
                f = self._flow(src, rec)
                if f:
                    out.append(f)
        return out

    @staticmethod
    def _int(v):
        return int.from_bytes(v, "big") if v else 0

    def _options(self, src, rec):
        """Sampler options: interval (34, 50, 305) and IPFIX packet space (306)."""
        for t in (34, 50, 305):
            if t in rec and self._int(rec[t]):
                n = self._int(rec[t])
                if t == 305 and 306 in rec:
                    n = (n + self._int(rec[306])) / n      # e.g. 1 sampled, 999 skipped -> 1:1000
                self.sampling[src] = max(1, n)
                return

    def _flow(self, src, rec):
        def addr(k4, k6):
            if k4 in rec and len(rec[k4]) == 4:
                return socket.inet_ntoa(rec[k4])
            if k6 in rec and len(rec[k6]) == 16:
                return str(ipaddress.IPv6Address(rec[k6]))
            return ""
        b = self._int(rec.get(1)) or self._int(rec.get(23)) or self._int(rec.get(85))
        if not b:
            return None
        if 34 in rec and self._int(rec[34]):
            self.sampling[src] = self._int(rec[34])
        return {"src": addr(8, 27), "dst": addr(12, 28), "nh": addr(15, 62), "bytes": b}

    # ---- accounting ------------------------------------------------------------
    def _subnet_of(self, rid, ip):
        if not ip:
            return None
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            return None
        for n, _ in (self.routers.get(rid) or {}).get("subnets", ()):    # longest prefix first
            if a.version == n.version and a in n:
                return str(n)
        return None

    def account(self, rid, src, f, now):
        mult = self.sampling.get(src) or self.default_sampling
        b = f["bytes"] * mult
        down = self._subnet_of(rid, f["dst"]) or self._subnet_of(rid, f["nh"])
        if down and not self._subnet_of(rid, f["dst"]):
            self._learn(rid, f["dst"], down)          # a block routed behind this subnet
        up = self._subnet_of(rid, f["src"]) or self.learned.get((rid, f["src"]))
        if not down and not up:
            self.stats["unmatched"] += 1
            return
        self.stats["flows"] += 1
        minute = int(now - now % BUCKET)
        with self.lock:
            for key, rx, tx, host in ((up, b, 0, f["src"]), (down, 0, b, f["dst"])):
                if not key:
                    continue
                a = self.acc.setdefault((rid, key), [0, 0])
                a[0] += rx
                a[1] += tx
                d = self.live.setdefault((rid, key), collections.deque(maxlen=61))
                if not d or d[-1][0] != minute:
                    d.append([minute, 0, 0])
                d[-1][1] += rx
                d[-1][2] += tx
                h = self.hosts.setdefault((rid, key), {}).setdefault(host, [0, 0])
                h[0] += rx
                h[1] += tx

    def _learn(self, rid, ip, key):
        if not ip:
            return
        self.learned[(rid, ip)] = key
        self.learned.move_to_end((rid, ip))
        while len(self.learned) > LEARN_MAX:
            self.learned.popitem(last=False)

    def store(self, now):
        """Every 5 minutes: average rates into the history database (rx = from the subnet, tx = to it)."""
        with self.lock:
            acc, self.acc = self.acc, {}
            self.hosts_prev, self.hosts = self.hosts, {}
            dt = max(1.0, now - self.last_store)
            self.last_store = now
        by_rid = {}
        for (rid, key), (rx, tx) in acc.items():
            by_rid.setdefault(rid, []).append(("flow:" + key, rx * 8 / dt, tx * 8 / dt))
        for rid, rows in by_rid.items():
            self.hist.store(rid, int(now), rows)

    # ---- queries ---------------------------------------------------------------
    def live_series(self, rid, key):
        """[[t, rx bps, tx bps], ...] per minute for the last hour; the current minute is left out until done."""
        now = time.time()
        with self.lock:
            d = list(self.live.get((rid, key), ()))
        return [[t, rx * 8 / BUCKET, tx * 8 / BUCKET] for t, rx, tx in d if t + BUCKET <= now]

    def top_hosts(self, rid, key, n=15):
        with self.lock:
            cur = {k: list(v) for k, v in (self.hosts.get((rid, key)) or {}).items()}
            prev = self.hosts_prev.get((rid, key)) or {}
        for k, v in prev.items():
            c = cur.setdefault(k, [0, 0])
            c[0] += v[0]
            c[1] += v[1]
        rows = sorted(cur.items(), key=lambda kv: -(kv[1][0] + kv[1][1]))[:n]
        return [{"address": k, "rx_bytes": round(v[0]), "tx_bytes": round(v[1])} for k, v in rows]

    def status(self, rid=None):
        ex = {k: dict(v, sampling=self.sampling.get(k) or self.default_sampling) for k, v in self.stats["exporters"].items()
              if rid is None or v.get("rid") == rid}
        r = self.routers.get(rid) or {} if rid else {}
        return {"running": bool(self.sock), "port": self.port, "packets": self.stats["packets"], "flows": self.stats["flows"],
                "dropped": self.stats["dropped"], "unmatched": self.stats["unmatched"], "unknown_template": self.stats["unknown_template"],
                "last": self.stats["last"], "exporters": ex, "subnets": len(r.get("subnets", [])), "error": r.get("error", ""),
                "default_sampling": self.default_sampling}
