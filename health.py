"""Smart health check: finds what makes customers slow before they call.

A background thread checks every saved router that has an SNMP login every 5 minutes and
learns each link's normal levels (a running average and spread per interface, optic and
latency target), then reports what is wrong or unusual:
  - input errors (CRC), the classic sign of a dirty or damaged fibre, a bad SFP or a cable fault
  - packet drops (discards) in and out: queues full, QoS limits hit
  - links close to full (the commonest cause of "slow internet" at peak)
  - SFP received / sent light outside the module's alarm levels, near them, or falling
  - links that went down or keep flapping
  - latency and packet loss, pinged from this server to the router and to customer CPEs
  - router CPU, memory and temperature
Nothing here talks to the router's CLI or changes anything.
"""
import json
import math
import os
import socket
import sqlite3
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from snmp import SnmpError, text

INTERVAL = 300
IF = "1.3.6.1.2.1.2.2.1"            # ifTable: .7 admin, .8 oper, .9 last change, .13 in discards, .14 in errors, .19 out discards, .20 out errors
IFX = "1.3.6.1.2.1.31.1.1.1"        # ifXTable: .1 name, .6/.10 HC octets, .7/.11 HC unicast packets, .15 speed Mbit/s, .18 alias
ENT_NAME = "1.3.6.1.2.1.47.1.1.1.1.7"
HW_ENT = "1.3.6.1.4.1.2011.5.25.31.1.1.1.1"    # .5 CPU %, .7 memory %, .11 temperature C
HW_OPT = "1.3.6.1.4.1.2011.5.25.31.1.1.3.1"    # .8 rx, .9 tx (0.01 dBm), .13/.14 rx low/high, .15/.16 tx low/high alarm
SKIP = ("loopback", "inloopback", "null", "virtual-template", "virtual-ethernet", "nve", "tunnel")
ALPHA = 1 / 288                     # baselines remember about a day of 5-minute samples
LEARN = 24                          # samples (2 hours) before "unusual" is judged

ADVICE = {
    "errors": "Clean or replace the patch cord and connectors, check the SFP and the far-end port. On copper, check the cable and speed/duplex.",
    "out_drops": "The port is asked to send more than it can or than its QoS profile allows. Check its usage at peak and its QoS profile; drops at peak mean the customer is hitting their limit or the link is full.",
    "in_drops": "Traffic arrives faster than the router accepts it: inbound policing (QoS profile / CAR) or bursts. Check the inbound QoS profile.",
    "congestion": "Customers on this link get slow speeds at peak. Upgrade the link, move traffic to another one, or check the QoS profiles behind it.",
    "rx_low": "Clean the connectors, check patch cords, splices and bends, and check the far end's transmitter.",
    "rx_high": "Too much light can damage the receiver: add an attenuator.",
    "rx_fall": "Light that falls over time means a bend, a dirty or loose connector, or a failing transceiver at the far end.",
    "tx": "The module's own laser is outside its range: replace the SFP.",
    "down": "Check the far-end equipment, its power, the fibre/cable and the SFP on both sides.",
    "flap": "A link that keeps going down and up: usually a loose or dirty connector, a failing SFP, or power problems at the far end.",
    "latency": "Delay usually comes from a full link on the way (queueing). Check usage and drops on the interfaces towards this address.",
    "loss": "Packets lost on the way: check drops and errors on the interfaces towards this address, and the device itself.",
    "cpu": "High CPU slows the router's own work (routing protocols, management). Check what changed: a traffic storm, an attack, or a debug left on.",
    "memory": "Memory close to full can make the router drop sessions or routes.",
    "temp": "Check fans, filters and the room's cooling.",
}


