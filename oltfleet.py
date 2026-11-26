"""OLT management: every ONU on every OLT in one place, without connecting to each OLT.

Two sources, both read-only here:
- SmartOLT: its REST API (https://<subdomain>.smartolt.com/api, header X-Token) for OLTs, ONUs, statuses, signals
  and the ONUs waiting for authorisation. The API key is kept in <data>/oltm.json (owner-only file).
- OLTs saved under Manager > Routers with the type OLT: read directly over SNMP (olt.Olt).

A background thread reads them on a timer, keeps the latest picture in memory, a 7-day history of the counts
(for the network status graph) and a 2-day history of each ONU's receive level (for its signal graph).
Authorising, rebooting and changing ONUs come in the next step."""
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from array import array

from auth import _write_private

SUBDOMAIN_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
WARN_DBM, CRIT_DBM = -27.0, -28.5           # ONU receive level (1490 nm): warning / critical below these
STATUSES = ("online", "power fail", "los", "offline", "disabled", "n/a")
HISTORY_S = 7 * 86400
RX_EVERY = 900                              # one receive-level point per ONU every 15 minutes
RX_POINTS = 2 * 86400 // RX_EVERY


class FleetError(Exception):
    pass


def _f(v):
    """'-24.95' / -24.95 / '-24.95 dBm' -> float, or None."""
    if v is None or v == "":
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", str(v))
    return float(m.group(0)) if m else None


def _status(v):
    s = str(v or "").strip().lower().replace("_", " ")
    if s in ("online", "working", "up"):
        return "online"
    if "power" in s or "dying" in s or s == "pwrfail":
        return "power fail"
    if "los" in s or "loss" in s:
        return "los"
    if "disab" in s:
        return "disabled"
    if s in ("offline", "down"):
        return "offline"
    return "n/a" if not s or s in ("n/a", "na", "unknown") else s


def signal_level(rx):
    if rx is None:
        return "none"
    return "critical" if rx < CRIT_DBM else "warning" if rx < WARN_DBM else "good"


class SmartOlt:
    """SmartOLT REST API client (read-only calls)."""

    def __init__(self, subdomain, key, timeout=30, opener=None):
        if not SUBDOMAIN_RE.match(subdomain or "") or not key:
            raise FleetError("SmartOLT subdomain and API key are required.")
        self.base = f"https://{subdomain}.smartolt.com/api/"
        self.key, self.timeout = key, timeout
        self.open = opener or urllib.request.urlopen

    def get(self, path):
        req = urllib.request.Request(self.base + path.lstrip("/"), headers={"X-Token": self.key, "Accept": "application/json",
                                                                             "User-Agent": "NE8000-Manager"})
        try:
            with self.open(req, timeout=self.timeout) as res:
                body = res.read(64 * 1024 * 1024)
        except urllib.error.HTTPError as exc:
            msg = ""
            try:
                msg = json.loads(exc.read(65536) or b"{}").get("error", "")
            except Exception:
                pass
            if exc.code in (401, 403) and not msg:
                msg = "the API key was refused"
            raise FleetError(f"SmartOLT {path}: HTTP {exc.code} {msg}".strip()) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise FleetError(f"SmartOLT could not be reached: {getattr(exc, 'reason', exc)}") from exc
        try:
            data = json.loads(body)
        except ValueError as exc:
            raise FleetError(f"SmartOLT {path}: not JSON") from exc
        if isinstance(data, dict) and data.get("status") is False:
            raise FleetError(f"SmartOLT {path}: {data.get('error') or data.get('response') or 'refused'}")
        return data

    @staticmethod
    def rows(data, *keys):
        if isinstance(data, list):
            return data
        for k in keys + ("response", "onus", "olts", "data"):
            v = data.get(k) if isinstance(data, dict) else None
            if isinstance(v, list):
                return v
        return []


