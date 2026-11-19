"""Sites: the places the network is built in (core, PoPs, BTS, customer hubs, data centres), fully registered, and what
is watched at each: the devices, the power, access control and CCTV.

A site must be fully registered (every required field) before anything can be attached to it. Each monitored item
with an address is pinged every minute from this server (up/down, delay); power, door and camera systems can also
post their alarms to POST /api/site-events (mains failed, on battery, door forced, camera offline...). Kept by the
app in <data>/site_registry.json and site_events.json; nothing here talks to the router.
"""
import json
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import health

TYPES = {"core": "Core / main PoP", "pop": "PoP", "bts": "BTS / tower", "hub": "Customer hub", "dc": "Data centre", "office": "Office / store"}
POWER = {"grid": "Grid (KPLC)", "generator": "Generator", "solar": "Solar", "battery": "Battery / UPS"}
KINDS = {"device": "Network device", "power": "Power (UPS, rectifier, generator, meter)", "access": "Access control", "cctv": "CCTV (NVR, camera)", "env": "Environment sensor"}
REQUIRED = [("name", "Name"), ("code", "Site code"), ("type", "Type"), ("county", "County"), ("town", "Town"), ("lat", "GPS latitude"), ("lon", "GPS longitude"),
            ("address", "Address and directions"), ("contact_name", "Site contact"), ("contact_phone", "Contact phone"), ("power", "Power source"),
            ("access", "How to get in (keys, codes, who to call)")]
OPTIONAL = ["landlord", "landlord_phone", "lease_expiry", "ups_kva", "battery_hours", "notes"]
# events the power, door and camera systems post: severity, and what clears them
EVENTS = {
    "power": {"mains_fail": "critical", "on_battery": "warning", "battery_low": "critical", "generator_on": "info", "generator_fail": "critical", "overload": "warning", "mains_ok": "ok"},
    "access": {"door_forced": "critical", "door_held": "warning", "access_denied": "warning", "door_open": "info", "access_granted": "info", "door_closed": "ok"},
    "cctv": {"camera_offline": "warning", "recording_stopped": "critical", "disk_full": "critical", "disk_error": "critical", "tamper": "critical", "motion": "info", "camera_ok": "ok"},
    "env": {"temp_high": "warning", "temp_critical": "critical", "water": "critical", "smoke": "critical", "humidity_high": "warning", "env_ok": "ok"},
    "device": {"down": "critical", "up": "ok"},
}
IP_RE = re.compile(r"^[A-Za-z0-9.:\-]{1,120}$")


def missing(site):
    """The required fields a site does not have yet (empty when it is fully registered)."""
    out = []
    for k, label in REQUIRED:
        v = site.get(k)
        if v in (None, "", []) or (k == "power" and not [p for p in v if p in POWER]):
            out.append(label)
    return out