def icmp_ping(host, count=5, timeout=1.0, gap=0.2):
    """Ping without root (an ICMP datagram socket). Returns sent, received, loss %, and rtt min/avg/max in ms."""
    try:
        s, raw = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP), False
    except PermissionError:     # ping_group_range does not include us: a raw socket works when running as root
        s, raw = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP), True
    s.settimeout(timeout)
    ident = os.getpid() & 0xFFFF
    rtts = []
    try:
        for seq in range(1, count + 1):
            payload = struct.pack("!d", time.time()) + b"ne8000-health"
            hdr = struct.pack("!BBHHH", 8, 0, 0, ident, seq)
            csum = _checksum(hdr + payload)
            s.sendto(struct.pack("!BBHHH", 8, 0, csum, ident, seq) + payload, (host, 0))
            t0, end = time.time(), time.time() + timeout
            while time.time() < end:
                try:
                    data = s.recv(1024)
                except socket.timeout:
                    break
                if raw:     # a raw socket hands over the IP header and every ICMP packet on the host
                    data = data[(data[0] & 0x0F) * 4:]
                    if len(data) < 8 or struct.unpack("!H", data[4:6])[0] != ident:
                        continue
                if len(data) >= 8 and data[0] == 0 and struct.unpack("!H", data[6:8])[0] == seq:
                    rtts.append((time.time() - t0) * 1000)
                    break
            time.sleep(gap)
    finally:
        s.close()
    got = len(rtts)
    return {"sent": count, "received": got, "loss": round(100 * (count - got) / count, 1),
            "min": round(min(rtts), 2) if rtts else None, "avg": round(sum(rtts) / got, 2) if rtts else None,
            "max": round(max(rtts), 2) if rtts else None}


def _checksum(b):
    if len(b) % 2:
        b += b"\0"
    s = sum(struct.unpack(f"!{len(b) // 2}H", b))
    s = (s >> 16) + (s & 0xFFFF)
    s += s >> 16
    return ~s & 0xFFFF