ACS_FIELDS = {   # TR-098 (InternetGatewayDevice) and TR-181 (Device) paths, first that answers wins
    "firmware": ["InternetGatewayDevice.DeviceInfo.SoftwareVersion", "Device.DeviceInfo.SoftwareVersion"],
    "hardware": ["InternetGatewayDevice.DeviceInfo.HardwareVersion", "Device.DeviceInfo.HardwareVersion"],
    "uptime_s": ["InternetGatewayDevice.DeviceInfo.UpTime", "Device.DeviceInfo.UpTime"],
    "ssid": ["InternetGatewayDevice.LANDevice.1.WLANConfiguration.1.SSID", "Device.WiFi.SSID.1.SSID"],
    "ssid_5g": ["InternetGatewayDevice.LANDevice.1.WLANConfiguration.5.SSID", "Device.WiFi.SSID.2.SSID"],
    "wifi_clients": ["InternetGatewayDevice.LANDevice.1.WLANConfiguration.1.TotalAssociations", "Device.WiFi.AccessPoint.1.AssociatedDeviceNumberOfEntries"],
    "wan_ip": ["InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1.ExternalIPAddress",
               "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANIPConnection.1.ExternalIPAddress",
               "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.2.WANPPPConnection.1.ExternalIPAddress",
               "Device.PPP.Interface.1.IPCP.LocalIPAddress", "Device.IP.Interface.1.IPv4Address.1.IPAddress"],
    "pppoe_user": ["InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1.Username",
                   "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.2.WANPPPConnection.1.Username", "Device.PPP.Interface.1.Username"],
    "wan_status": ["InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1.ConnectionStatus",
                   "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANIPConnection.1.ConnectionStatus", "Device.PPP.Interface.1.ConnectionStatus"],
    "lan_hosts": ["InternetGatewayDevice.LANDevice.1.Hosts.HostNumberOfEntries", "Device.Hosts.HostNumberOfEntries"],
}
ACS_RECENT = 3 * 3600      # a device that reported (informed) within this time counts as reachable


def _gv(dev, path):
    cur = dev
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur.get("_value") if isinstance(cur, dict) else None


def _ts(v):
    """GenieACS time ('2026-11-26T09:12:00.123Z') -> epoch seconds."""
    if not v:
        return None
    try:
        import datetime
        return datetime.datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def sn_keys(sn):
    """Ways the same ONU serial is written: GPON 'HWTC8D959C31', TR-069 '485754438D959C31' (vendor id in hex)."""
    s = re.sub(r"[^A-Za-z0-9]", "", str(sn or "")).upper()
    if not s:
        return []
    out = {s}
    if len(s) == 12 and s[:4].isalpha():
        out.add(s[:4].encode().hex().upper() + s[4:])
    if len(s) == 16 and re.fullmatch(r"[0-9A-F]{16}", s):
        try:
            v = bytes.fromhex(s[:8]).decode()
            if v.isalpha():
                out.add(v.upper() + s[8:])
        except (ValueError, UnicodeDecodeError):
            pass
    return list(out)


class GenieAcs:
    """GenieACS north-bound API (default port 7557), read-only: the customer routers / ONTs managed over TR-069."""

    def __init__(self, url, user="", password="", timeout=30, opener=None):
        u = urllib.parse.urlsplit(str(url or "").strip())
        if u.scheme not in ("http", "https") or not u.hostname:
            raise FleetError("The GenieACS address looks like http://10.0.0.5:7557")
        self.base = str(url).strip().rstrip("/") + "/"
        self.user, self.password, self.timeout = user, password, timeout
        self.open = opener or urllib.request.urlopen

    def get(self, path):
        h = {"Accept": "application/json", "User-Agent": "NE8000-Manager"}
        if self.user:
            import base64
            h["Authorization"] = "Basic " + base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
        try:
            with self.open(urllib.request.Request(self.base + path, headers=h), timeout=self.timeout) as res:
                return json.loads(res.read(64 * 1024 * 1024))
        except urllib.error.HTTPError as exc:
            raise FleetError(f"GenieACS: HTTP {exc.code}" + (" (login refused)" if exc.code in (401, 403) else "")) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise FleetError(f"GenieACS could not be reached: {getattr(exc, 'reason', exc)}") from exc
        except ValueError as exc:
            raise FleetError("GenieACS: not JSON (is this the north-bound API port, usually 7557?)") from exc

    def devices(self):
        proj = ["_id", "_lastInform", "_registered", "_deviceId", "_tags"] + [p for ps in ACS_FIELDS.values() for p in ps]
        rows = self.get("devices/?projection=" + urllib.parse.quote(",".join(proj)))
        out = []
        for d in rows if isinstance(rows, list) else []:
            di = d.get("_deviceId") or {}
            x = {"id": str(d.get("_id", "")), "serial": str(di.get("_SerialNumber", "") or ""), "maker": str(di.get("_Manufacturer", "") or ""),
                 "oui": str(di.get("_OUI", "") or ""), "model": str(di.get("_ProductClass", "") or ""),
                 "last_inform": _ts(d.get("_lastInform")), "registered": _ts(d.get("_registered")),
                 "tags": [str(t) for t in d.get("_tags") or []][:20]}
            for k, paths in ACS_FIELDS.items():
                x[k] = next((v for v in (_gv(d, p) for p in paths) if v not in (None, "")), None)
            for k in ("uptime_s", "wifi_clients", "lan_hosts"):
                try:
                    x[k] = int(x[k]) if x[k] is not None else None
                except (TypeError, ValueError):
                    x[k] = None
            x["recent"] = bool(x["last_inform"] and time.time() - x["last_inform"] < ACS_RECENT)
            out.append(x)
        return out


