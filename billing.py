"""Splynx billing integration.

Matches Splynx customers (filtered by partner/reseller, location and router) to
router interfaces by IP address, interface description, or a manual mapping,
then keeps the interface state in line with the billing status:

  customer blocked / disabled in Splynx  ->  interface shutdown
  customer active again (after payment)  ->  undo shutdown

Only interfaces this module shut down are ever re-enabled, so ports an
engineer shut by hand are left alone. An interface shared by several
customers is only shut when all of them are blocked.
"""
import base64
import hashlib
import hmac
import ipaddress
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import parsers as P

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(HERE, "billing.json")
STATE_FILE = os.path.join(HERE, "billing_state.json")

DEFAULT_CONFIG = {
    "url": "", "api_key": "", "api_secret": "",
    "partner_ids": [], "location_ids": [], "router_id": 0,
    "block_statuses": ["blocked", "disabled"],
    "mode": "off",            # off | dry-run | enforce
    "interval": 120,          # seconds between syncs
    "match_description": True,
    "manual": {},             # customer_id -> interface name ("" = never match)
}


class BillingError(Exception):
    pass


def _load(path, default):
    try:
        with open(path) as f:
            data = json.load(f)
        return {**default, **data} if isinstance(default, dict) else data
    except (OSError, ValueError):
        return json.loads(json.dumps(default))