class Health:
    def __init__(self, data_dir, saved, history):
        self.path = os.path.join(data_dir, "health.db")
        self.tpath = os.path.join(data_dir, "health_targets.json")
        self.saved, self.history = saved, history       # history owns the SNMP clients
        self.prev = {}          # (rid, ifname) -> previous counters
        self.findings = {}      # rid -> [finding]
        self.latency = {}       # rid -> [{ip, label, sent, received, loss, avg, ..., base}]
        self.status = {}        # rid -> {time, error, interfaces, optics, targets, ping}
        self.flaps = {}         # (rid, ifname) -> [times the link changed state]
        self.desc = {}          # rid -> {ifname: description}, for naming the customer behind an optic
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.thread = None
        with self._db() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS base (rid TEXT, key TEXT, metric TEXT, n INTEGER, mean REAL, var REAL, ts INTEGER,
                                                 PRIMARY KEY (rid, key, metric));
                CREATE TABLE IF NOT EXISTS seen (rid TEXT, fid TEXT, first INTEGER, last INTEGER, PRIMARY KEY (rid, fid));
            """)

    def _db(self):
        return sqlite3.connect(self.path, timeout=30)

    # ---- latency targets (pinged from this server) ------------------------------
    def targets(self, rid):
        try:
            with open(self.tpath) as f:
                return json.load(f).get(rid, [])
        except (OSError, ValueError):
            return []

    def set_targets(self, rid, rows):
        out, seen = [], set()
        for r in rows[:500]:
            ip, label = str(r.get("ip", "")).strip(), str(r.get("label", "")).strip()[:80]
            try:
                socket.inet_aton(ip)
            except OSError:
                raise ValueError(f"Not an IPv4 address: {ip}")
            if ip.count(".") == 3 and ip not in seen:
                seen.add(ip)
                out.append({"ip": ip, "label": label})
        try:
            with open(self.tpath) as f:
                allt = json.load(f)
        except (OSError, ValueError):
            allt = {}
        allt[rid] = out
        tmp = self.tpath + ".tmp"
        with open(tmp, "w") as f:
            json.dump(allt, f, indent=1)
        os.replace(tmp, self.tpath)
        return out

    # ---- loop -------------------------------------------------------------------
    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._loop, daemon=True, name="health")
        self.thread.start()

    def _loop(self):
        self.stop.wait(20)      # let the history poller make its SNMP clients first
        while not self.stop.is_set():
            t = time.time()
            for r in self.saved.list():
                if self.stop.is_set():
                    break
                try:
                    self.check(r["id"])
                except Exception as exc:     # keep going for the other routers
                    self.status[r["id"]] = {**self.status.get(r["id"], {}), "time": t, "error": str(exc)[:200]}
            self.stop.wait(max(5, INTERVAL - (time.time() - t)))

    def check(self, rid):
        full = self.saved.get(rid) or {}
        c = self.history._client(rid)
        now = time.time()
        found, st = [], {"time": now, "error": ""}
        with self._db() as db:
            self.db = db
            if c is not None:
                try:
                    st["interfaces"] = self._interfaces(rid, c, now, found)
                    st["optics"] = self._optics(rid, c, found)
                    self._device(c, found)
                except SnmpError as exc:
                    st["error"] = str(exc)[:200]
            else:
                st["error"] = "Add this router's SNMP login under Manager > Routers to check its links."
            targets = [{"ip": full.get("host", ""), "label": "Router (management address)"}] if full.get("host") else []
            targets += [t for t in self.targets(rid) if t["ip"] != full.get("host")]
            st["targets"], st["ping"] = len(targets), self._latency(rid, targets, found)
            self._stamp(rid, found, now)
            self.db = None
        with self.lock:
            self.findings[rid] = found
            self.status[rid] = st

    # ---- baselines: running mean and spread, per (key, metric) ------------------------
    def learn(self, rid, key, metric, x, clip=True):
        """Fold x into the baseline; returns (n, mean, sd) from BEFORE x, so x is judged against normal."""
        row = self.db.execute("SELECT n, mean, var FROM base WHERE rid=? AND key=? AND metric=?", (rid, key, metric)).fetchone()
        n, mean, var = row if row else (0, x, 0.0)
        sd = math.sqrt(max(var, 0))
        y = x
        if clip and n >= LEARN:     # an incident should not become the new normal
            y = min(max(x, mean - 4 * sd - 1e-9), mean + 4 * sd + 1e-9) if sd else mean + (x - mean) * 0.1
        a = max(ALPHA, 1 / (n + 1))
        d = y - mean
        nmean = mean + a * d
        nvar = (1 - a) * (var + a * d * d)
        self.db.execute("INSERT OR REPLACE INTO base VALUES (?,?,?,?,?,?,?)", (rid, key, metric, n + 1, nmean, nvar, int(time.time())))
        return n, mean, sd

    def _stamp(self, rid, found, now):
        """Remember when each problem was first seen, so the page can say 'since'."""
        ids = {f["id"] for f in found}
        old = dict(self.db.execute("SELECT fid, first FROM seen WHERE rid=?", (rid,)).fetchall())
        for f in found:
            f["since"] = old.get(f["id"], int(now))
            self.db.execute("INSERT OR REPLACE INTO seen VALUES (?,?,?,?)", (rid, f["id"], f["since"], int(now)))
        gone = [k for k in old if k not in ids]
        self.db.executemany("DELETE FROM seen WHERE rid=? AND fid=?", [(rid, k) for k in gone])

    # ---- interfaces: errors, drops, usage, link state ------------------------------
    def _interfaces(self, rid, c, now, found):
        cols = {k: c.walk(o, 5000) for k, o in (
            ("name", IFX + ".1"), ("alias", IFX + ".18"), ("speed", IFX + ".15"), ("inb", IFX + ".6"), ("outb", IFX + ".10"),
            ("inp", IFX + ".7"), ("outp", IFX + ".11"), ("indisc", IF + ".13"), ("inerr", IF + ".14"),
            ("outdisc", IF + ".19"), ("outerr", IF + ".20"), ("admin", IF + ".7"), ("oper", IF + ".8"), ("change", IF + ".9"))}
        n = 0
        self.desc[rid] = {text(v): text(cols["alias"].get(k)) for k, v in cols["name"].items()}
        for idx, raw in cols["name"].items():
            name = text(raw)
            if not name or name.lower().startswith(SKIP) or cols["admin"].get(idx) != 1:
                continue
            n += 1
            g = lambda k: cols[k].get(idx)
            desc, up = text(g("alias")), g("oper") == 1
            cur = {k: g(k) for k in ("inb", "outb", "inp", "outp", "indisc", "inerr", "outdisc", "outerr", "change")}
            cur["t"], cur["up"] = now, up
            prev = self.prev.get((rid, name))
            self.prev[(rid, name)] = cur
            base = {"target": name, "description": desc}
            # link state
            if prev and prev["up"] != up or prev and up and cur["change"] != prev["change"] and isinstance(cur["change"], int):
                self.flaps.setdefault((rid, name), []).append(now)
            fl = [x for x in self.flaps.get((rid, name), []) if now - x < 86400]
            self.flaps[(rid, name)] = fl
            if not up and desc:
                found.append({**base, "id": "down:" + name, "kind": "down", "severity": "critical", "slow": True,
                              "title": "Link down", "detail": f"{name} is enabled but down.", "advice": ADVICE["down"]})
            elif len(fl) >= 2:
                found.append({**base, "id": "flap:" + name, "kind": "flap", "severity": "critical" if len(fl) >= 4 else "warning", "slow": True,
                              "title": "Link flapping", "detail": f"Went down and up {len(fl)} times in the last 24 hours.", "advice": ADVICE["flap"]})
            if not prev or not up or not prev["up"]:
                continue
            dt = now - prev["t"]
            if dt < 30 or dt > 3 * INTERVAL:
                continue
            d = {}
            for k in ("inb", "outb", "inp", "outp", "indisc", "inerr", "outdisc", "outerr"):
                a, b = cur[k], prev[k]
                d[k] = (a - b) / dt if isinstance(a, int) and isinstance(b, int) and a >= b else None
            pin, pout = d["inp"] or 0, d["outp"] or 0
            # errors
            if d["inerr"]:
                ratio = d["inerr"] / max(pin + d["inerr"], 1)
                nb, mean, sd = self.learn(rid, name, "inerr", d["inerr"])
                if ratio >= 1e-4 or d["inerr"] >= 1 or nb >= LEARN and d["inerr"] > mean + 4 * sd and d["inerr"] > 0.05:
                    found.append({**base, "id": "errors:" + name, "kind": "errors", "slow": True,
                                  "severity": "critical" if ratio >= 1e-3 or d["inerr"] >= 10 else "warning",
                                  "title": "Input errors (CRC)", "value": d["inerr"],
                                  "detail": f"{_n(d['inerr'])} errors/s, {ratio * 100:.3f}% of packets received.", "advice": ADVICE["errors"]})
            elif d["inerr"] == 0:
                self.learn(rid, name, "inerr", 0.0)
            # drops
            for k, pk, label, kind in (("outdisc", pout, "Output drops", "out_drops"), ("indisc", pin, "Input drops", "in_drops")):
                v = d[k]
                if v is None:
                    continue
                nb, mean, sd = self.learn(rid, name, k, v)
                if not v:
                    continue
                ratio = v / max(pk + v, 1)
                unusual = nb >= LEARN and v > mean + 4 * sd and v > 2 * mean
                if ratio >= 1e-3 or unusual and ratio >= 1e-4:
                    found.append({**base, "id": f"{kind}:{name}", "kind": kind, "slow": True, "value": v,
                                  "severity": "critical" if ratio >= 0.01 else "warning", "title": label,
                                  "detail": f"{_n(v)} packets/s dropped, {ratio * 100:.2f}% of packets"
                                            + (f" (normally {_n(mean)}/s)." if nb >= LEARN else "."), "advice": ADVICE[kind]})
            # usage
            sp = g("speed")
            if isinstance(sp, int) and sp > 0 and d["inb"] is not None and d["outb"] is not None:
                use = max(d["inb"], d["outb"]) * 8 / (sp * 1e6) * 100
                if use >= 90:
                    way = "in" if d["inb"] >= d["outb"] else "out"
                    found.append({**base, "id": "congestion:" + name, "kind": "congestion", "slow": True, "value": use,
                                  "severity": "critical" if use >= 98 else "warning", "title": "Link nearly full",
                                  "detail": f"{use:.0f}% used ({way}) of {_speed(sp)}.", "advice": ADVICE["congestion"]})
        return n

    # ---- SFP light ---------------------------------------------------------------
    def _optics(self, rid, c, found):
        rx = c.walk(HW_OPT + ".8", 30)
        if not rx:
            return 0
        names = {k: text(v) for k, v in c.walk(ENT_NAME, 30).items()}
        cols = {k: c.walk(f"{HW_OPT}.{o}", 30) for k, o in (("tx", 9), ("rx_low", 13), ("rx_high", 14), ("tx_low", 15), ("tx_high", 16))}
        n = 0
        descs = self._aliases(rid)
        for idx, v in rx.items():
            dbm = lambda x: x / 100 if isinstance(x, int) else None
            r, t = dbm(v), dbm(cols["tx"].get(idx))
            lo, hi, tlo, thi = (dbm(cols[k].get(idx)) for k in ("rx_low", "rx_high", "tx_low", "tx_high"))
            name = names.get(idx, idx)
            if str(name).upper().startswith(("100GE", "40GE", "400GE", "200GE")):
                # 4-lane modules: the router can report the total of the lanes against per-lane limits (+6 dB)
                hi = hi + 6.02 if hi is not None else None
                thi = thi + 6.02 if thi is not None else None
            if r is None or r <= -40:       # no module or no light at all: the link-down check covers it
                continue
            n += 1
            base = {"target": name, "description": descs.get(name, "")}
            nb, mean, sd = self.learn(rid, name, "rx_dbm", r)
            if lo is not None and r < lo:
                found.append({**base, "id": "rx:" + name, "kind": "optics", "severity": "critical", "slow": True, "value": r,
                              "title": "SFP light too low", "detail": f"Receiving {r:.2f} dBm; the module alarms below {lo:.2f} dBm.", "advice": ADVICE["rx_low"]})
            elif hi is not None and r > hi:
                found.append({**base, "id": "rx:" + name, "kind": "optics", "severity": "critical", "slow": True, "value": r,
                              "title": "SFP light too high", "detail": f"Receiving {r:.2f} dBm; the module alarms above {hi:.2f} dBm.", "advice": ADVICE["rx_high"]})
            elif lo is not None and r < lo + 2:
                found.append({**base, "id": "rx:" + name, "kind": "optics", "severity": "warning", "slow": False, "value": r,
                              "title": "SFP light weak", "detail": f"Receiving {r:.2f} dBm, within {r - lo:.1f} dB of the alarm level ({lo:.2f} dBm).", "advice": ADVICE["rx_low"]})
            elif nb >= LEARN and mean - r >= max(2, 4 * sd):
                found.append({**base, "id": "rxfall:" + name, "kind": "optics", "severity": "warning", "slow": False, "value": r,
                              "title": "SFP light falling", "detail": f"Receiving {r:.2f} dBm, {mean - r:.1f} dB below its normal {mean:.2f} dBm.", "advice": ADVICE["rx_fall"]})
            if t is not None and (tlo is not None and t < tlo or thi is not None and t > thi):
                found.append({**base, "id": "tx:" + name, "kind": "optics", "severity": "critical", "slow": True, "value": t,
                              "title": "SFP transmit power out of range", "detail": f"Sending {t:.2f} dBm (range {tlo} to {thi} dBm).", "advice": ADVICE["tx"]})
        return n

    def _aliases(self, rid):
        return self.desc.get(rid, {})

    # ---- the router itself ---------------------------------------------------------
    def _device(self, c, found):
        for o, kind, title, warn, crit in ((".5", "cpu", "CPU high", 80, 90), (".7", "memory", "Memory high", 85, 95), (".11", "temp", "Temperature high", 65, 75)):
            vals = [v for v in c.walk(HW_ENT + o, 3).values() if isinstance(v, int) and 0 < v < 200]
            if vals and max(vals) >= warn:
                v = max(vals)
                found.append({"id": kind, "kind": kind, "target": "Router", "description": "", "severity": "critical" if v >= crit else "warning",
                              "slow": kind == "cpu", "value": v, "title": title,
                              "detail": f"{v}{' C' if kind == 'temp' else '%'} on the busiest board.", "advice": ADVICE[kind]})

    # ---- latency and loss -------------------------------------------------------------
    def _latency(self, rid, targets, found):
        if not targets:
            self.latency[rid] = []
            return "no targets"
        try:
            with ThreadPoolExecutor(32) as ex:
                res = list(ex.map(lambda t: (t, _safe_ping(t["ip"])), targets))
        except Exception as exc:
            self.latency[rid] = []
            return f"unavailable: {exc}"
        rows, note = [], "ok"
        for t, r in res:
            if "error" in r:
                note = "unavailable: " + r["error"]
                rows.append({**t, **r})
                continue
            row = {**t, **r}
            if r["avg"] is not None:
                nb, mean, sd = self.learn(rid, t["ip"], "rtt", r["avg"])
                row["normal"] = round(mean, 2) if nb >= LEARN else None
                if nb >= LEARN and r["avg"] > mean + max(4 * sd, 15) and r["avg"] > 1.5 * mean:
                    found.append({"id": "latency:" + t["ip"], "kind": "latency", "target": t["ip"], "description": t["label"], "slow": True,
                                  "severity": "critical" if r["avg"] > 3 * mean + 50 else "warning", "value": r["avg"], "title": "Latency up",
                                  "detail": f"{r['avg']:.1f} ms round trip, normally {mean:.1f} ms.", "advice": ADVICE["latency"]})
            nb, mean, sd = self.learn(rid, t["ip"], "loss", r["loss"], clip=False)
            if r["received"] == 0:
                if nb >= 3 and mean < 50:       # it normally answers; a device that never answers ping is not a problem
                    found.append({"id": "loss:" + t["ip"], "kind": "loss", "target": t["ip"], "description": t["label"], "slow": True,
                                  "severity": "critical", "value": 100, "title": "Not answering",
                                  "detail": "No reply to 5 pings; it normally answers.", "advice": ADVICE["loss"]})
            elif r["loss"] >= 20:
                found.append({"id": "loss:" + t["ip"], "kind": "loss", "target": t["ip"], "description": t["label"], "slow": True,
                              "severity": "critical" if r["loss"] >= 40 else "warning", "value": r["loss"], "title": "Packet loss",
                              "detail": f"{r['loss']:.0f}% of pings lost.", "advice": ADVICE["loss"]})
            rows.append(row)
        self.latency[rid] = rows
        return note

    def report(self, rid):
        with self.lock:
            f = list(self.findings.get(rid, []))
            st = dict(self.status.get(rid, {}))
            lat = list(self.latency.get(rid, []))
        f.sort(key=lambda x: (x["severity"] != "critical", not x.get("slow"), x["kind"], x["target"]))
        return {"findings": f, "status": st, "latency": lat, "targets": self.targets(rid), "interval": INTERVAL, "learn_hours": LEARN * INTERVAL // 3600}


def _safe_ping(ip):
    try:
        return icmp_ping(ip)
    except OSError as exc:
        return {"error": str(exc)}


def _n(x):
    return f"{x:.0f}" if x >= 10 else f"{x:.2f}".rstrip("0").rstrip(".") or "0"


def _speed(mbps):
    return f"{mbps / 1000:g} Gbit/s" if mbps >= 1000 else f"{mbps} Mbit/s"


def demo_report(ifs):
    """Made-up findings for demo mode, on the demo router's interfaces."""
    d = lambda n: (ifs.get(n) or {}).get("desc", "")
    now = int(time.time())
    f = [
        {"id": "congestion:GigabitEthernet0/1/6.301", "kind": "congestion", "target": "GigabitEthernet0/1/6.301", "severity": "critical", "slow": True,
         "value": 99, "title": "Link nearly full", "detail": "99% used (out) of 1 Gbit/s.", "advice": ADVICE["congestion"], "since": now - 2400},
        {"id": "out_drops:GigabitEthernet0/1/6.301", "kind": "out_drops", "target": "GigabitEthernet0/1/6.301", "severity": "warning", "slow": True,
         "value": 41, "title": "Output drops", "detail": "41 packets/s dropped, 0.35% of packets (normally 0/s).", "advice": ADVICE["out_drops"], "since": now - 2100},
        {"id": "latency:102.216.117.2", "kind": "latency", "target": "102.216.117.2", "description": "Reseller AlphaNet CPE", "severity": "warning", "slow": True,
         "value": 48.5, "title": "Latency up", "detail": "48.5 ms round trip, normally 3.2 ms.", "advice": ADVICE["latency"], "since": now - 1800},
        {"id": "errors:GigabitEthernet0/1/1", "kind": "errors", "target": "GigabitEthernet0/1/1", "severity": "warning", "slow": True,
         "value": 3.2, "title": "Input errors (CRC)", "detail": "3.2 errors/s, 0.004% of packets received.", "advice": ADVICE["errors"], "since": now - 9000},
        {"id": "rxfall:GigabitEthernet0/1/6", "kind": "optics", "target": "GigabitEthernet0/1/6", "severity": "warning", "slow": False,
         "value": -17.8, "title": "SFP light falling", "detail": "Receiving -17.80 dBm, 3.1 dB below its normal -14.70 dBm.", "advice": ADVICE["rx_fall"], "since": now - 86400},
    ]
    for x in f:
        x.setdefault("description", d(x["target"]))
    lat = [{"ip": "1.1.1.1", "label": "Router (management address)", "sent": 5, "received": 5, "loss": 0, "min": 0.8, "avg": 1.1, "max": 1.6, "normal": 1.0},
           {"ip": "102.216.117.2", "label": "Reseller AlphaNet CPE", "sent": 5, "received": 5, "loss": 0, "min": 41.2, "avg": 48.5, "max": 57.0, "normal": 3.2},
           {"ip": "102.216.117.34", "label": "Shared reseller VLAN CPE", "sent": 5, "received": 4, "loss": 20, "min": 2.1, "avg": 2.6, "max": 3.3, "normal": 2.4}]
    return {"findings": f, "status": {"time": now, "error": "", "interfaces": len(ifs), "optics": 4, "targets": len(lat), "ping": "ok", "demo": True},
            "latency": lat, "targets": [{"ip": x["ip"], "label": x["label"]} for x in lat[1:]], "interval": INTERVAL, "learn_hours": LEARN * INTERVAL // 3600}