class Registry:
    def __init__(self, data_dir):
        self.path = os.path.join(data_dir, "site_registry.json")
        self.epath = os.path.join(data_dir, "site_events.json")
        self.lock = threading.Lock()
        self.state = {}         # asset id -> {up, ms, since, checked}
        self.stop = threading.Event()
        self.thread = None

    # ---- storage
    def _load(self, path, empty):
        try:
            with open(path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return empty

    def _write(self, path, data):
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, path)

    def sites(self):
        d = self._load(self.path, {})
        return d.get("sites", []) if isinstance(d, dict) else []

    def get(self, sid):
        return next((s for s in self.sites() if s["id"] == sid or s.get("code", "").lower() == str(sid).lower()), None)

    def complete(self, sid):
        s = self.get(sid)
        return bool(s) and not missing(s)

    @staticmethod
    def clean(d, old=None):
        s = lambda k, n=200: str(d.get(k) if d.get(k) is not None else "").strip()[:n]
        row = {"name": s("name", 80), "code": s("code", 20).upper(), "type": s("type", 20), "county": s("county", 40), "town": s("town", 60),
               "address": s("address", 400), "contact_name": s("contact_name", 80), "contact_phone": s("contact_phone", 40), "access": s("access", 400)}
        if row["type"] and row["type"] not in TYPES:
            raise ValueError("Pick the site type from the list")
        if row["code"] and not re.match(r"^[A-Z0-9][A-Z0-9_-]{0,19}$", row["code"]):
            raise ValueError("A site code is letters, digits, - and _ (e.g. MBT-01)")
        for k in ("lat", "lon"):
            v = d.get(k)
            if v in (None, ""):
                row[k] = None
                continue
            try:
                row[k] = round(float(v), 6)
            except (TypeError, ValueError):
                raise ValueError("GPS is two numbers, e.g. 2.3346 and 37.9899")
        if row["lat"] is not None and not -5.5 <= row["lat"] <= 5.5 or row["lon"] is not None and not 33.5 <= row["lon"] <= 42.5:
            raise ValueError("That GPS point is not in Kenya: latitude -5.5 to 5.5, longitude 33.5 to 42.5")
        row["power"] = [p for p in (d.get("power") or []) if p in POWER]
        for k in OPTIONAL:
            row[k] = s(k, 400)
        row["assets"] = (old or {}).get("assets", [])
        return row

    def save(self, d, sid=None):
        with self.lock:
            all_ = self.sites()
            old = next((x for x in all_ if x["id"] == sid), None) if sid else None
            if sid and not old:
                raise ValueError("No such site")
            row = self.clean(d, old)
            if not row["name"] or not row["code"]:
                raise ValueError("A site needs at least a name and a code")
            if any(x["code"] == row["code"] and x["id"] != sid for x in all_):
                raise ValueError(f"Another site already has the code {row['code']}")
            row["id"] = old["id"] if old else uuid.uuid4().hex[:10]
            row["created"] = old.get("created") if old else int(time.time())
            row["updated"] = int(time.time())
            all_ = [x for x in all_ if x["id"] != row["id"]] + [row]
            self._write(self.path, {"sites": all_})
        return row

    def delete(self, sid, attached=0):
        with self.lock:
            all_ = self.sites()
            s = next((x for x in all_ if x["id"] == sid), None)
            if not s:
                raise ValueError("No such site")
            if attached:
                raise ValueError(f"{attached} saved router(s) are attached to {s['name']}: move them to another site first")
            self._write(self.path, {"sites": [x for x in all_ if x["id"] != sid]})
        return s

    def save_asset(self, sid, d, aid=None):
        with self.lock:
            all_ = self.sites()
            s = next((x for x in all_ if x["id"] == sid), None)
            if not s:
                raise ValueError("No such site")
            if missing(s):
                raise ValueError(f"Finish registering {s['name']} first: {', '.join(missing(s))}")
            kind, name, ip = str(d.get("kind") or ""), str(d.get("name") or "").strip()[:80], str(d.get("ip") or "").strip()[:120]
            if kind not in KINDS:
                raise ValueError("Pick what it is: device, power, access control, CCTV or sensor")
            if not name:
                raise ValueError("Give it a name")
            if ip and not IP_RE.match(ip):
                raise ValueError("Not an address: " + ip)
            row = {"id": aid or uuid.uuid4().hex[:10], "kind": kind, "name": name, "ip": ip, "model": str(d.get("model") or "").strip()[:80],
                   "note": str(d.get("note") or "").strip()[:200]}
            s["assets"] = [a for a in s.get("assets", []) if a["id"] != row["id"]] + [row]
            self._write(self.path, {"sites": all_})
        return row

    def delete_asset(self, sid, aid):
        with self.lock:
            all_ = self.sites()
            s = next((x for x in all_ if x["id"] == sid), None)
            if not s:
                raise ValueError("No such site")
            s["assets"] = [a for a in s.get("assets", []) if a["id"] != aid]
            self._write(self.path, {"sites": all_})
        self.state.pop(aid, None)

    # ---- events from power, door and camera systems (and from the ping monitor)
    def events(self, sid=None, limit=200):
        ev = self._load(self.epath, [])
        ev = [e for e in ev if not sid or e["site"] == sid]
        return ev[-limit:][::-1]

    def add_event(self, site_ref, kind, event, detail="", asset="", source=""):
        s = self.get(site_ref)
        if not s:
            raise ValueError(f"No site {site_ref}")
        kind, event = str(kind or "").lower(), str(event or "").lower()
        if kind not in EVENTS or event not in EVENTS[kind]:
            raise ValueError(f"Unknown event: {kind}/{event}. Known: " + "; ".join(f"{k}: {', '.join(v)}" for k, v in EVENTS.items()))
        e = {"ts": int(time.time()), "site": s["id"], "kind": kind, "event": event, "severity": EVENTS[kind][event], "detail": str(detail or "")[:300],
             "asset": str(asset or "")[:80], "source": str(source or "")[:40]}
        with self.lock:
            ev = self._load(self.epath, [])
            ev = [x for x in ev if x["ts"] > time.time() - 30 * 86400][-5000:] + [e]
            self._write(self.epath, ev)
        return e

    def alarms(self, sid):
        """What is wrong at a site now: each kind/asset's latest event when it is a warning or worse, plus items not answering."""
        latest = {}
        for e in reversed(self.events(sid, 5000)):
            if e.get("source") != "ping" and e["severity"] != "info":     # pings are judged live below; info events never stay as alarms
                latest[(e["kind"], e["asset"])] = e
        out = [e for e in latest.values() if e["severity"] in ("warning", "critical") and time.time() - e["ts"] < 7 * 86400]
        s = self.get(sid) or {}
        for a in s.get("assets", []):
            st = self.state.get(a["id"])
            if a.get("ip") and st and st["up"] is False:
                out.append({"ts": st["since"], "site": sid, "kind": a["kind"], "event": "down", "severity": "critical" if a["kind"] in ("device", "power") else "warning",
                            "detail": f"{a['name']} ({a['ip']}) is not answering", "asset": a["name"], "source": "ping"})
        return sorted(out, key=lambda e: (e["severity"] != "critical", -e["ts"]))

    # ---- the ping monitor
    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._loop, daemon=True, name="siteregistry")
        self.thread.start()

    def _loop(self):
        self.stop.wait(20)
        while not self.stop.is_set():
            try:
                self.check()
            except Exception as exc:        # keep the loop alive
                print(f"sites: {exc}", flush=True)
            self.stop.wait(60)

    def check(self):
        items = [(s, a) for s in self.sites() for a in s.get("assets", []) if a.get("ip")]

        def one(sa):
            s, a = sa
            try:
                p = health.icmp_ping(a["ip"], count=2, timeout=1.0)
                up, ms = p["received"] > 0, p["avg"]
            except OSError:
                up, ms = False, None
            old = self.state.get(a["id"])
            now = int(time.time())
            self.state[a["id"]] = {"up": up, "ms": ms, "since": now if not old or old["up"] != up else old["since"], "checked": now}
            if old and old["up"] != up:
                self._log_ping(s, a, up)
        with ThreadPoolExecutor(8) as ex:
            list(ex.map(one, items))
        return len(items)

    def _log_ping(self, s, a, up):
        e = {"ts": int(time.time()), "site": s["id"], "kind": a["kind"], "event": "up" if up else "down", "severity": "ok" if up else "critical",
             "detail": f"{a['name']} ({a['ip']}) {'answers again' if up else 'stopped answering'}", "asset": a["name"], "source": "ping"}
        with self.lock:
            ev = self._load(self.epath, [])
            self._write(self.epath, ev[-5000:] + [e])

    def report(self, routers=()):
        """Every site with its registration state, its items (with up/down), its alarms and its saved routers."""
        out = []
        for s in self.sites():
            assets = [{**a, **({"up": self.state[a["id"]]["up"], "ms": self.state[a["id"]]["ms"], "since": self.state[a["id"]]["since"],
                                "checked": self.state[a["id"]]["checked"]} if a["id"] in self.state else {})} for a in s.get("assets", [])]
            al = self.alarms(s["id"])
            out.append({**s, "assets": assets, "missing": missing(s), "alarms": al, "routers": [r for r in routers if r.get("site") == s["id"]],
                        "status": "critical" if any(a["severity"] == "critical" for a in al) else "warning" if al else "ok"})
        return sorted(out, key=lambda x: x["name"].lower())