class OltFleet:
    def __init__(self, data_dir, saved_olts=lambda: [], snmp_reader=None, smartolt_factory=SmartOlt, acs_factory=GenieAcs):
        self.path = os.path.join(data_dir, "oltm.json")
        self.hist_path = os.path.join(data_dir, "oltm_history.json")
        self.saved_olts = saved_olts              # () -> saved routers with kind "olt"
        self.snmp_reader = snmp_reader            # (saved router) -> olt.Olt-like with onus(), overview()
        self.factory = smartolt_factory
        self.acs_factory = acs_factory
        self.acs = []                             # TR-069 devices from GenieACS
        self.lock = threading.Lock()
        self.cfg = {"smartolt": {"subdomain": "", "key": "", "enabled": False}, "acs": {"url": "", "user": "", "password": "", "enabled": False},
                    "every": 300, "details_every": 1800}
        try:
            with open(self.path) as f:
                saved = json.load(f)
            self.cfg.update({k: v for k, v in saved.items() if k in self.cfg})
            self.cfg["acs"] = {"url": "", "user": "", "password": "", "enabled": False, **(self.cfg.get("acs") or {})}
        except (OSError, ValueError):
            pass
        self.snap = {"olts": [], "onus": [], "unconfigured": [], "errors": [], "time": 0, "acs_time": 0}
        self.details = {}                         # SmartOLT ONU details (name, zone, ...), refreshed less often
        self.details_at = 0
        self.rx = {}                              # onu key -> array of receive levels, newest last
        self.rx_at = 0
        self.history = []
        try:
            with open(self.hist_path) as f:
                self.history = json.load(f)
        except (OSError, ValueError):
            pass
        self.wake = threading.Event()
        self.thread = None

    # ---- settings -------------------------------------------------------------
    def settings(self):
        s = self.cfg["smartolt"]
        a = self.cfg["acs"]
        return {"smartolt": {"subdomain": s["subdomain"], "enabled": s["enabled"], "key_set": bool(s["key"])},
                "acs": {"url": a["url"], "user": a["user"], "enabled": a["enabled"], "password_set": bool(a["password"])},
                "every": self.cfg["every"]}

    def save_settings(self, body):
        s = dict(self.cfg["smartolt"])
        so = body.get("smartolt") or {}
        sub = str(so.get("subdomain", s["subdomain"]) or "").strip().lower()
        sub = re.sub(r"^https?://", "", sub).split(".smartolt.com")[0].strip("/")
        if sub and not SUBDOMAIN_RE.match(sub):
            raise FleetError("The SmartOLT subdomain is the first part of your SmartOLT address, e.g. 'savenet' for savenet.smartolt.com.")
        key = so.get("key")
        if key:
            key = str(key).strip()
            if len(key) > 200 or any(ord(c) < 33 for c in key):
                raise FleetError("Invalid API key.")
            s["key"] = key
        s["subdomain"] = sub
        s["enabled"] = bool(so.get("enabled", s["enabled"])) and bool(sub and s["key"])
        a = dict(self.cfg["acs"])
        if "acs" in body:
            ab = body.get("acs") or {}
            url = str(ab.get("url", a["url"]) or "").strip()
            if url:
                GenieAcs(url)                    # checks the address
            user = str(ab.get("user", a["user"]) or "").strip()[:100]
            if ab.get("password"):
                a["password"] = str(ab["password"])[:200]
            if user != a["user"] and not ab.get("password"):
                a["password"] = "" if not user else a["password"]
            a["url"], a["user"] = url, user
            a["enabled"] = bool(ab.get("enabled", a["enabled"])) and bool(url)
        every = int(body.get("every") or self.cfg["every"])
        with self.lock:
            self.cfg["acs"] = a
            self.cfg["smartolt"] = s
            self.cfg["every"] = min(3600, max(120, every))
            _write_private(self.path, json.dumps(self.cfg, indent=1))
        self.details_at = 0
        self.wake.set()
        return self.settings()

    def test_smartolt(self, subdomain=None, key=None):
        s = self.cfg["smartolt"]
        c = self.factory(subdomain or s["subdomain"], key or s["key"])
        olts = c.rows(c.get("system/get_olts"))
        return {"olts": len(olts), "names": [str(o.get("name", "")) for o in olts][:20]}

    def test_acs(self, url=None, user=None, password=None):
        a = self.cfg["acs"]
        same = not url or url == a["url"]
        c = self.acs_factory(url or a["url"], a["user"] if user is None else user,
                             password if password else (a["password"] if same else ""))
        devs = c.devices()
        return {"devices": len(devs), "recent": sum(d["recent"] for d in devs), "models": sorted({d["model"] for d in devs if d["model"]})[:15]}

    # ---- reading ---------------------------------------------------------------
    def start(self):
        if self.thread:
            return
        self.thread = threading.Thread(target=self._loop, daemon=True, name="olt-fleet")
        self.thread.start()

    def _loop(self):
        while True:
            try:
                self.refresh()
            except Exception as exc:          # never let the reader die
                with self.lock:
                    self.snap["errors"] = [f"OLT reader: {exc}"]
            self.wake.wait(self.cfg["every"])
            self.wake.clear()

    def refresh(self):
        olts, onus, unconf, errors = [], [], [], []
        s = self.cfg["smartolt"]
        if s["enabled"] and s["subdomain"] and s["key"]:
            try:
                o, n, u = self._read_smartolt()
                olts += o; onus += n; unconf += u
            except FleetError as exc:
                errors.append(str(exc))
        for r in self.saved_olts():
            if not self.snmp_reader:
                break
            try:
                o, n = self._read_snmp(r)
                olts.append(o); onus += n
            except Exception as exc:
                errors.append(f"{r.get('name')}: {exc}")
                olts.append({"id": "snmp-" + r["id"], "name": r.get("name", ""), "source": "SNMP", "online": False,
                             "model": "", "uptime": "", "temp": None, "error": str(exc)[:200]})
        a = self.cfg["acs"]
        acs, acs_time = self.acs, self.snap.get("acs_time", 0)
        if a["enabled"] and a["url"]:
            try:
                acs = self.acs_factory(a["url"], a["user"], a["password"]).devices()
                acs_time = time.time()
            except FleetError as exc:
                errors.append(str(exc))
        else:
            acs = []
        by_sn = {}
        for d in acs:
            for k in sn_keys(d["serial"]):
                by_sn[k] = d
        now = time.time()
        for o in onus:
            o["signal"] = signal_level(o.get("rx"))
            d = next((by_sn[k] for k in sn_keys(o["sn"]) if k in by_sn), None)
            o["acs_id"] = d["id"] if d else ""
            if d:
                d["onu_key"] = o["key"]
        with self.lock:
            self.acs = acs
            self.snap = {"olts": olts, "onus": onus, "unconfigured": unconf, "errors": errors, "time": now, "acs_time": acs_time}
            self._record(onus, now)
        return self.snap

    def _read_smartolt(self):
        s = self.cfg["smartolt"]
        c = self.factory(s["subdomain"], s["key"])
        olts = []
        env = {}
        try:
            for e in c.rows(c.get("olt/get_olts_uptime_and_env_temperature")):
                env[str(e.get("olt_id", e.get("id", "")))] = e
        except FleetError:
            pass
        for o in c.rows(c.get("system/get_olts")):
            oid = str(o.get("id", ""))
            e = env.get(oid, {})
            olts.append({"id": "so-" + oid, "name": str(o.get("name", "")), "source": "SmartOLT", "online": True,
                         "model": str(o.get("olt_hardware_version", "") or ""), "ip": str(o.get("ip", "") or ""),
                         "uptime": str(e.get("uptime", "") or ""), "temp": _f(e.get("env_temp", e.get("temperature")))})
        names = {o["id"][3:]: o["name"] for o in olts}
        if time.time() - self.details_at > self.cfg["details_every"] or not self.details:
            try:
                det = c.rows(c.get("onu/get_all_onus_details"), "onus")
                self.details = {str(d.get("unique_external_id") or d.get("sn")): d for d in det}
                self.details_at = time.time()
            except FleetError:
                if not self.details:
                    raise
        statuses, signals = {}, {}
        try:
            for x in c.rows(c.get("onu/get_onus_statuses"), "onus"):
                statuses[str(x.get("unique_external_id") or x.get("sn"))] = x
        except FleetError:
            pass
        try:
            for x in c.rows(c.get("onu/get_onus_signals"), "onus"):
                signals[str(x.get("unique_external_id") or x.get("sn"))] = x
        except FleetError:
            pass
        onus = []
        for key, d in self.details.items():
            st, sg = statuses.get(key, {}), signals.get(key, {})
            olt_id = str(d.get("olt_id", ""))
            board, port, onu = str(d.get("board", "")), str(d.get("port", "")), str(d.get("onu", ""))
            onus.append({
                "key": "so-" + key, "ext_id": key, "sn": str(d.get("sn", "")), "olt_id": "so-" + olt_id,
                "olt": names.get(olt_id) or str(d.get("olt_name", "")), "board": board, "port": port, "onu": onu,
                "pon_type": str(d.get("pon_type", "gpon") or "gpon").upper(),
                "onu_path": f"{(d.get('pon_type') or 'gpon').lower()}-onu_0/{board}/{port}:{onu}",
                "name": str(d.get("name", "") or ""), "zone": str(d.get("zone_name", "") or ""),
                "address": str(d.get("address", "") or ""), "contact": str(d.get("contact", "") or ""),
                "type": str(d.get("onu_type_name", "") or ""), "mode": str(d.get("mode", "") or ""),
                "vlan": str(d.get("vlan", "") or ""), "auth_date": str(d.get("authorization_date", "") or ""),
                "status": _status(st.get("status", d.get("status"))), "since": str(st.get("last_status_change", "") or ""),
                "rx": _f(sg.get("signal_1490", d.get("signal_1490"))), "olt_rx": _f(sg.get("signal_1310", d.get("signal_1310"))),
                "distance_m": None, "down_cause": "", "lat": _f(d.get("latitude")), "lon": _f(d.get("longitude")),
                "source": "SmartOLT"})
        unconf = []
        try:
            for u in c.rows(c.get("onu/unconfigured_onus"), "onus"):
                oid = str(u.get("olt_id", ""))
                unconf.append({"olt_id": "so-" + oid, "olt": names.get(oid, ""), "pon_type": str(u.get("pon_type", "") or "").upper(),
                               "board": str(u.get("board", "")), "port": str(u.get("port", "")),
                               "pon_description": str(u.get("pon_description", "") or ""), "sn": str(u.get("sn", "")),
                               "type": str(u.get("onu_type_name", u.get("onu_type", "")) or ""),
                               "detected": str(u.get("detected", u.get("date", "")) or "")})
        except FleetError:
            pass
        return olts, onus, unconf

    def _read_snmp(self, r):
        dev = self.snmp_reader(r)
        try:
            data = dev.onus()
            ov = dev.overview()
        finally:
            try:
                dev.close()
            except Exception:
                pass
        oid = "snmp-" + r["id"]
        olt = {"id": oid, "name": r.get("name", ""), "source": "SNMP", "online": True, "model": ov.get("model", ""),
               "ip": r.get("host", ""), "uptime": ov.get("uptime", ""), "temp": ov.get("max_temp")}
        onus = []
        for o in data.get("onus", []):
            m = re.search(r"(\d+)/(\d+)/(\d+)\s*$", o["port"]) or re.search(r"(\d+)/(\d+)\s*$", o["port"])
            board, port = (m.group(m.re.groups - 1), m.group(m.re.groups)) if m else ("", o["port"])
            st = {"online": "online", "los": "los", "power off": "power fail"}.get(o["status"], "offline")
            onus.append({"key": f"{oid}-{o['port']}-{o['id']}", "ext_id": o["sn"], "sn": o["sn"], "olt_id": oid, "olt": r.get("name", ""),
                         "board": board, "port": port, "onu": str(o["id"]), "pon_type": "GPON", "onu_path": f"{o['port']}:{o['id']}",
                         "name": o.get("name", ""), "zone": "", "address": "", "contact": "", "type": "", "mode": "", "vlan": "",
                         "auth_date": "", "status": st, "since": "", "rx": o.get("rx_dbm"), "olt_rx": o.get("olt_rx_dbm"),
                         "distance_m": o.get("distance_m"), "down_cause": o.get("down_cause", ""), "lat": None, "lon": None,
                         "source": "SNMP"})
        return olt, onus

    def _record(self, onus, now):
        counts = {s: 0 for s in STATUSES}
        for o in onus:
            counts[o["status"] if o["status"] in counts else "n/a"] += 1
        self.history.append({"t": int(now), **counts})
        self.history = [h for h in self.history if h["t"] > now - HISTORY_S]
        try:
            _write_private(self.hist_path, json.dumps(self.history))
        except OSError:
            pass
        if now - self.rx_at >= RX_EVERY - 30:
            self.rx_at = now
            for o in onus:
                a = self.rx.get(o["key"])
                if a is None:
                    a = self.rx[o["key"]] = array("f")
                a.append(o["rx"] if o["rx"] is not None else float("nan"))
                if len(a) > RX_POINTS:
                    del a[0]

    # ---- views ----------------------------------------------------------------
    def summary(self):
        with self.lock:
            snap = self.snap
            onus = snap["onus"]
            c = {s: 0 for s in STATUSES}
            sig = {"good": 0, "warning": 0, "critical": 0, "none": 0}
            for o in onus:
                c[o["status"] if o["status"] in c else "n/a"] += 1
                sig[o["signal"]] += 1
            per_olt = {}
            for o in onus:
                p = per_olt.setdefault(o["olt_id"], {"total": 0, "online": 0})
                p["total"] += 1
                p["online"] += o["status"] == "online"
            olts = [{**o, **per_olt.get(o["id"], {"total": 0, "online": 0})} for o in snap["olts"]]
            return {"total": len(onus), "counts": c, "signal": sig, "waiting": len(snap["unconfigured"]),
                    "olts": olts, "outages": self._outages(onus), "errors": snap["errors"], "time": snap["time"],
                    "history": self.history[-2016:], "configured": bool(self.cfg["smartolt"]["enabled"] or self.saved_olts()),
                    "warn_dbm": WARN_DBM, "crit_dbm": CRIT_DBM,
                    "located": sum(o["lat"] is not None and o["lon"] is not None for o in onus),
                    "acs": {"on": bool(self.cfg["acs"]["enabled"]), "total": len(self.acs), "recent": sum(d["recent"] for d in self.acs),
                            "matched": sum(bool(o.get("acs_id")) for o in onus)}}

    @staticmethod
    def _outages(onus):
        """PON ports where many ONUs are down the same way: a full or partial PON LOS (fibre), or a power cut in the area."""
        ports = {}
        for o in onus:
            ports.setdefault((o["olt_id"], o["olt"], o["board"], o["port"]), []).append(o)
        out = []
        for (oid, olt, b, p), lst in ports.items():
            n = len(lst)
            los = sum(o["status"] == "los" for o in lst)
            pf = sum(o["status"] == "power fail" for o in lst)
            name = f"{olt} board {b} port {p}"
            if n >= 2 and los == n:
                out.append({"kind": "full", "what": "Full PON LOS", "port": name, "olt_id": oid, "board": b, "pon": p, "count": los, "total": n})
            elif los >= 3 and los >= n * 0.3:
                out.append({"kind": "partial", "what": "Partial LOS", "port": name, "olt_id": oid, "board": b, "pon": p, "count": los, "total": n})
            if n >= 4 and pf >= max(3, n * 0.5):
                out.append({"kind": "power", "what": "Power cut in the area", "port": name, "olt_id": oid, "board": b, "pon": p, "count": pf, "total": n})
        return sorted(out, key=lambda x: -x["count"])

    def onus(self, q="", olt="", board="", port="", status="", signal="", page=1, per=100, located=False):
        with self.lock:
            rows = self.snap["onus"]
        q = (q or "").strip().lower()

        def keep(o):
            if olt and o["olt_id"] != olt:
                return False
            if board and o["board"] != board:
                return False
            if port and o["port"] != port:
                return False
            if status and o["status"] != status:
                return False
            if signal and o["signal"] != signal:
                return False
            if located and (o["lat"] is None or o["lon"] is None):
                return False
            if q and q not in " ".join(str(o.get(k, "")) for k in ("name", "sn", "address", "zone", "olt", "onu_path", "type", "contact")).lower():
                return False
            return True
        sel = [o for o in rows if keep(o)]
        sel.sort(key=lambda o: (o["olt"], int(o["board"]) if o["board"].isdigit() else 0, int(o["port"]) if o["port"].isdigit() else 0,
                                int(o["onu"]) if o["onu"].isdigit() else 0))
        per = min(500, max(10, int(per or 100)))
        page = max(1, int(page or 1))
        return {"total": len(sel), "page": page, "per": per, "pages": max(1, -(-len(sel) // per)),
                "onus": sel[(page - 1) * per: page * per],
                "boards": sorted({o["board"] for o in rows if not olt or o["olt_id"] == olt}, key=lambda x: (len(x), x)),
                "ports": sorted({o["port"] for o in rows if (not olt or o["olt_id"] == olt) and (not board or o["board"] == board)}, key=lambda x: (len(x), x)),
                "olts": [{"id": o["id"], "name": o["name"]} for o in self.snap["olts"]]}

    def onu(self, key):
        with self.lock:
            o = next((x for x in self.snap["onus"] if x["key"] == key), None)
            if not o:
                return None
            a = self.rx.get(key, array("f"))
            t1 = self.rx_at
            hist = [[int(t1 - (len(a) - 1 - i) * RX_EVERY), round(v, 2)] for i, v in enumerate(a) if v == v]
            tr = next((d for d in self.acs if d["id"] == o.get("acs_id")), None) if o.get("acs_id") else None
            return {**o, "rx_history": hist, "tr069": tr, "acs_on": bool(self.cfg["acs"]["enabled"])}

    def map_points(self):
        """Every ONU with coordinates, small enough for thousands of points."""
        with self.lock:
            pts = [[o["key"], round(o["lat"], 6), round(o["lon"], 6), o["status"], o["signal"], o["name"], o["olt_id"], o["board"], o["port"]]
                   for o in self.snap["onus"] if o["lat"] is not None and o["lon"] is not None and (o["lat"] or o["lon"])]
            return {"points": pts, "fields": ["key", "lat", "lon", "status", "signal", "name", "olt_id", "board", "port"],
                    "olts": [{"id": o["id"], "name": o["name"]} for o in self.snap["olts"]], "total": len(self.snap["onus"]), "time": self.snap["time"]}

    def tr069(self, q=""):
        with self.lock:
            devs = list(self.acs)
            names = {o["key"]: o for o in self.snap["onus"]}
        q = (q or "").strip().lower()
        rows = []
        for d in devs:
            o = names.get(d.get("onu_key", ""))
            r = {**d, "onu_name": o["name"] if o else "", "onu_olt": o["olt"] if o else "", "onu_status": o["status"] if o else ""}
            if q and q not in " ".join(str(r.get(k) or "") for k in ("serial", "model", "maker", "onu_name", "pppoe_user", "wan_ip", "ssid")).lower():
                continue
            rows.append(r)
        rows.sort(key=lambda r: (not r["recent"], r["onu_name"] or r["serial"]))
        return {"on": bool(self.cfg["acs"]["enabled"]), "devices": rows[:3000], "total": len(rows), "time": self.snap.get("acs_time", 0)}

    def unconfigured(self):
        with self.lock:
            by = {}
            for o in self.snap["olts"]:
                by[o["id"]] = {"olt_id": o["id"], "olt": o["name"], "source": o["source"], "onus": []}
            for u in self.snap["unconfigured"]:
                by.setdefault(u["olt_id"], {"olt_id": u["olt_id"], "olt": u["olt"], "source": "SmartOLT", "onus": []})["onus"].append(u)
            return {"olts": list(by.values()), "time": self.snap["time"]}

    def smartolt_client(self):
        s = self.cfg["smartolt"]
        if not (s["enabled"] and s["subdomain"] and s["key"]):
            raise FleetError("SmartOLT is not set up.")
        return self.factory(s["subdomain"], s["key"])