def _save(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


# ---- Splynx REST API v2 client ------------------------------------------------
class SplynxClient:
    def __init__(self, url, key, secret, timeout=30):
        if not url or not key or not secret:
            raise BillingError("Splynx URL, API key and API secret are required.")
        self.base = url.rstrip("/")
        if not self.base.startswith(("http://", "https://")):
            self.base = "https://" + self.base
        self.key, self.secret, self.timeout = key, secret, timeout
        self.token, self.token_exp, self.use_basic = None, 0, False

    def _auth_header(self):
        if self.use_basic:
            return "Basic " + base64.b64encode(f"{self.key}:{self.secret}".encode()).decode()
        if not self.token or time.time() > self.token_exp - 60:
            nonce = str(int(time.time() * 1000))
            sig = hmac.new(self.secret.encode(), (nonce + self.key).encode(), hashlib.sha256).hexdigest().upper()
            try:
                r = self._request("POST", "/api/2.0/admin/auth/tokens",
                                  {"auth_type": "api_key", "key": self.key, "signature": sig, "nonce": nonce}, auth=False)
                self.token = r["access_token"]
                self.token_exp = float(r.get("access_token_expiration") or time.time() + 1800)
            except BillingError:
                # API keys created with "Basic auth" enabled cannot request tokens
                self.use_basic = True
                return self._auth_header()
        return f"Splynx-EA (access_token={self.token})"

    def _request(self, method, path, body=None, params=None, auth=True):
        url = self.base + path + ("?" + urllib.parse.urlencode(params, doseq=True) if params else "")
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json", "Accept": "application/json"})
        if auth:
            req.add_header("Authorization", self._auth_header())
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:300]
            raise BillingError(f"Splynx {method} {path} failed: HTTP {exc.code} {detail}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise BillingError(f"Cannot reach Splynx at {self.base}: {exc}") from exc
        try:
            return json.loads(raw or b"null")
        except ValueError:
            raise BillingError(f"Splynx returned non-JSON for {path}")

    def get(self, path, params=None):
        return self._request("GET", path, params=params)

    def customers(self, partner_ids=(), location_ids=()):
        rows = []
        for pid in (sorted(partner_ids) or [None]):
            params = {"main_attributes[partner_id]": pid} if pid else None
            rows += self.get("/api/2.0/admin/customers/customer", params) or []
        return [c for c in rows if c.get("category") != "lead"
                and (not partner_ids or int(c.get("partner_id") or 0) in partner_ids)
                and (not location_ids or int(c.get("location_id") or 0) in location_ids)]

    def services(self):
        return self.get("/api/2.0/admin/customers/customer/0/internet-services") or []

    def partners(self):
        return self.get("/api/2.0/admin/administration/partners") or []

    def locations(self):
        return self.get("/api/2.0/admin/administration/locations") or []

    def routers(self):
        return self.get("/api/2.0/admin/networking/routers") or []


class StaticSource:
    """Billing data from memory (demo mode and tests)."""

    def __init__(self, customers, services, partners=(), locations=(), routers=()):
        self._c, self._s = customers, services
        self._p, self._l, self._r = list(partners), list(locations), list(routers)

    def customers(self, partner_ids=(), location_ids=()):
        return [c for c in self._c if (not partner_ids or int(c["partner_id"]) in partner_ids)
                and (not location_ids or int(c["location_id"]) in location_ids)]

    def services(self):
        return self._s

    def partners(self):
        return self._p

    def locations(self):
        return self._l

    def routers(self):
        return self._r


# ---- engine -----------------------------------------------------------------
class BillingEngine:
    def __init__(self, router, source_factory=None, config_file=CONFIG_FILE, state_file=STATE_FILE, defaults=None):
        self.router = router
        self.source_factory = source_factory
        self.config_file, self.state_file = config_file, state_file
        self.defaults = {**DEFAULT_CONFIG, **(defaults or {})}
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.last = {"time": None, "rows": [], "planned": [], "error": "", "counts": {}}
        self.thread = None

    # config & state
    def config(self):
        return _load(self.config_file, self.defaults)

    def save_config(self, cfg):
        _save(self.config_file, cfg)

    def state(self):
        return _load(self.state_file, {"shut_by_billing": {}, "log": []})

    def save_state(self, st):
        st["log"] = st["log"][-500:]
        _save(self.state_file, st)

    def source(self, cfg=None):
        cfg = cfg or self.config()
        if self.source_factory:
            return self.source_factory(cfg)
        return SplynxClient(cfg["url"], cfg["api_key"], cfg["api_secret"])

    # matching
    def interface_networks(self):
        """[(interface, ip_interface)] for every IPv4 address configured on the router."""
        out = []
        for name, lines in self.router.interface_config().items():
            if name.lower().startswith(("null", "loopback")):
                continue
            for l in lines:
                m = re.match(r"^ip address (\S+) (\S+)", l)
                if m:
                    try:
                        mask = m.group(2)
                        plen = int(mask) if mask.isdigit() else ipaddress.IPv4Network("0.0.0.0/" + mask).prefixlen
                        out.append((name, ipaddress.ip_interface(f"{m.group(1)}/{plen}")))
                    except ValueError:
                        pass
        return out

    @staticmethod
    def customer_networks(services):
        nets = []
        for s in services:
            if s.get("status") in ("hidden",):
                continue
            for field in ("ipv4", "ipv4_route"):
                for part in re.split(r"[,\s]+", s.get(field) or ""):
                    if part:
                        try:
                            nets.append(ipaddress.ip_network(part, strict=False))
                        except ValueError:
                            pass
        return nets

    def match(self, customers, services, interfaces, cfg):
        by_cust = {}
        router_id = int(cfg.get("router_id") or 0)
        for s in services:
            if router_id and int(s.get("router_id") or 0) not in (0, router_id):
                continue
            by_cust.setdefault(int(s["customer_id"]), []).append(s)
        ifnets = self.interface_networks()
        descs = {i["name"]: i.get("description", "") for i in interfaces}
        rows = []
        for c in customers:
            cid = int(c["id"])
            svcs = by_cust.get(cid, [])
            nets = self.customer_networks(svcs)
            iface, how = None, ""
            manual = cfg.get("manual", {}).get(str(cid))
            if manual is not None:
                iface, how = (manual or None), ("manual" if manual else "excluded")
            if not iface and how != "excluded":
                best = None
                for name, ipif in ifnets:
                    for n in nets:
                        if n.version == 4 and n.subnet_of(ipif.network) and n.network_address != ipif.ip:
                            if best is None or ipif.network.prefixlen > best[1].network.prefixlen:
                                best = (name, ipif)
                if best:
                    iface, how = best[0], f"IP in {best[1].network}"
            if not iface and how != "excluded" and cfg.get("match_description", True):
                tokens = {str(cid), str(c.get("login") or "")} - {""}
                for name, d in descs.items():
                    if any(len(t) >= 4 and re.search(r"(?<![\w.])" + re.escape(t) + r"(?![\w.])", d or "") for t in tokens):
                        iface, how = name, "description"
                        break
            rows.append({
                "id": cid, "name": c.get("name", ""), "login": c.get("login", ""), "status": c.get("status", ""),
                "partner_id": c.get("partner_id"), "location_id": c.get("location_id"),
                "plans": ", ".join(s.get("description", "") for s in svcs if s.get("status") != "hidden"),
                "ips": ", ".join(str(n.network_address) if n.num_addresses == 1 else str(n) for n in nets),
                "interface": iface, "match": how,
            })
        return rows

    def plan(self, rows, interfaces, cfg, st):
        blocked = set(cfg.get("block_statuses") or ["blocked", "disabled"])
        state = {i["name"]: i for i in interfaces}
        per_if = {}
        for r in rows:
            if r["interface"]:
                per_if.setdefault(r["interface"], []).append(r)
        actions = []
        for name, custs in per_if.items():
            i = state.get(name)
            if not i:
                for r in custs:
                    r["note"] = "interface not found on router"
                continue
            want_down = all(r["status"] in blocked for r in custs)
            shared = len(custs) > 1
            ours = name in st["shut_by_billing"]
            for r in custs:
                r["if_state"] = i["phy"]
                r["shared"] = shared
            if want_down and not i["admin_down"]:
                actions.append({"interface": name, "action": "shutdown", "customers": [r["id"] for r in custs],
                                "reason": ", ".join(f"{r['name']} is {r['status']}" for r in custs)})
            elif not want_down and i["admin_down"] and ours:
                actions.append({"interface": name, "action": "undo shutdown", "customers": [r["id"] for r in custs],
                                "reason": ", ".join(f"{r['name']} is {r['status']}" for r in custs if r["status"] not in blocked)})
            elif not want_down and i["admin_down"]:
                for r in custs:
                    r["note"] = "shut manually (not by billing), left alone"
            elif shared and any(r["status"] in blocked for r in custs):
                for r in custs:
                    r["note"] = "shared interface: kept up while another customer on it is active"
        for a in actions:
            for r in rows:
                if r["id"] in a["customers"]:
                    r["planned"] = a["action"]
        return actions

    def sync(self, apply=None):
        """Fetch Splynx, match, plan, and (in enforce mode, or apply=True) change the router."""
        with self.lock:
            cfg = self.config()
            st = self.state()
            apply = (cfg["mode"] == "enforce") if apply is None else apply
            try:
                src = self.source(cfg)
                customers = src.customers(set(map(int, cfg["partner_ids"])), set(map(int, cfg["location_ids"])))
                services = src.services()
            except BillingError as exc:
                self.last.update(time=time.time(), error=str(exc))
                raise
            interfaces = self.router.interfaces()
            rows = self.match(customers, services, interfaces, cfg)
            actions = self.plan(rows, interfaces, cfg, st)
            done = []
            for a in actions:
                entry = {"time": time.time(), "interface": a["interface"], "action": a["action"], "reason": a["reason"],
                         "customers": a["customers"], "applied": False, "result": "planned (dry run)"}
                if apply:
                    try:
                        self.router.configure([f"interface {a['interface']}", a["action"]])
                        entry.update(applied=True, result="ok")
                        if a["action"] == "shutdown":
                            st["shut_by_billing"][a["interface"]] = {"customers": a["customers"], "time": time.time()}
                        else:
                            st["shut_by_billing"].pop(a["interface"], None)
                    except Exception as exc:
                        entry["result"] = f"failed: {exc}"
                    st["log"].append(entry)
                done.append(entry)
            if apply:
                self.save_state(st)
                if done:  # show the state after the changes
                    interfaces = self.router.interfaces()
                    rows = self.match(customers, services, interfaces, cfg)
                    self.plan(rows, interfaces, cfg, st)
                    for r in rows:
                        r["planned"] = next((a["action"] + " (done)" for a in done if r["id"] in a["customers"] and a["applied"]), r.get("planned"))
            blocked = set(cfg["block_statuses"])
            self.last = {
                "time": time.time(), "error": "", "rows": rows, "planned": done, "applied": apply,
                "counts": {"customers": len(rows), "matched": sum(1 for r in rows if r["interface"]),
                           "unmatched": sum(1 for r in rows if not r["interface"] and r["match"] != "excluded"),
                           "blocked": sum(1 for r in rows if r["status"] in blocked),
                           "shut_by_billing": len(st["shut_by_billing"])},
            }
            return self.last

    # background loop
    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop.clear()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _loop(self):
        while not self.stop.is_set():
            cfg = self.config()
            if cfg["mode"] != "off" and (cfg["url"] or self.source_factory):
                try:
                    self.sync()
                except Exception as exc:
                    self.last["error"] = str(exc)
            self.stop.wait(max(15, int(cfg.get("interval") or 60)))

    def close(self):
        self.stop.set()


def public_config(cfg):
    c = dict(cfg)
    c["api_secret"] = "********" if cfg.get("api_secret") else ""
    return c


# ---- demo billing data ------------------------------------------------------------
def demo_source():
    """Sample Splynx data that lines up with the demo router's reseller sub-interfaces."""
    partners = [{"id": 1, "name": "Main Office"}, {"id": 13, "name": "RESELLERS/COOPORATE"}]
    locations = [{"id": 1, "name": "MARSABIT"}, {"id": 6, "name": "NAIROBI"}]
    routers = [{"id": 27, "title": "BNG-NDOVU1", "model": "NetEngine-8K-M8"}]

    def cust(i, name, status, login=None, partner=13, location=6):
        return {"id": i, "name": name, "login": login or str(i), "status": status, "category": "person",
                "partner_id": partner, "location_id": location}

    def svc(cid, plan, ip="", route="", router=27, status="active"):
        return {"customer_id": cid, "description": plan, "ipv4": ip, "ipv4_route": route, "router_id": router, "status": status}

    customers = [
        cust(552901, "AlphaNet Solutions", "active"), cust(552902, "BetaLink WiFi", "blocked"),
        cust(552903, "Gamma Cable", "active"), cust(552904, "Delta Hotspot", "active"),
        cust(552905, "Epsilon Net", "blocked"), cust(552906, "Zeta Internet", "active"),
        cust(553350, "TEST DUMMY - NE8000 GUI", "active", login="ne8000test"),
        cust(547001, "Home user (not a reseller)", "blocked", partner=1, location=1),
    ]
    services = [
        svc(552901, "COOPORATE 600 mbps", ip="102.216.117.2"), svc(552902, "COOPORATE 300 mbps", ip="102.216.117.6"),
        svc(552903, "COOPORATE 500 mbps", router=0), svc(552904, "400MBPS", ip="102.216.117.34"),
        svc(552905, "400MBPS", ip="102.216.117.35"), svc(552906, "2Gbps", router=0),
        svc(553350, "TEST 400MBPS", router=0), svc(547001, "3Mbps", ip="10.2.1.2", router=4),
    ]
    return StaticSource(customers, services, partners, locations, routers)


DEMO_DEFAULTS = {"partner_ids": [13], "location_ids": [6], "router_id": 27, "mode": "enforce", "interval": 20}
