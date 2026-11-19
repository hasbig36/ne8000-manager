"""NE8000 Manager: a Winbox-style web GUI for Huawei NetEngine 8000 M8 routers.

Run:  python app.py                          (http://127.0.0.1:5000)
      python app.py --host 0.0.0.0 --port 8080   (reachable from your LAN)
      python app.py --add-user NAME --role admin  (create or reset an app login)

Settings for hosting come from environment variables (see README, "Hosting").
"""
import argparse
import hmac
import json
import os
import datetime
import ipaddress
import re
import sys
import threading
import time
import uuid

from flask import Flask, Response, g, jsonify, render_template, request, session

import actions
import auth
import backups
import billing
import changes
import adopt
import health
import history
import ipdisabled
import netdesign
import netflow
import netmap
import panel
import plans
import policy
import routers
import ipam
import sitereg
import serverinfo
import alerts
import sitewatch
import viewcache
from device import DeviceError, RealRouter, _cidr, is_read_only
from snmp_router import SnmpRouter, SshRequired

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.abspath(os.environ.get("NE8000_DATA_DIR") or HERE)
os.makedirs(DATA_DIR, exist_ok=True)
env_on = lambda k, d="0": os.environ.get(k, d).strip().lower() in ("1", "true", "yes", "on")
ALLOW_DEMO = env_on("NE8000_ALLOW_DEMO", "1")
BEHIND_PROXY = env_on("NE8000_BEHIND_PROXY")

app = Flask(__name__)
app.secret_key = auth.secret_key(DATA_DIR)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_NAME="ne8000",
                  SESSION_COOKIE_SECURE=env_on("NE8000_SECURE_COOKIES", "1" if BEHIND_PROXY else "0"),
                  PERMANENT_SESSION_LIFETIME=int(os.environ.get("NE8000_SESSION_HOURS", "12")) * 3600,
                  MAX_CONTENT_LENGTH=4 * 1024 * 1024)

USERS = auth.UserStore(DATA_DIR)
AUDIT = auth.Audit(DATA_DIR)
SAVED = routers.RouterStore(DATA_DIR)
HIST = history.History(DATA_DIR, SAVED)
SITES = sitewatch.SiteWatch(DATA_DIR)
HEALTH = health.Health(DATA_DIR, SAVED, HIST)
FLOWS = netflow.FlowCollector(SAVED, HIST, default_sampling=os.environ.get("NE8000_NETFLOW_SAMPLING", "1000"))
NETMAP = netmap.NetMapStore(DATA_DIR)
NETMAP_CACHE = {}   # map key -> (time, response)
DISABLED_IPS = ipdisabled.DisabledIPs(DATA_DIR)
PANELS = panel.PanelStore(DATA_DIR)
NOTES = panel.NotesStore(DATA_DIR)
BACKUPS = backups.BackupStore(DATA_DIR)
VIEWCACHE = viewcache.ViewCache()   # router views kept in server memory, so pages open without waiting for the router
TRACK = changes.Tracker(DATA_DIR, SAVED)   # change log: ARP, MAC, routes, protocol sessions, BGP counts, policy counters
VIEWCACHE.observers.append(lambda r, name, value: TRACK.feed_view(getattr(r, "track_id", ""), name, value))
VIEWCACHE.observers.append(lambda r, name, value: daily_backup(r) if name == "track" else None)
THROTTLE_IP = auth.Throttle(limit=5)        # per client address
THROTTLE_USER = auth.Throttle(limit=20)     # per username, high enough that strangers cannot easily lock out a real user
THROTTLE_ROUTER = auth.Throttle(limit=5)    # failed router SSH logins per app user (slows password guessing on the router)


def allowed_routers():
    """Routers the app may connect to: the ones admins saved under Manager > Routers, plus
    NE8000_ALLOWED_ROUTERS (comma-separated IPs, subnets or hostnames). Both empty = any."""
    env = [x.strip() for x in os.environ.get("NE8000_ALLOWED_ROUTERS", "").split(",") if x.strip()]
    return env + [h for h in SAVED.hosts() if h not in env]


def check_router_allowed(host):
    allow = allowed_routers()
    if not allow:
        return
    import ipaddress
    for a in allow:
        if host.lower() == a.lower():
            return
        try:
            if ipaddress.ip_address(host) in ipaddress.ip_network(a, strict=False):
                return
        except ValueError:
            pass
    raise DeviceError("That router is not in the saved list. An admin can add it under Manager > Routers.")

ROUTERS = {}   # browser session id -> Router. Credentials are never stored.
NAME_RE = re.compile(r"^[A-Za-z0-9/.:\-_]{1,64}$")

VIEWS = ("overview", "interfaces", "vlans", "qos", "arp", "neighbors", "vpn_instances", "bgp", "ospf", "isis", "mpls",
         "system", "hardware", "logs", "route_policies")


def router():
    r = ROUTERS.get(session.get("sid"))
    if r is None:
        raise DeviceError("Not connected.")
    return r


def same_router(r):
    return [x for x in ROUTERS.values() if x is not r and not x.demo and not r.demo and x.host == r.host]


def start_billing(r):
    """Only one billing loop per router, however many people are connected to it. It needs SSH
    to change the router, so an SNMP-only session never runs it."""
    if not r.can_configure:
        return
    if not any(x.billing.thread and x.billing.thread.is_alive() and not x.billing.stop.is_set() for x in same_router(r)):
        r.billing.start()


def close_router(r):
    was_running = bool(getattr(r, "billing", None) and r.billing.thread and r.billing.thread.is_alive() and not r.billing.stop.is_set())
    if getattr(r, "billing", None):
        r.billing.close()
    r.close()
    others = [x for x in same_router(r) if x.can_configure]
    if was_running and others:
        others[0].billing.start()


# ---- app login ---------------------------------------------------------------------
OPEN = {"index", "static", "auth_login", "auth_me", "healthz", "otdr_event", "site_event"}     # otdr_event checks its own token or login
# endpoints a viewer may POST to; everything else that changes something needs operator or admin
VIEWER_POST = {"connect", "connect_ssh", "disconnect", "cli", "run_action", "auth_logout", "auth_password", "sites_check", "sites_run", "ipam_layout", "ipam_renumber"}
ADMIN_ONLY = {"alerts_channel_add", "alerts_channel_delete", "alerts_test", "alerts_rules", "alerts_settings", "alerts_check_save", "alerts_check_delete",
              "users_list", "users_add", "users_update", "users_delete", "audit_log", "routers_add", "routers_update", "routers_delete",
              "routers_test_snmp", "adopt_discover", "adopt_scan", "adopt_build", "adopt_apply", "adopt_identify"}


def current_user():
    name = session.get("user")
    u = USERS.get(name) if name else None
    if not u or session.get("pwc") != u["pw_changed"]:
        return None
    return u


def me():
    return current_user()["username"]


def client_ip():
    return request.remote_addr or "?"


@app.before_request
def started():
    g.t0 = time.time()


@app.before_request
def guard():
    if request.endpoint in OPEN:
        return None
    u = current_user()
    if not u:
        return jsonify(error="Please log in.", login=True), 401
    if request.method != "GET":
        if request.headers.get("X-Requested-With") != "ne8000":
            return jsonify(error="Missing request header."), 403
        if u["role"] == "viewer" and request.endpoint not in VIEWER_POST:
            return jsonify(error="Your account is read-only."), 403
    if request.endpoint in ADMIN_ONLY and u["role"] != "admin":
        return jsonify(error="Only admins can do this."), 403
    return None




@app.after_request
def security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    resp.headers.setdefault("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
    # a change made on the router through the app (a commit, a reset, a restore) or a new login: the stored views are
    # out of date. Reviews, Check only, notes, backups, billing and the like leave them alone, so pages stay instant.
    if request.method == "POST" and resp.status_code < 400 and request.path.startswith("/api/"):
        r = ROUTERS.get(session.get("sid"))
        if r is not None and (request.endpoint in ("connect", "connect_ssh") or getattr(r, "changed_at", 0) >= getattr(g, "t0", 0)):
            VIEWCACHE.invalidate(r)
    if request.path.startswith("/api/") and request.path != "/api/panel/photo":   # the photo is versioned (?v=) and may be cached
        resp.headers["Cache-Control"] = "no-store"
    if request.is_secure:
        resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    return resp


@app.get("/healthz")
def healthz():
    return jsonify(ok=True)


@app.get("/api/auth/me")
def auth_me():
    u = current_user()
    if not u:
        return jsonify(user=None)
    return jsonify(user={"username": u["username"], "role": u["role"]}, allow_demo=ALLOW_DEMO, routers=SAVED.list(),
                   default_host=os.environ.get("NE8000_DEFAULT_HOST", ""), default_port=os.environ.get("NE8000_DEFAULT_PORT", "22"))


@app.post("/api/auth/login")
def auth_login():
    if request.headers.get("X-Requested-With") != "ne8000":
        return jsonify(error="Missing request header."), 403
    d = request.get_json(force=True)
    name, ip = (d.get("username") or "").strip(), client_ip()
    THROTTLE_IP.check(ip)
    THROTTLE_USER.check(name.lower())
    u = USERS.verify(name, d.get("password") or "")
    if not u:
        THROTTLE_IP.fail(ip)
        THROTTLE_USER.fail(name.lower())
        AUDIT.log(name, "login_failed", ip=ip)
        time.sleep(0.5)
        raise auth.AuthError("Wrong username or password.")
    THROTTLE_IP.ok(ip)
    THROTTLE_USER.ok(name.lower())
    old = ROUTERS.pop(session.get("sid"), None)
    if old:
        close_router(old)
    session.clear()
    session.permanent = True
    session["user"], session["pwc"] = u["username"], u["pw_changed"]
    AUDIT.log(u["username"], "login", ip=ip)
    return jsonify(user={"username": u["username"], "role": u["role"]})


@app.post("/api/auth/logout")
def auth_logout():
    r = ROUTERS.pop(session.get("sid"), None)
    if r:
        close_router(r)
    AUDIT.log(me(), "logout", ip=client_ip())
    session.clear()
    return jsonify(ok=True)


@app.post("/api/auth/password")
def auth_password():
    d = request.get_json(force=True)
    name = me()
    if not USERS.verify(name, d.get("current") or ""):
        raise auth.AuthError("Your current password is wrong.")
    USERS.update(name, password=d.get("new"))
    session["pwc"] = USERS.get(name)["pw_changed"]  # keep this browser signed in; other sessions are signed out
    AUDIT.log(name, "password_changed", ip=client_ip())
    return jsonify(ok=True)


@app.get("/api/users")
def users_list():
    return jsonify(users=USERS.list(), roles=auth.ROLES)


@app.post("/api/users")
def users_add():
    d = request.get_json(force=True)
    USERS.add(d.get("username"), d.get("password"), d.get("role"))
    AUDIT.log(me(), "user_added", target=d.get("username"), role=d.get("role"))
    return jsonify(users=USERS.list())


@app.put("/api/users/<name>")
def users_update(name):
    d = request.get_json(force=True)
    USERS.update(name, password=d.get("password") or None, role=d.get("role") or None)
    AUDIT.log(me(), "user_changed", target=name, role=d.get("role"), password=bool(d.get("password")))
    return jsonify(users=USERS.list())


@app.delete("/api/users/<name>")
def users_delete(name):
    if name == me():
        raise auth.AuthError("You cannot delete your own account.")
    USERS.delete(name)
    AUDIT.log(me(), "user_deleted", target=name)
    return jsonify(users=USERS.list())


@app.get("/api/routers")
def routers_list():
    return jsonify(routers=SAVED.list())


def need_site(d, required=True):
    """A device is attached to a site that is fully registered (Sites)."""
    sid = str(d.get("site") or "").strip()
    if not sid:
        if required:
            raise DeviceError("Pick the site the device is at. Register the site under Sites first.")
        return ""
    s = SITEREG.get(sid)
    if not s:
        raise DeviceError("That site is not registered (Sites).")
    m = sitereg.missing(s)
    if m:
        raise DeviceError(f"Finish registering the site {s['name']} first: {', '.join(m)}.")
    return s["id"]


@app.post("/api/routers")
def routers_add():
    d = request.get_json(force=True) or {}
    d["site"] = need_site(d)
    r = SAVED.add(d)
    AUDIT.log(me(), "router_added", name=r["name"], host=r["host"], port=r["port"])
    return jsonify(routers=SAVED.list())


@app.put("/api/routers/<rid>")
def routers_update(rid):
    d = request.get_json(force=True) or {}
    d["site"] = need_site(d, required=False)       # routers saved before sites existed can be edited, and given a site
    r = SAVED.update(rid, d)
    AUDIT.log(me(), "router_changed", name=r["name"], host=r["host"], port=r["port"])
    return jsonify(routers=SAVED.list())


@app.post("/api/routers/<rid>/test-snmp")
def routers_test_snmp(rid):
    saved = SAVED.get(rid)
    if not saved or not saved.get("snmp"):
        raise DeviceError("No SNMP login saved for this router.")
    from snmp import SnmpClient, SnmpError, text
    s = saved["snmp"]
    try:
        c = SnmpClient(saved["host"], s["user"], s["auth_proto"], s["auth_key"], s["priv_proto"], s["priv_key"], port=s["port"])
        try:
            got = c.get("1.3.6.1.2.1.1.5.0", "1.3.6.1.2.1.1.3.0")
        finally:
            c.close()
    except SnmpError as exc:
        raise DeviceError(f"SNMP failed: {exc}")
    name = text(got.get("1.3.6.1.2.1.1.5.0"))
    if not name:
        raise DeviceError("SNMP answered, but without a system name.")
    return jsonify(ok=True, sysname=name, uptime_days=(got.get("1.3.6.1.2.1.1.3.0") or 0) // 8640000)


@app.delete("/api/routers/<rid>")
def routers_delete(rid):
    r = SAVED.delete(rid)
    AUDIT.log(me(), "router_deleted", name=r["name"], host=r["host"])
    return jsonify(routers=SAVED.list())


# ---- adopt new devices ---------------------------------------------------------------
def adopt_demo():
    r = ROUTERS.get(session.get("sid"))
    return bool(r and r.demo)


@app.get("/api/adopt/discover")
def adopt_discover():
    """LLDP neighbours of every saved router with an SNMP login."""
    known = set(SAVED.hosts())
    if adopt_demo():
        return jsonify(devices=adopt.demo_lldp(known), errors=[], demo=True)
    from snmp import SnmpClient
    rows, errors = [], []
    for r in SAVED.list():
        full = SAVED.get(r["id"])
        s = full.get("snmp")
        if not s:
            continue
        try:
            c = SnmpClient(full["host"], s["user"], s["auth_proto"], s["auth_key"], s["priv_proto"], s["priv_key"], port=s["port"], timeout=5)
            try:
                rows += adopt.lldp_candidates(c, r["name"], known)
            finally:
                c.close()
        except Exception as exc:
            errors.append(f"{r['name']}: {str(exc)[:160]}")
    return jsonify(devices=rows, errors=errors, demo=False)


@app.post("/api/adopt/scan")
def adopt_scan():
    d = request.get_json(force=True)
    try:
        port = int(d.get("port") or 22)
    except ValueError:
        raise DeviceError("Invalid SSH port.")
    demo = adopt_demo()
    try:
        rows = adopt.scan(d.get("subnet") or "", port, set(SAVED.hosts()), probe=adopt.demo_probe if demo else adopt.ssh_banner)
    except adopt.AdoptError as exc:
        raise DeviceError(str(exc))
    AUDIT.log(me(), "adopt_scan", subnet=str(d.get("subnet"))[:40], found=len(rows), ip=client_ip())
    return jsonify(devices=rows, demo=demo)


def adopt_target(d):
    host = (d.get("host") or "").strip()
    if not re.match(r"^[0-9.]{7,15}$", host):
        raise DeviceError("Invalid device address.")
    env = [x.strip() for x in os.environ.get("NE8000_ALLOWED_ROUTERS", "").split(",") if x.strip()]
    if env:
        import ipaddress
        if not any(host == a or (re.match(r"^[0-9./]+$", a) and ipaddress.ip_address(host) in ipaddress.ip_network(a, strict=False)) for a in env):
            raise DeviceError("That address is outside NE8000_ALLOWED_ROUTERS on this server.")
    try:
        port = int(d.get("port") or 22)
    except ValueError:
        raise DeviceError("Invalid SSH port.")
    return host, port


@app.post("/api/adopt/identify")
def adopt_identify():
    d = request.get_json(force=True)
    host, port = adopt_target(d)
    THROTTLE_ROUTER.check(me())
    try:
        res = adopt.identify(host, port, d.get("username") or "", d.get("password") or "",
                             connect=adopt.demo_connect if adopt_demo() else adopt.ssh_connect)
    except adopt.AdoptError as exc:
        if "Could not log in" in str(exc):
            THROTTLE_ROUTER.fail(me())
        raise DeviceError(str(exc))
    THROTTLE_ROUTER.ok(me())
    return jsonify(res)


@app.post("/api/adopt/build")
def adopt_build():
    d = request.get_json(force=True)
    try:
        lines = adopt.build(d.get("fields") or {}, d.get("platform") or "")
    except (adopt.AdoptError, actions.ActionError) as exc:
        raise DeviceError(str(exc))
    return jsonify(text="\n".join(lines))


@app.post("/api/adopt/apply")
def adopt_apply():
    d = request.get_json(force=True)
    f = d.get("fields") or {}
    host, port = adopt_target(d)
    commit = bool(d.get("commit"))
    demo = adopt_demo()
    name = (f.get("hostname") or "").strip()
    if commit and not demo and any(r["name"].lower() == name.lower() for r in SAVED.list()):
        raise DeviceError(f"A saved router is already called {name}. Pick another hostname.")
    site = need_site(f) if commit and not demo else ""       # it is saved under Manager > Routers at a registered site
    THROTTLE_ROUTER.check(me())
    try:
        res = adopt.apply(host, port, d.get("username") or "", d.get("password") or "", d.get("text") or "", d.get("platform") or "",
                          commit, ((f.get("admin_user") or "").strip(), f.get("admin_password") or ""),
                          connect=adopt.demo_connect if demo else adopt.ssh_connect)
    except adopt.AdoptError as exc:
        if "Could not log in" in str(exc):
            THROTTLE_ROUTER.fail(me())
        AUDIT.log(me(), "adopt_failed", host=host, commit=commit, error=str(exc)[:200], ip=client_ip())
        raise DeviceError(str(exc))
    THROTTLE_ROUTER.ok(me())
    if not commit:
        AUDIT.log(me(), "adopt_checked", host=host, ok=res["ok"], ip=client_ip())
        return jsonify(res)
    AUDIT.log(me(), "adopt_applied", host=host, final=res["host"], name=name, platform=res["platform"], ip=client_ip())
    saved = None
    if demo:
        res["warnings"].append("Demo mode: the device was not added to Manager > Routers.")
    else:
        try:
            saved = SAVED.add({"name": name, "host": res["host"], "port": port, "username": (f.get("admin_user") or "").strip(),
                               "notes": f"Adopted {time.strftime('%Y-%m-%d')} {res.get('model', '')}"[:200],
                               "snmp_user": f.get("snmp_user") or "", "snmp_auth_proto": f.get("snmp_auth_proto"), "snmp_auth_key": f.get("snmp_auth_key"),
                               "snmp_priv_proto": f.get("snmp_priv_proto"), "snmp_priv_key": f.get("snmp_priv_key"), "site": site})
            AUDIT.log(me(), "router_added", name=saved["name"], host=saved["host"], port=saved["port"])
        except Exception as exc:
            res["warnings"].append(f"Configured, but not added to Manager > Routers: {exc}")
    res["saved"] = saved
    return jsonify(res)


@app.get("/api/audit")
def audit_log():
    return jsonify(events=AUDIT.tail(int(request.args.get("n", 300))))


def safe_name(value, label="name"):
    if value and not NAME_RE.match(value):
        raise DeviceError(f"Invalid {label}.")
    return value


@app.errorhandler(DeviceError)
@app.errorhandler(actions.ActionError)
@app.errorhandler(billing.BillingError)
@app.errorhandler(plans.PlanError)
@app.errorhandler(auth.AuthError)
def handle_error(exc):
    if getattr(exc, "ssh_required", False):
        return jsonify(error=str(exc), ssh_required=True), 400
    return jsonify(error=str(exc)), 400


@app.errorhandler(Exception)
def handle_unexpected(exc):
    from werkzeug.exceptions import HTTPException
    if isinstance(exc, HTTPException):
        return jsonify(error=exc.description), exc.code
    app.logger.exception("Unexpected error")
    return jsonify(error=f"Server error: {exc}"), 500


@app.get("/")
def index():
    return render_template("index.html")


@app.post("/api/connect")
def connect():
    data = request.get_json(force=True)
    warning = ""
    if data.get("demo"):
        if not ALLOW_DEMO:
            raise DeviceError("Demo mode is turned off on this server.")
        from demo import DemoRouter
        r = DemoRouter((data.get("host") or "").strip() or "demo")
    else:
        saved = SAVED.get(data.get("router_id")) if data.get("router_id") else None
        if data.get("router_id") and not saved:
            raise DeviceError("That saved router no longer exists. Reload the page.")
        if saved:
            data["host"], data["port"] = saved["host"], saved["port"]
        snmp_login = saved.get("snmp") if saved else None
        host = (data.get("host") or "").strip()
        if not host:
            raise DeviceError("Host is required.")
        if len(host) > 253 or not re.match(r"^[A-Za-z0-9.:\-\[\]]+$", host):
            raise DeviceError("Invalid host.")
        try:
            port = int(data.get("port") or 22)
        except ValueError:
            raise DeviceError("Invalid SSH port.")
        if not 1 <= port <= 65535:
            raise DeviceError("Invalid SSH port.")
        check_router_allowed(host)
        username, password = data.get("username", ""), data.get("password", "")
        r = None
        if snmp_login and not data.get("ssh_only"):
            # SNMP first: no router password needed for monitoring. SSH joins if a login was typed.
            try:
                r = SnmpRouter(host, snmp_login, port)
            except DeviceError as exc:
                AUDIT.log(me(), "router_snmp_failed", host=host, error=str(exc)[:200], ip=client_ip())
                if not (username and password):
                    raise SshRequired(f"{exc}. Enter your SSH login to connect over SSH instead.")
                warning = f"{exc}. Connected over SSH instead."
        if r is None or (username and password):
            THROTTLE_ROUTER.check(me())
            try:
                if r is None:
                    r = RealRouter(host, username, password, port)
                else:
                    r.attach_ssh(username, password)
            except DeviceError as exc:
                THROTTLE_ROUTER.fail(me())
                AUDIT.log(me(), "router_connect_failed", host=host, ip=client_ip(), via="ssh", why=fail_reason(exc, password))
                if r is None:
                    raise
                warning = f"Connected over SNMP, but the SSH login failed: {exc}"
            else:
                THROTTLE_ROUTER.ok(me())
    old = ROUTERS.pop(session.get("sid"), None)
    if old:
        close_router(old)
    if r.demo:
        import tempfile
        tmp = tempfile.mkdtemp(prefix="ne8000-demo-billing-")
        src = billing.demo_source()
        r.billing_demo_source = src
        r.billing = billing.BillingEngine(r, lambda cfg: src, os.path.join(tmp, "billing.json"),
                                          os.path.join(tmp, "billing_state.json"), billing.DEMO_DEFAULTS)
    else:
        r.billing = billing.BillingEngine(r, config_file=os.path.join(DATA_DIR, "billing.json"),
                                          state_file=os.path.join(DATA_DIR, "billing_state.json"))
    session["sid"] = uuid.uuid4().hex
    session["router_id"] = "" if r.demo else (data.get("router_id") or "")
    ROUTERS[session["sid"]] = r
    r.track_id = netmap_key(r)
    VIEWCACHE.start(r)    # start filling the views now, so the dashboard opens from memory
    netmap_background(r, netmap_key(r))     # the map takes the longest to read: start it now too
    start_billing(r)
    AUDIT.log(me(), "router_connect", host=r.host, demo=r.demo, via=r.via, ip=client_ip())
    return jsonify(ok=True, host=r.host, demo=r.demo, via=r.via, warning=warning)


@app.post("/api/connect/ssh")
def connect_ssh():
    """Add an SSH login to an SNMP session, for the console, configuration and changes."""
    r = router()
    if not isinstance(r, SnmpRouter):
        return jsonify(ok=True, via=r.via)
    data = request.get_json(force=True)
    if not data.get("username") or not data.get("password"):
        raise SshRequired("Enter your router username and password.")
    THROTTLE_ROUTER.check(me())
    try:
        r.attach_ssh(data["username"], data["password"])
    except DeviceError as exc:
        THROTTLE_ROUTER.fail(me())
        AUDIT.log(me(), "router_connect_failed", host=r.host, ip=client_ip(), via="ssh", why=fail_reason(exc, data.get("password")))
        raise
    THROTTLE_ROUTER.ok(me())
    netmap_background(r, netmap_key(r))
    start_billing(r)
    AUDIT.log(me(), "router_ssh_added", host=r.host, ip=client_ip())
    return jsonify(ok=True, via=r.via)


@app.post("/api/disconnect")
def disconnect():
    r = ROUTERS.pop(session.pop("sid", None), None)
    if r:
        close_router(r)
        AUDIT.log(me(), "router_disconnect", host=r.host)
    return jsonify(ok=True)


@app.get("/api/status")
def status():
    r = ROUTERS.get(session.get("sid"))
    return jsonify(connected=r is not None, host=r.host if r else None, demo=r.demo if r else False,
                   via=r.via if r else None, ssh=bool(r and r.can_configure), router_id=session.get("router_id", "") if r else "")


@app.get("/api/view/<name>")
def view(name):
    if name not in VIEWS:
        raise DeviceError("Unknown view.")
    data, age = VIEWCACHE.get(router(), name)
    if name == "system" and current_user()["role"] == "viewer":
        data = {**data, "snmp": "(hidden for read-only accounts)"}
    resp = jsonify(data)
    resp.headers["X-Data-Age"] = f"{age:.1f}"
    return resp


@app.get("/api/resources")
def resources():
    return jsonify(router().resources(float(request.args.get("since", 0))))


@app.get("/api/interface")
def interface_detail():
    return jsonify(router().interface_detail(safe_name(request.args["name"], "interface")))


@app.get("/api/traffic")
def traffic():
    names = [safe_name(n, "interface") for n in request.args.get("names", "").split(",") if n][:8]
    return jsonify(router().traffic(names, float(request.args.get("since", 0))))


@app.get("/api/queues")
def queues():
    return jsonify(router().queue_stats(safe_name(request.args["name"], "interface")))


@app.get("/api/routes")
def routes():
    return jsonify(router().routes(safe_name(request.args.get("vpn") or None, "VPN instance"), route_lookup()))


def route_lookup():
    """?lookup=8.8.8.8 or 8.8.8.0/24 (validated: it goes into a router command)."""
    v = (request.args.get("lookup") or "").strip()
    if v and not re.match(r"^\d{1,3}(\.\d{1,3}){3}(/\d{1,2})?$", v):
        raise DeviceError("Lookup takes an IPv4 address or prefix, e.g. 8.8.8.8 or 8.8.8.0/24.")
    return v or None


@app.get("/api/bgp/routes")
def bgp_routes():
    peer = request.args.get("peer") or None
    if peer and not re.match(r"^[\d.:a-fA-F]+$", peer):
        raise DeviceError("Invalid peer.")
    return jsonify(router().bgp_routes(peer, request.args.get("which", "received"), route_lookup()))


@app.post("/api/action")
def run_action():
    return jsonify(do_action(request.get_json(force=True)))


def _yes(v):
    return v in (True, "true", "1")


def guard_primary(data):
    """"ip address X M" without "sub" REPLACES the interface's primary address on VRP. Adding an address must never
    do that by accident: refuse unless the interface has no primary yet, or the caller says replace_primary."""
    if data.get("action") != "interface.ip_add":
        return
    p = data.get("params") or {}
    if _yes(p.get("secondary")) or _yes(p.get("replace_primary")) or (p.get("vpn") or "").strip():
        return
    name = str(p.get("interface") or "")
    try:
        lines = router().interface_config().get(name, [])
    except Exception:
        raise actions.ActionError(f"Could not read the addresses on {name} to check for a primary. "
                                  "Tick Secondary (sub), or try again.")
    prim = [l for l in lines if l.startswith("ip address ") and len(l.split()) >= 4 and not l.endswith(" sub")]
    if prim:
        cur = _cidr(prim[0])
        if cur.split("/")[0] == str(p.get("address") or "").split("/")[0]:
            return
        raise actions.ActionError(
            f"{name} already has {cur} as its primary address. Without Secondary (sub) the new address would replace it "
            f"and cut off whatever uses {cur}. Tick Secondary (sub) to add it beside the primary. "
            f"To replace the primary on purpose, use Change on {cur}.")


def guard_policy(data):
    """Never delete a route-policy or prefix list that is in use: a BGP peer pointing at a policy that is gone, or a
    policy matching on a prefix list that is gone, changes what is accepted or announced in ways nobody intended."""
    act = data.get("action", "")
    if act not in ("policy.remove", "policy.node_remove", "prefix.remove", "prefix.entry_remove"):
        return
    p = data.get("params") or {}
    name = str(p.get("name") or "")
    d, _ = VIEWCACHE.get(router(), "route_policies")
    if act.startswith("policy."):
        users = [f"{peer} {k}" for peer, x in d["peers"].items() for k in ("import", "export") if x.get(k) == name]
        pol = next((x for x in d["policies"] if x["name"] == name), None)
        last = act == "policy.node_remove" and pol is not None and len(pol["nodes"]) <= 1
        if users and (act == "policy.remove" or last):
            raise actions.ActionError(f"{name} is used by BGP peer {', '.join(users)}. Take it off "
                                      f"{'those peers' if len(users) > 1 else 'that peer'} first"
                                      + (" (removing its last rule would delete the policy)." if last else "."))
        return
    using = sorted({x["name"] for x in d["policies"] for n in x["nodes"] for c in n["match"]
                    if re.match(rf"^if-match ip-prefix {re.escape(name)}$", c)})
    plist = d["prefixes"].get(name)
    last = act == "prefix.entry_remove" and plist is not None and len(plist["entries"]) <= 1
    if using and (act == "prefix.remove" or last):
        raise actions.ActionError(f"Prefix list {name} is used by route-policy {', '.join(using)}. Change those rules first"
                                  + (" (removing its last entry would delete the list)." if last else "."))


def do_action(data):
    built = actions.build(data.get("action", ""), data.get("params") or {})
    guard_primary(data)
    guard_policy(data)
    if data.get("preview"):
        return dict(lines=built["preview"], mode=built["mode"])
    if current_user()["role"] == "viewer":
        raise auth.AuthError("Your account is read-only.")
    r = router()
    AUDIT.log(me(), "action", host=r.host, demo=r.demo, action=data.get("action"), commit=data.get("commit", True),
              lines=built["preview"])
    if built["mode"] == "exec":
        out = "\n".join(r.execute(c) for c in built["lines"])
    else:
        out = r.configure(built["lines"], commit=data.get("commit", True))
        if data.get("commit", True):
            auto_backup(r, "after-change", me(), data.get("action", ""))
    for secret in [str(v) for k, v in (data.get("params") or {}).items() if k in actions.SECRET_KEYS and v]:
        out = out.replace(secret, "********")
    done = built["mode"] == "config" and bool(data.get("commit", True))
    return dict(output=out, lines=built["preview"], committed=done, commit_id=commit_id(r) if done else "", time=time.time())


def commit_id(r):
    """The router's id of the newest commit (display configuration commit list 1), for the "applied" note. "" if unknown."""
    try:
        m = re.search(r"^\s*1\s+(\d{6,})\s", r.run("display configuration commit list 1", read_timeout=20), re.M)
        return m.group(1) if m else ""
    except Exception:
        return ""


@app.get("/api/ip/disabled")
def ip_disabled():
    return jsonify(DISABLED_IPS.get(netmap_key(router())))


@app.post("/api/ip/toggle")
def ip_toggle():
    """Disable (remove from the router, remember here) or Enable (add back) one address."""
    data = request.get_json(force=True)
    p = data.get("params") or {}
    if data.get("action") not in ("interface.ip_remove", "interface.ip_add"):
        raise actions.ActionError("Only add or remove an address here.")
    if data.get("preview"):
        return jsonify(do_action(data))
    res = do_action(data)
    if data.get("commit", True):
        r = router()
        name, ip = p.get("interface", ""), str(p.get("address", ""))
        if data["action"] == "interface.ip_remove":
            DISABLED_IPS.disable(netmap_key(r), name, ip, p.get("secondary") in (True, "true", "1"), me(), str(p.get("reason") or ""))
        else:
            DISABLED_IPS.enable(netmap_key(r), name, ip)
        AUDIT.log(me(), "ip_disable" if data["action"] == "interface.ip_remove" else "ip_enable", host=r.host, demo=r.demo,
                  interface=name, address=ip, reason=str(p.get("reason") or ""))
    return jsonify(res)


@app.post("/api/cli")
def cli():
    command = (request.get_json(force=True).get("command") or "").strip()
    if not is_read_only(command):
        raise DeviceError("The console runs display, ping and tracert only. Use Config editor for changes.")
    if current_user()["role"] == "viewer" and "configuration" in command.lower():
        raise auth.AuthError("Viewers cannot read the full configuration.")
    r = router()
    r.check_table_size(command)
    return jsonify(output=r.run(command, read_timeout=120))


@app.post("/api/config")
def config():
    data = request.get_json(force=True)
    r = router()
    lines = (data.get("config") or "").splitlines()
    AUDIT.log(me(), "config", host=r.host, demo=r.demo, commit=bool(data.get("commit", True)), lines=len(lines))
    out = r.configure(lines, commit=bool(data.get("commit", True)))
    done = bool(data.get("commit", True))
    if done:
        auto_backup(r, "after-change", me(), "config editor")
    return jsonify(output=out, committed=done, commit_id=commit_id(r) if done else "", time=time.time())


@app.post("/api/save")
def save():
    r = router()
    AUDIT.log(me(), "save", host=r.host, demo=r.demo)
    out = r.execute("save")
    auto_backup(r, "after-save", me())
    return jsonify(output=out, saved="successfully" in out.lower() or "succeeded" in out.lower(), time=time.time())


# ---- configuration backups on the server ----------------------------------------------------------------------
BACKUP_BUSY = {}      # router key -> Lock: one automatic copy at a time per router


def fail_reason(exc, password=""):
    """Why a router login failed, for the audit log: one line, never the password."""
    t = " ".join(str(exc).split())[:300]
    return t.replace(password, "********") if password else t


def backup_key(r):
    return getattr(r, "track_id", "") or netmap_key(r)


def running_text(r):
    return r.run("display current-configuration", read_timeout=180)


def auto_backup(r, kind, by, note="", wait=False):
    """Keep a copy of the running configuration on the server, in the background (or now with wait=True).
    Skipped when it is the same as the newest copy, so only real changes add to the list."""
    if not r.can_configure:
        return None
    key = backup_key(r)

    def work():
        lock = BACKUP_BUSY.setdefault(key, threading.Lock())
        with lock:
            try:
                text = running_text(r)
                last = BACKUPS.last(key)
                if last and kind != "before-restore" and backups.clean(BACKUPS.text(key, last["id"])) == backups.clean(text):
                    return last
                return BACKUPS.add(key, text, by, kind, note, host=r.host)
            except Exception as exc:          # a copy must never break the change it follows
                print(f"backup ({kind}) failed: {exc}", flush=True)
                if wait:
                    raise
                return None
    if wait:
        return work()
    threading.Thread(target=work, daemon=True).start()
    return None


def daily_backup(r):
    """Once a day while someone is signed in with SSH (the router's password is never stored, so it cannot run alone)."""
    if not r.can_configure:
        return
    last = BACKUPS.last(backup_key(r), backups.AUTO + ("manual",))
    if not last or time.time() - last["time"] > 23 * 3600:
        auto_backup(r, "daily", "automatic")


def backup_reader():
    if current_user()["role"] == "viewer":
        raise auth.AuthError("Viewers cannot read the full configuration.")
    return router()


@app.get("/api/backups")
def backups_list():
    r = backup_reader()
    return jsonify(backups=BACKUPS.list(backup_key(r)), ssh=bool(r.can_configure), host=r.host, restore=True)


@app.post("/api/backups")
def backups_take():
    r = router()
    d = request.get_json(force=True) or {}
    m = BACKUPS.add(backup_key(r), running_text(r), me(), "manual", d.get("note", ""), host=r.host)
    AUDIT.log(me(), "backup_take", host=r.host, backup=m["id"])
    return jsonify(m)


@app.post("/api/backups/upload")
def backups_upload():
    r = router()
    d = request.get_json(force=True) or {}
    text = str(d.get("text") or "")
    if "\x00" in text:
        raise DeviceError("That is not a text configuration file.")
    try:
        m = BACKUPS.add(backup_key(r), text, me(), "upload", (d.get("note") or d.get("name") or "")[:200], host=r.host)
    except ValueError as exc:
        raise DeviceError(str(exc))
    AUDIT.log(me(), "backup_upload", host=r.host, backup=m["id"], name=str(d.get("name") or "")[:100])
    return jsonify(m)


@app.get("/api/backups/<bid>/download")
def backups_download(bid):
    r = backup_reader()
    try:
        text = BACKUPS.text(backup_key(r), bid)
    except ValueError as exc:
        raise DeviceError(str(exc))
    safe_host = re.sub(r"[^A-Za-z0-9.-]", "_", r.host)
    return Response(text, mimetype="text/plain", headers={"Content-Disposition": f"attachment; filename={safe_host}-{bid}.cfg"})


@app.post("/api/backups/<bid>")
def backups_update(bid):
    r = router()
    d = request.get_json(force=True) or {}
    try:
        if d.get("delete"):
            BACKUPS.delete(backup_key(r), bid)
            AUDIT.log(me(), "backup_delete", host=r.host, backup=bid)
            return jsonify(ok=True)
        return jsonify(BACKUPS.update(backup_key(r), bid, d.get("pinned"), d.get("note")))
    except ValueError as exc:
        raise DeviceError(str(exc))


RESTORING = threading.Lock()


@app.post("/api/backups/<bid>/restore")
def backups_restore(bid):
    """Put a server copy back on the router. commit=false is Check only: the router loads the file, lists what it
    would change, and throws it away. commit=true needs the copy's id typed back, takes a before-restore copy of
    the router as it is now, then commits. The router's startup configuration is not touched (Save stays manual)."""
    r = router()
    d = request.get_json(force=True) or {}
    key = backup_key(r)
    try:
        meta, text = BACKUPS.meta(key, bid), BACKUPS.text(key, bid)
    except ValueError as exc:
        raise DeviceError(str(exc))
    commit = bool(d.get("commit"))
    if commit and str(d.get("confirm") or "").strip() != bid:
        raise DeviceError(f"Type {bid} to confirm the restore.")
    if not RESTORING.acquire(blocking=False):
        raise DeviceError("A restore is already running.")
    try:
        if commit:
            auto_backup(r, "before-restore", me(), f"before restoring {bid}", wait=True)
        AUDIT.log(me(), "backup_restore" if commit else "backup_restore_check", host=r.host, demo=r.demo, backup=bid)
        out = r.load_replace(text, commit=commit, description=f"ne8000-manager restore {bid}")
        cid = ""
        if commit:
            VIEWCACHE.invalidate(r)
            auto_backup(r, "after-change", me(), f"restored {bid}")
            cid = commit_id(r)
        return jsonify(output=out, committed=commit, backup=meta, commit_id=cid, time=time.time())
    finally:
        RESTORING.release()


@app.get("/api/backups/<bid>/diff")
def backups_diff(bid):
    """Compare a copy with the router now (against=running) or with another copy (against=<id>)."""
    r = backup_reader()
    key = backup_key(r)
    against = request.args.get("against", "running")
    try:
        old = BACKUPS.text(key, bid)
        new, name = (running_text(r), "router now") if against == "running" else (BACKUPS.text(key, against), against)
    except ValueError as exc:
        raise DeviceError(str(exc))
    return jsonify(backups.diff(old, new, bid, name))


@app.get("/api/running-config")
def running_config():
    if current_user()["role"] == "viewer":
        raise auth.AuthError("Viewers cannot read the full configuration.")
    r = router()
    text = r.run("display current-configuration", read_timeout=180)
    if request.args.get("download"):
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        safe_host = re.sub(r"[^A-Za-z0-9.-]", "_", r.host)
        return Response(text, mimetype="text/plain",
                        headers={"Content-Disposition": f"attachment; filename={safe_host}-{stamp}.cfg"})
    return jsonify(output=text)


# ---- billing (Splynx) -----------------------------------------------------------
@app.get("/api/billing")
def billing_status():
    eng = router().billing
    st = eng.state()
    return jsonify(config=billing.public_config(eng.config()), last=eng.last, log=list(reversed(st["log"]))[:200],
                   shut_by_billing=st["shut_by_billing"], demo=router().demo)


@app.get("/api/billing/usage")
def billing_usage():
    """Live state and traffic of each matched customer's port, for the billing page. Rates come
    from SNMP counters (or the demo); over SSH only, the port's utilisation is used instead so
    the router is not asked for every port's counters every few seconds."""
    r = router()
    rows = [x for x in ((r.billing.last or {}).get("rows") or []) if x.get("interface")]
    names = sorted({x["interface"] for x in rows})
    ifs = {i["name"]: i for i in VIEWCACHE.get(r, "interfaces")[0]} if names else {}
    plan = {}
    try:   # the QoS profile speed on each port, when it has one
        for q in r.qos()["rules"]:
            if q.get("kind") == "profile" and q["direction"] == "outbound" and q.get("pir_kbps"):
                plan[q["interface"]] = q["pir_kbps"] * 1000
    except Exception:
        pass
    series = r.traffic(names) if names and (isinstance(r, SnmpRouter) or r.demo) else {}
    out = {}
    for n in names:
        i, pts = ifs.get(n, {}), series.get(n) or []
        u = {"phy": i.get("phy", ""), "protocol": i.get("protocol", ""), "admin_down": i.get("admin_down", False),
             "plan_bps": plan.get(n), "in_util": i.get("in_util"), "out_util": i.get("out_util"), "window": 0}
        if pts:
            span = pts[-1][0] - pts[0][0]
            gaps = [(b[0] - a[0]) for a, b in zip(pts, pts[1:])]
            u.update(rx_bps=pts[-1][1], tx_bps=pts[-1][2], window=round(span),
                     rx_avg=round(sum(p[1] for p in pts) / len(pts)), tx_avg=round(sum(p[2] for p in pts) / len(pts)),
                     rx_peak=max(p[1] for p in pts), tx_peak=max(p[2] for p in pts),
                     rx_bytes=round(sum(b[1] * g for b, g in zip(pts[1:], gaps)) / 8), tx_bytes=round(sum(b[2] * g for b, g in zip(pts[1:], gaps)) / 8))
        out[n] = u
    return jsonify(ports=out, rates=bool(series))


@app.get("/api/graph")
def graph():
    """Interface traffic history: range=day|week|month|year (the 'now' graph uses /api/traffic)."""
    r, name, rng = router(), request.args.get("name", ""), request.args.get("range", "day")
    if rng not in history.RANGES:
        raise DeviceError("Unknown range.")
    if r.demo:
        base = (getattr(r, "ifs", {}).get(name) or {}).get("base") or (demo_flow_base(name) if name.startswith("flow:") else 0)
        return jsonify(points=history.synthetic(base, rng, seed=len(name)), collecting=False)
    rid = session.get("router_id", "")
    if not rid:
        return jsonify(points=[], collecting=False, note="History is kept for saved routers only (Manager > Routers).")
    full = SAVED.get(rid) or {}
    if not full.get("snmp"):
        return jsonify(points=[], collecting=False, note="Add this router's SNMP login under Manager > Routers to keep history.")
    st = HIST.status.get(rid) or {}
    return jsonify(points=HIST.series(rid, name, rng), collecting=True, error=st.get("error", ""), last=st.get("time"))


def demo_flow_base(name):
    return 2e6 + (sum(map(ord, name)) % 40) * 3e6


def demo_flow_subnets(r):
    out = []
    for i in r.interfaces():
        for a in [i.get("ip")] + list(i.get("secondary") or []):
            if a and not i["name"].lower().startswith(("loopback", "null")) and not a.endswith("/32"):
                out.append({"subnet": str(ipaddress.ip_interface(a).network), "interface": i["name"]})
    return out


@app.get("/api/health")
def health_report():
    """Smart health check for the connected router: problems found, latency targets and when it last ran."""
    r = router()
    if r.demo:
        return jsonify(health.demo_report(getattr(r, "ifs", {})))
    rid = session.get("router_id", "")
    if not rid:
        return jsonify(findings=[], status={}, latency=[], targets=[], note="The health check runs for saved routers only (Manager > Routers).")
    out = HEALTH.report(rid)
    if not (SAVED.get(rid) or {}).get("snmp"):
        out["note"] = "Add this router's SNMP login under Manager > Routers so the health check can read its links and optics."
    return jsonify(out)


# ---- alerts: Telegram, email, SMS, Slack/Teams/webhook ------------------------------
SK_LABEL = {"power": "power", "access": "access", "cctv": "CCTV", "env": "environment", "device": "device"}


def alert_sources():
    """Everything wrong now, as alert problems: health findings and down sessions of every saved router, site alarms, this server."""
    out = []
    for r in list(SAVED.routers):
        rid, name = r["id"], r.get("name") or r.get("host")
        for f in HEALTH.report(rid).get("findings", []):
            out.append({"key": f"{rid}:{f['id']}", "kind": alerts.HEALTH_MAP.get(f["kind"], "router"), "severity": f.get("severity", "warning"),
                        "title": f"{f.get('title') or f['kind']}: {f.get('target', '')}".rstrip(": "), "detail": f.get("detail", ""),
                        "target": f.get("target", ""), "description": f.get("description", ""), "where": name, "since": f.get("since")})
        for kind, key, state, info, since in TRACK.down_sessions(rid):
            out.append({"key": f"{rid}:{kind}:{key}", "kind": "session_down", "severity": "critical", "title": f"{kind.upper()} session down: {key}",
                        "detail": (f"state {state}" if state else "down") + (f", {info}" if info else ""), "target": key, "where": name, "since": since})
    for st in SITEREG.sites():
        for a in SITEREG.alarms(st["id"]):
            out.append({"key": f"site:{st['id']}:{a['kind']}:{a.get('asset', '')}:{a['event'] if a.get('source') != 'ping' else 'down'}", "kind": "site",
                        "severity": a["severity"], "title": f"{st.get('code') or st['name']} {SK_LABEL.get(a['kind'], a['kind'])}: {a['event'].replace('_', ' ')}"
                        + (f" ({a['asset']})" if a.get("asset") else ""), "detail": a.get("detail", ""), "target": a.get("asset", ""), "where": st["name"], "since": a.get("ts")})
    for w in serverinfo.report(DATA_DIR)["warnings"]:
        out.append({"key": "server:" + w["text"].split(":")[0][:40], "kind": "server", "severity": w["severity"], "title": "Server: " + w["text"].split(".")[0],
                    "detail": w["text"], "where": "NE8000 Manager server"})
    return out


ALERTS = alerts.Alerts(DATA_DIR, alert_sources, health._safe_ping)


def _alerts_do(fn):
    try:
        return fn()
    except ValueError as exc:
        return jsonify(error=str(exc)), 400


@app.get("/api/alerts")
def alerts_get():
    return jsonify(ALERTS.public())


@app.post("/api/alerts/channels")
def alerts_channel_add():
    d = request.get_json(silent=True) or {}
    def go():
        c = ALERTS.save_channel(d, d.get("id") or None)
        AUDIT.log(me(), "alert_channel_saved", name=c["name"], type=c["type"])
        return jsonify(ALERTS.public())
    return _alerts_do(go)


@app.delete("/api/alerts/channels/<cid>")
def alerts_channel_delete(cid):
    ALERTS.delete_channel(cid)
    AUDIT.log(me(), "alert_channel_deleted", id=cid)
    return jsonify(ALERTS.public())


@app.post("/api/alerts/test")
def alerts_test():
    d = request.get_json(silent=True) or {}
    def go():
        ok, err = ALERTS.test_channel(d, d.get("id") or None)
        return jsonify(ok=ok, error=err)
    return _alerts_do(go)


@app.post("/api/alerts/rules")
def alerts_rules():
    d = request.get_json(silent=True) or {}
    return _alerts_do(lambda: (ALERTS.save_rules(d.get("rules") or []), AUDIT.log(me(), "alert_rules_saved"), jsonify(ALERTS.public()))[2])


@app.post("/api/alerts/settings")
def alerts_settings():
    d = request.get_json(silent=True) or {}
    return _alerts_do(lambda: (ALERTS.save_settings(d), jsonify(ALERTS.public()))[1])


@app.post("/api/alerts/checks")
def alerts_check_save():
    d = request.get_json(silent=True) or {}
    return _alerts_do(lambda: (ALERTS.save_check(d, d.get("id") or None), jsonify(ALERTS.public()))[1])


@app.delete("/api/alerts/checks/<cid>")
def alerts_check_delete(cid):
    ALERTS.delete_check(cid)
    return jsonify(ALERTS.public())


@app.post("/api/alerts/ack")
def alerts_ack():
    d = request.get_json(silent=True) or {}
    return _alerts_do(lambda: (ALERTS.ack(str(d.get("key") or ""), me()), jsonify(ALERTS.public()))[1])


@app.post("/api/alerts/run")
def alerts_run():
    ALERTS.run_now()
    return jsonify(ok=True)


@app.get("/api/server")
def server_report():
    """The machine this app runs on: CPU, memory, disk, uptime, the app process and the last 24 h of use."""
    return jsonify(serverinfo.report(DATA_DIR))


@app.post("/api/health/run")
def health_run():
    """Check now instead of waiting for the next 5-minute round (runs in the background)."""
    r = router()
    rid = session.get("router_id", "")
    if r.demo or not rid:
        return jsonify(ok=True)
    threading.Thread(target=lambda: HEALTH.check(rid), daemon=True).start()
    return jsonify(ok=True)


# ---- front panel photo: one photo per router and where each port is on it ----
def panel_key():
    r = router()
    return "demo" if r.demo else (session.get("router_id") or "host-" + r.host)


@app.get("/api/panel")
def panel_get():
    return jsonify(PANELS.get(panel_key()))


@app.get("/api/panel/photo")
def panel_photo():
    path, mime = PANELS.photo(panel_key())
    if not path:
        return jsonify(error="No photo yet."), 404
    with open(path, "rb") as f:
        data = f.read()
    return Response(data, mimetype=mime, headers={"Cache-Control": "private, max-age=86400", "X-Content-Type-Options": "nosniff"})


@app.post("/api/panel/photo")
def panel_photo_set():
    try:
        out = PANELS.set_photo(panel_key(), (request.get_json(force=True) or {}).get("image"), by=me())
    except ValueError as exc:
        raise DeviceError(str(exc))
    AUDIT.log(me(), "panel_photo", host=router().host)
    return jsonify(out)


@app.post("/api/panel/marks")
def panel_marks():
    body = request.get_json(force=True) or {}
    try:
        out = PANELS.set_marks(panel_key(), body.get("marks"), body.get("size"), by=me())
    except (ValueError, TypeError) as exc:
        raise DeviceError(str(exc))
    AUDIT.log(me(), "panel_marks", host=router().host, ports=len(out["marks"]))
    return jsonify(out)


@app.post("/api/panel/delete")
def panel_delete():
    PANELS.delete(panel_key())
    AUDIT.log(me(), "panel_delete", host=router().host)
    return jsonify(PANELS.get(panel_key()))


# ---- device notes: what engineers need to know about this router ----
@app.get("/api/notes")
def notes_list():
    return jsonify(notes=NOTES.list(panel_key()))


@app.post("/api/notes")
def notes_add():
    body = request.get_json(force=True) or {}
    try:
        n = NOTES.add(panel_key(), body.get("text"), me(), body.get("pinned"), body.get("port"))
    except ValueError as exc:
        raise DeviceError(str(exc))
    AUDIT.log(me(), "note_add", host=router().host, note=n["id"], text=n["text"][:200])
    return jsonify(notes=NOTES.list(panel_key()))


@app.post("/api/notes/<note_id>/update")
def notes_update(note_id):
    body = request.get_json(force=True) or {}
    try:
        n = NOTES.update(panel_key(), note_id, me(), text=body.get("text"), pinned=body.get("pinned"), port=body.get("port"))
    except ValueError as exc:
        raise DeviceError(str(exc))
    AUDIT.log(me(), "note_update", host=router().host, note=note_id, text=n["text"][:200], pinned=n.get("pinned"))
    return jsonify(notes=NOTES.list(panel_key()))


@app.post("/api/notes/<note_id>/delete")
def notes_delete(note_id):
    try:
        n = NOTES.delete(panel_key(), note_id)
    except ValueError as exc:
        raise DeviceError(str(exc))
    AUDIT.log(me(), "note_delete", host=router().host, note=note_id, text=n["text"][:200])
    return jsonify(notes=NOTES.list(panel_key()))


@app.post("/api/health/targets")
def health_targets():
    """Addresses this server pings for latency and loss (customer CPEs, upstreams)."""
    r = router()
    rows = (request.get_json(force=True) or {}).get("targets") or []
    if r.demo:
        return jsonify(targets=rows)
    rid = session.get("router_id", "")
    if not rid:
        raise DeviceError("Latency targets are kept for saved routers only.")
    try:
        out = HEALTH.set_targets(rid, rows)
    except ValueError as exc:
        raise DeviceError(str(exc))
    AUDIT.log(me(), "health_targets", host=r.host, count=len(out))
    return jsonify(targets=out)


@app.get("/api/flows")
def flows():
    """Per-subnet traffic from NetStream for the connected router: collector status and every subnet with its last rates."""
    r = router()
    if r.demo:
        subs = demo_flow_subnets(r)
        for x in subs:
            pts = history.synthetic(demo_flow_base("flow:" + x["subnet"]), "day", seed=len("flow:" + x["subnet"]))
            x["rx_bps"], x["tx_bps"] = pts[-1][1], pts[-1][2]
        return jsonify(subnets=subs, status={"running": True, "demo": True, "packets": 0, "exporters": {}}, configured=True)
    rid = session.get("router_id", "")
    if not rid:
        return jsonify(subnets=[], status=FLOWS.status(None), configured=False, note="Per-address traffic is kept for saved routers only (Manager > Routers).")
    known = FLOWS.routers.get(rid) or {}
    if not known.get("subnets") and time.time() - known.get("time", 0) > 20:     # not read yet, or the last read failed
        try:
            FLOWS.refresh(rid)
        except Exception:
            pass
    subs = FLOWS.subnets(rid)
    for x in subs:
        pts = FLOWS.live_series(rid, x["subnet"])[-5:]
        x["rx_bps"] = sum(p[1] for p in pts) / len(pts) if pts else None
        x["tx_bps"] = sum(p[2] for p in pts) / len(pts) if pts else None
    st = FLOWS.status(rid)
    return jsonify(subnets=subs, status=st, configured=bool(st["exporters"]))


@app.get("/api/flows/live")
def flows_live():
    """The last hour per minute for one subnet, and its busiest addresses over the last 5-10 minutes."""
    r, key = router(), request.args.get("subnet", "")
    try:
        key = str(ipaddress.ip_network(key, strict=False))
    except ValueError:
        raise DeviceError("Unknown subnet.")
    if r.demo:
        base, now = demo_flow_base("flow:" + key), time.time()
        pts = [[int(now - now % 60 - 60 * k), base * (0.9 + 0.2 * ((k * 7) % 5) / 5), base * 0.25 * (0.9 + 0.2 * ((k * 3) % 5) / 5)] for k in range(60, 0, -1)]
        net = ipaddress.ip_network(key)
        hosts = [{"address": str(h), "rx_bytes": int(base * 30 / (j + 1)), "tx_bytes": int(base * 30 / (j + 1) * 0.25)} for j, h in enumerate(list(net.hosts())[:5])]
        return jsonify(points=pts, hosts=hosts, demo=True)
    rid = session.get("router_id", "")
    return jsonify(points=FLOWS.live_series(rid, key), hosts=FLOWS.top_hosts(rid, key))


@app.post("/api/billing/config")
def billing_config():
    eng = router().billing
    data = request.get_json(force=True)
    cfg = eng.config()
    for k in ("url", "api_key", "mode", "match_description"):
        if k in data:
            cfg[k] = data[k]
    if data.get("api_secret") and data["api_secret"] != "********":
        cfg["api_secret"] = data["api_secret"]
    if cfg["mode"] not in ("off", "dry-run", "enforce"):
        raise billing.BillingError("Mode must be off, dry-run or enforce.")
    for k in ("partner_ids", "location_ids"):
        if k in data:
            cfg[k] = [int(x) for x in data[k] if str(x).strip()]
    for k, lo in (("router_id", 0), ("interval", 15)):
        if k in data and str(data[k]).strip():
            cfg[k] = max(lo, int(data[k]))
    if "block_statuses" in data:
        cfg["block_statuses"] = [x for x in data["block_statuses"] if x in ("blocked", "disabled", "new")] or ["blocked"]
    eng.save_config(cfg)
    AUDIT.log(me(), "billing_settings", mode=cfg["mode"])
    return jsonify(config=billing.public_config(cfg))


@app.get("/api/billing/meta")
def billing_meta():
    src = router().billing.source()
    return jsonify(partners=src.partners(), locations=src.locations(),
                   routers=[{"id": x.get("id"), "title": x.get("title"), "model": x.get("model")} for x in src.routers()])


@app.post("/api/billing/sync")
def billing_sync():
    apply = bool(request.get_json(force=True).get("apply"))
    if apply:
        AUDIT.log(me(), "billing_sync_apply", host=router().host)
    return jsonify(last=router().billing.sync(apply=apply))


@app.post("/api/billing/map")
def billing_map():
    data = request.get_json(force=True)
    eng = router().billing
    cfg = eng.config()
    cid = str(int(data["customer_id"]))
    iface = data.get("interface")
    if iface is None:
        cfg["manual"].pop(cid, None)
    else:
        safe_name(iface, "interface")
        cfg["manual"][cid] = iface
    eng.save_config(cfg)
    AUDIT.log(me(), "billing_map", customer=cid, interface=iface)
    return jsonify(ok=True)


@app.post("/api/billing/demo-status")
def billing_demo_status():
    """Demo mode only: change a sample customer's Splynx status (block / record a payment)."""
    r = router()
    if not r.demo:
        raise billing.BillingError("Only available in demo mode.")
    data = request.get_json(force=True)
    for c in r.billing_demo_source._c:
        if c["id"] == int(data["customer_id"]):
            c["status"] = "blocked" if data.get("status") == "blocked" else "active"
    return jsonify(ok=True)


# ---- network plans ----------------------------------------------------------------
PLANS = plans.PlanStore(os.path.join(DATA_DIR, "plans"))
plans.ensure_example(PLANS)


@app.get("/api/plans")
def plan_list():
    router()
    return jsonify(plans=PLANS.list())


@app.post("/api/plans")
def plan_create():
    router()
    d = request.get_json(force=True)
    return jsonify(PLANS.create(d.get("name"), d.get("description", ""), me(), d.get("diagram"), d.get("tasks")))


@app.get("/api/changes")
def changes_list():
    """The change log of the connected router: ?kind=arp,bgp&q=text&days=7&limit=500"""
    r = router()
    rid = netmap_key(r)
    kinds = [k for k in (request.args.get("kind") or "").split(",") if k]
    days = min(max(float(request.args.get("days") or 7), 0.01), 180)
    since = time.time() - days * 86400
    st = TRACK.status.get(session.get("router_id", "")) or {}
    return jsonify(events=TRACK.events(rid, kinds, since, (request.args.get("q") or "").strip()[:100],
                                       min(int(request.args.get("limit") or 500), 5000), (request.args.get("key") or "").strip()[:100]),
                   counts=TRACK.counts(rid, time.time() - 86400), tracked={k: v[0] for k, v in TRACK.tracked(rid).items()},
                   poller=dict(on=TRACK.polled(session.get("router_id", "")), last=st.get("time"), error=st.get("error", ""),
                               saved=bool(session.get("router_id")), snmp=bool((SAVED.get(session.get("router_id", "")) or {}).get("snmp"))),
                   ssh=bool(r.can_configure))


@app.get("/api/changes/series")
def changes_series():
    kind = request.args.get("kind", "")
    if kind not in ("peer", "routes", "policy"):
        raise DeviceError("Unknown series.")
    rng = request.args.get("range", "day")
    if rng not in changes.RANGES:
        raise DeviceError("Unknown range.")
    return jsonify(points=TRACK.series(netmap_key(router()), kind, request.args.get("key", ""), rng))


@app.get("/api/changes/spark")
def changes_spark():
    """Small trend lines for every key of a kind: {key: [[ts, a], ...]} (at most 48 points each)."""
    kind = request.args.get("kind", "peer")
    if kind not in ("peer", "routes", "policy"):
        raise DeviceError("Unknown series.")
    rng = request.args.get("range", "day")
    if rng not in changes.RANGES:
        raise DeviceError("Unknown range.")
    return jsonify(series=TRACK.spark(netmap_key(router()), kind, rng))


@app.get("/api/changes/latest")
def changes_latest():
    """Newest numbers of a kind, plus the numbers 1 hour and 1 day before, for the change columns."""
    kind = request.args.get("kind", "peer")
    if kind not in ("peer", "routes", "policy"):
        raise DeviceError("Unknown series.")
    rid = netmap_key(router())
    return jsonify(now=TRACK.latest(rid, kind), hour=TRACK.latest(rid, kind, 3600), day=TRACK.latest(rid, kind, 86400))


@app.get("/api/policy/test")
def policy_test():
    """What would route-policy P do to prefix X (from the policy and prefix lists read from the router)."""
    pfx = (request.args.get("prefix") or "").strip()
    if not re.match(r"^\d{1,3}(\.\d{1,3}){3}(/\d{1,2})?$", pfx):
        raise DeviceError("Give an IPv4 prefix, e.g. 8.8.8.0/24.")
    try:
        ipaddress.ip_network(pfx, strict=False)
    except ValueError:
        raise DeviceError("Give an IPv4 prefix, e.g. 8.8.8.0/24.")
    data, _ = VIEWCACHE.get(router(), "route_policies")
    pols = [p for p in data["policies"] if not request.args.get("policy") or p["name"] == request.args["policy"]]
    if not pols:
        raise DeviceError("No such route-policy.")
    return jsonify(results=[dict(policy.evaluate(pfx, p, data["prefixes"]), policy=p["name"]) for p in pols])


def netmap_key(r):
    return "demo" if r.demo else (session.get("router_id") or "host-" + r.host)


@app.get("/api/search")
def search():
    """Search everything the server already holds for the connected router (never waits for the router):
    interfaces, addresses, ARP, BGP peers, VLANs, VPNs, policies and prefix lists, QoS, LLDP neighbours,
    billing customers, the map, notes, backups and the change log. -> {results: [{group, title, sub, go}]}"""
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify(results=[])
    r = router()
    ql, out = q.lower(), []
    viewer = current_user()["role"] == "viewer"

    def hit(*vals):
        return any(ql in str(v).lower() for v in vals if v not in (None, ""))

    def add(group, title, sub, go, limit=8):
        if sum(1 for x in out if x["group"] == group) < limit:
            out.append({"group": group, "title": str(title), "sub": str(sub or ""), "go": go})

    peek = lambda n: VIEWCACHE.peek(r, n)
    for i in peek("interfaces") or []:
        if hit(i.get("name"), i.get("description"), i.get("ip"), i.get("vlan"), i.get("vpn")):
            add("Interfaces", i["name"], " · ".join(x for x in (i.get("description"), i.get("ip"), i.get("phy")) if x), {"page": "interfaces", "q": i["name"]})
    for e in (peek("arp") or {}).get("entries", []):
        if hit(e.get("ip"), e.get("mac")):
            add("ARP", e["ip"], f"{e.get('mac', '')} on {e.get('interface', '')}", {"page": "arp", "q": e["ip"]})
    b = peek("bgp") or {}
    desc = {}
    for lines in (b.get("config") or {}).values():
        for l in lines:
            m = re.match(r"^peer (\S+) description (.+)$", l)
            if m:
                desc[m.group(1)] = m.group(2)
    for p in b.get("peers", []):
        if hit(p.get("peer"), p.get("as"), desc.get(p.get("peer")), "as" + str(p.get("as", ""))):
            add("BGP peers", p["peer"], f"AS {p.get('as', '')} · {desc.get(p['peer'], '')} · {p.get('state', '')}", {"page": "bgp", "peer": p["peer"]})
    v = peek("vlans") or {}
    for x in v.get("vlans", []):
        if hit(x.get("id"), x.get("description")):
            add("VLANs", f"VLAN {x['id']}", x.get("description", ""), {"page": "vlan", "q": str(x["id"])})
    for x in (peek("vpn_instances") or {}).get("instances", []):
        if hit(x.get("name"), x.get("rd")):
            add("VPN instances", x["name"], x.get("rd", ""), {"page": "vpn", "q": x["name"]})
    rp = peek("route_policies") or {}
    for x in rp.get("policies", []):
        if hit(x.get("name")):
            add("Route policies", x["name"], f"{len(x.get('nodes', []))} rules", {"page": "policies", "policy": x["name"]})
    for name, pl in (rp.get("prefixes") or {}).items():
        ents = pl.get("entries", []) if isinstance(pl, dict) else []
        if hit(name) or any(hit(e.get("prefix")) for e in ents):
            add("Prefix lists", name, ", ".join(str(e.get("prefix", "")) for e in ents[:4]), {"page": "policies", "q": name})
    qos = peek("qos") or {}
    for x in qos.get("profiles", []) if isinstance(qos.get("profiles"), list) else []:
        if hit(x.get("name") if isinstance(x, dict) else x):
            add("QoS", x.get("name") if isinstance(x, dict) else x, "QoS profile", {"page": "queues", "q": x.get("name") if isinstance(x, dict) else x})
    for x in (peek("neighbors") or {}).get("lldp", []):
        if hit(x.get("neighbor"), x.get("local")):
            add("Neighbours (LLDP)", x.get("neighbor", ""), f"{x.get('local', '')} → {x.get('neighbor_port', '')}", {"page": "neighbors", "q": x.get("neighbor", "")})
    eng = getattr(r, "billing", None)
    for c in ((eng.last if eng else None) or {}).get("rows", []) or []:
        if hit(c.get("name"), c.get("login"), c.get("ips"), c.get("id")):
            add("Customers (Splynx)", c.get("name", ""), " · ".join(str(x) for x in (c.get("login"), c.get("interface") or "no port", c.get("status")) if x), {"page": "billing", "q": c.get("name", "")})
    hitmap = NETMAP_CACHE.get(netmap_key(r))
    for n in (hitmap[1]["diagram"]["nodes"] if hitmap else []):
        if hit(n.get("label"), *(n.get("props") or {}).values()):
            add("Network map", n.get("label", ""), (n.get("props") or {}).get("location") or n.get("type", ""), {"page": "netmap"})
    for n in NOTES.list(panel_key()):
        if hit(n.get("text"), n.get("port")):
            add("Notes", (n.get("text") or "")[:80], n.get("port") or n.get("by", ""), {"page": "dashboard"})
    if not viewer:
        for m in BACKUPS.list(backup_key(r)):
            if hit(m.get("note"), m.get("id"), m.get("by")):
                add("Backups", m["id"], m.get("note") or m.get("kind", ""), {"page": "backup"})
    for e in TRACK.events(netmap_key(r), None, time.time() - 30 * 86400, q[:100], 6):
        add("Change log", f"{e['key']} {e['change']}", f"{time.strftime('%d %b %H:%M', time.localtime(e['ts']))} · {e.get('info') or ''}", {"page": "changes", "q": e["key"]})
    return jsonify(results=out)


NETMAP_BUSY = set()       # map keys being rebuilt in the background


def build_netmap(r, key):
    eng = getattr(r, "billing", None)
    snap = netdesign.snapshot(r, ((eng.last if eng else None) or {}).get("rows"))
    diagram, summary, m = netdesign.discover(snap)
    rec = NETMAP.update(key, netmap.inventory(snap, m))
    out = {"key": key, "diagram": diagram, "summary": summary, "at": rec["at"], "warnings": snap["warnings"],
           "rates": isinstance(r, SnmpRouter) or r.demo}
    NETMAP_CACHE[key] = (time.time(), out)
    return out


def netmap_background(r, key):
    """Rebuild the map without anyone waiting for it (at connect, and when a stored map is over a minute old)."""
    if key in NETMAP_BUSY:
        return
    NETMAP_BUSY.add(key)

    def work():
        try:
            build_netmap(r, key)
        except Exception as exc:
            print(f"network map ({key}): {exc}", flush=True)
        finally:
            NETMAP_BUSY.discard(key)
    threading.Thread(target=work, daemon=True).start()


@app.get("/api/netmap")
def netmap_view():
    """The live as-built map of the connected router. A stored map up to 10 minutes old is shown at once and rebuilt
    in the background when it is over a minute old; refresh=1 (Refresh now) waits for a new one."""
    r = router()
    key = netmap_key(r)
    hit = NETMAP_CACHE.get(key)
    if hit and request.args.get("refresh") != "1" and time.time() - hit[0] < 600:
        if time.time() - hit[0] >= 60:
            netmap_background(r, key)
        return jsonify({**with_manual(hit[1], key), "changes": NETMAP.load(key)["changes"][-100:][::-1]})
    out = build_netmap(r, key)
    return jsonify({**with_manual(out, key), "changes": NETMAP.load(key)["changes"][-100:][::-1]})


@app.get("/api/netmap/traffic")
def netmap_traffic():
    """Current rate (SNMP or demo) or utilisation of the interfaces on the map, and BGP session states."""
    r = router()
    hit = NETMAP_CACHE.get(netmap_key(r))
    if not hit:
        return jsonify(ifs={}, peers={})
    manual = links_load().get(netmap_key(r), [])
    names = sorted({l["props"]["iface"] for l in hit[1]["diagram"]["links"] if l["props"].get("iface")} | {m["iface"] for m in manual if m.get("iface")})
    ifs = {i["name"]: i for i in VIEWCACHE.get(r, "interfaces")[0]}   # from memory: the map asks every 5 s
    series = r.traffic(names) if r.demo else {n: r.series("bps:" + n) for n in names} if isinstance(r, SnmpRouter) else {}
    out = {}
    for n in names:
        i, pts = ifs.get(n, {}), series.get(n) or []
        out[n] = {"phy": i.get("phy", ""), "in_util": i.get("in_util"), "out_util": i.get("out_util"),
                  "rx": pts[-1][1] if pts else None, "tx": pts[-1][2] if pts else None}
    peers = {}
    try:
        peers = {p["peer"]: p["state"] for p in VIEWCACHE.get(r, "bgp")[0]["peers"]}
    except Exception:
        pass
    far = {}
    for m in manual:     # the far end of a line drawn by hand, from that router's own SNMP history (5-minute rates)
        if m.get("far_router") and m.get("far_iface"):
            far["ml:" + m["id"]] = HIST.latest(m["far_router"], m["far_iface"])
    return jsonify(ifs=out, peers=peers, far=far, at=time.time())


DEMO_PLACES = {"u:64512": "MOMBASA", "s:100GE0/2/1": "MOMBASA", "g:GigabitEthernet0/1/4": "NAKURU", "c:GigabitEthernet0/1/4.100": "ELDORET",
               "c:GigabitEthernet0/1/4.200": "KISUMU", "g:GigabitEthernet0/1/5": "ISIOLO", "c:GigabitEthernet0/1/6.301": "MARSABIT",
               "c:GigabitEthernet0/1/6.302": "THIKA", "c:GigabitEthernet0/1/6.303": "MALINDI", "c:GigabitEthernet0/1/6.304": "KAKAMEGA",
               "c:GigabitEthernet0/1/6.399": "MACHAKOS"}
LINKS_PATH = os.path.join(DATA_DIR, "netmap_links.json")


def links_load():
    try:
        with open(LINKS_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def with_manual(out, key):
    """The map plus the lines drawn by hand (and the devices they lead to that the router does not see)."""
    allp = links_load()
    rows, edits = allp.get(key, []), allp.get(key + "#edits", {})
    if not rows and not edits:
        return out
    nodes, links = [dict(n) for n in out["diagram"]["nodes"]], list(out["diagram"]["links"])
    byid = {n["id"]: n for n in nodes}
    root = next((n["id"] for n in nodes if n["type"] == "ne8000"), nodes[0]["id"] if nodes else "")
    under = {}
    for m in rows:
        frm = m.get("from") if m.get("from") in byid else root
        to = m.get("to") if m.get("to") in byid else "m:" + m["id"]
        if to not in byid:
            p = byid.get(frm) or {"x": 40, "y": 40}
            k = under[frm] = under.get(frm, 0) + 1
            n = {"id": to, "type": m.get("to_type") or "router", "label": m.get("to_label") or "Remote site", "x": p["x"] + 230 * (k - 1), "y": p["y"] + 170,
                 "props": {"location": m.get("town", ""), "drawn": "by hand", **({"monitored": m["far_name"]} if m.get("far_name") else {}),
                           **({"site": (SITEREG.get(m["site"]) or {}).get("code", "")} if m.get("site") else {})}}
            nodes.append(n)
            byid[to] = n
        links.append({"id": "ml:" + m["id"], "from": frm, "to": to, "props": {"iface": m.get("iface", ""), "label": m.get("label") or m.get("iface") or "Drawn line",
                      "manual": "1", "down": "tx", "far_router": m.get("far_router", ""), "far_iface": m.get("far_iface", ""), "far_name": m.get("far_name", "")}})
    if edits:
        nodes, links = apply_edits(nodes, links, root, edits)
    return {**out, "diagram": {**out["diagram"], "nodes": nodes, "links": links}}


def apply_edits(nodes, links, root, edits):
    """Corrections made on the map to lines the app found by itself: a new label, a note, the far device's name, type or
    town, or the line hidden (with whatever is only reached through it). Only the app's map changes, never the router."""
    def reach(ls):
        adj = {}
        for l in ls:
            adj.setdefault(l["from"], []).append(l["to"])
            adj.setdefault(l["to"], []).append(l["from"])
        seen, todo = {root}, [root]
        while todo:
            for y in adj.get(todo.pop(), []):
                if y not in seen:
                    seen.add(y)
                    todo.append(y)
        return seen
    before = reach(links)
    byid = {n["id"]: n for n in nodes}
    kept = []
    for l in links:
        e = edits.get(l["id"])
        if not e:
            kept.append(l)
            continue
        if e.get("hidden"):
            continue
        props = {**l["props"], "edited": "1"}
        if e.get("label"):
            props["label"] = e["label"]
        if e.get("note"):
            props["note"] = e["note"]
        far = l["from"] if l["to"] == root else l["to"]     # the device at the far end, away from the router
        n = byid.get(far)
        if n and (e.get("to_label") or e.get("to_type") or e.get("town")):
            n = byid[far] = {**n, "label": e.get("to_label") or n["label"], "type": e.get("to_type") or n["type"],
                             "props": {**n.get("props", {}), **({"location": e["town"]} if e.get("town") else {}), "edited": "on the map"}}
        kept.append({**l, "props": props})
    after = reach(kept)
    gone = before - after
    return [byid[n["id"]] for n in nodes if n["id"] not in gone], [l for l in kept if l["from"] not in gone and l["to"] not in gone]


@app.get("/api/netmap/links")
def netmap_links():
    return jsonify(links=links_load().get(netmap_key(router()), []))


@app.post("/api/netmap/links")
def netmap_links_save():
    """Add or change a line drawn by hand: {id?, from, to (a device on the map, or empty for a new one), to_label, to_type, town,
    iface (this router's interface), far_router (a saved router with SNMP), far_iface, label}. Only this app's data changes."""
    r, d = router(), request.get_json(force=True) or {}
    key = netmap_key(r)
    s = lambda k, n=120: str(d.get(k) or "").strip()[:n]
    row = {"id": s("id", 20) or uuid.uuid4().hex[:10], "from": s("from", 200), "to": s("to", 200), "to_label": s("to_label", 80), "to_type": s("to_type", 20),
           "town": s("town", 60), "iface": s("iface", 80), "far_router": s("far_router", 60), "far_iface": s("far_iface", 80), "label": s("label", 80)}
    if not row["iface"]:
        raise DeviceError("Pick the router interface this line uses.")
    if not row["to"] and not row["to_label"]:
        raise DeviceError("Pick the device at the far end, or name a new one.")
    row["site"] = need_site(d) if not row["to"] else ""       # a new device is placed at a registered site
    if row["far_router"]:
        far = SAVED.get(row["far_router"])
        if not far:
            raise DeviceError("That far-end router is not saved under Manager > Routers.")
        row["far_name"] = far.get("name", "")
        if (far.get("snmp")) and not row["far_iface"]:
            raise DeviceError("The far end is monitored over SNMP: pick its interface too.")
    with GEO_LOCK:
        allp = links_load()
        cur = [x for x in allp.get(key, []) if x["id"] != row["id"]] + [row]
        allp[key] = cur[-500:]
        tmp = LINKS_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(allp, f, indent=1)
        os.replace(tmp, LINKS_PATH)
    AUDIT.log(me(), "map_line", line=row["id"], iface=row["iface"], far=row["far_router"])
    return jsonify(link=row)


def links_write(allp):
    tmp = LINKS_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(allp, f, indent=1)
    os.replace(tmp, LINKS_PATH)


@app.get("/api/netmap/edits")
def netmap_edits():
    """The corrections made on the map to lines the app found by itself (and the hidden ones, to bring back)."""
    return jsonify(edits=links_load().get(netmap_key(router()) + "#edits", {}))


@app.post("/api/netmap/edits")
def netmap_edits_save():
    """Correct a line the app found by itself: {id, label, note, to_label, to_type, town, hidden, was}. Only the app's map changes."""
    d = request.get_json(force=True) or {}
    s = lambda k, n=120: str(d.get(k) or "").strip()[:n]
    lid = s("id", 200)
    if not lid or lid.startswith("ml:"):
        raise DeviceError("Pick a line on the map.")
    row = {k: v for k, v in {"label": s("label", 80), "note": s("note", 300), "to_label": s("to_label", 80), "to_type": s("to_type", 20),
                              "town": s("town", 60), "was": s("was", 120)}.items() if v}
    if d.get("hidden"):
        row["hidden"] = True
    row.update(by=me(), at=int(time.time()))
    key = netmap_key(router()) + "#edits"
    with GEO_LOCK:
        allp = links_load()
        cur = allp.get(key, {})
        cur[lid] = row
        allp[key] = dict(list(cur.items())[-1000:])
        links_write(allp)
    AUDIT.log(me(), "map_line_hidden" if row.get("hidden") else "map_line_edited", line=lid)
    return jsonify(edit=row)


@app.delete("/api/netmap/edits/<path:lid>")
def netmap_edits_delete(lid):
    """Undo a correction (or bring a hidden line back)."""
    key = netmap_key(router()) + "#edits"
    with GEO_LOCK:
        allp = links_load()
        allp.get(key, {}).pop(lid, None)
        links_write(allp)
    AUDIT.log(me(), "map_line_restored", line=lid)
    return jsonify(ok=True)


@app.delete("/api/netmap/links/<lid>")
def netmap_links_delete(lid):
    key = netmap_key(router())
    with GEO_LOCK:
        allp = links_load()
        allp[key] = [x for x in allp.get(key, []) if x["id"] != lid]
        tmp = LINKS_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(allp, f, indent=1)
        os.replace(tmp, LINKS_PATH)
    AUDIT.log(me(), "map_line_deleted", line=lid)
    return jsonify(ok=True)


@app.get("/api/netmap/remote")
def netmap_remote():
    """Saved routers with SNMP (for the far end of a line), or with ?rid= the interfaces that router reports."""
    rid = request.args.get("rid", "")
    if not rid:
        return jsonify(routers=[{"id": x["id"], "name": x["name"], "host": x["host"], "snmp": bool(x.get("snmp"))} for x in SAVED.list()])
    if r_demo():
        return jsonify(interfaces=[{"name": n, "rx": 4e7, "tx": 9e7, "ts": time.time()} for n in ("GigabitEthernet0/0/1", "GigabitEthernet0/0/2", "XGigabitEthernet0/0/1")])
    return jsonify(interfaces=HIST.ifnames(rid))


def r_demo():
    try:
        return router().demo
    except Exception:
        return False


@app.get("/api/sites")
def sites_report():
    """Major sites: delay, loss and path to Google, YouTube, Facebook... measured from this server through the router."""
    if r_demo():
        return jsonify(sitewatch.demo_report(SITES.sites()))
    return jsonify(SITES.report())


@app.post("/api/sites")
def sites_set():
    rows = (request.get_json(force=True) or {}).get("sites") or []
    try:
        out = SITES.set_sites(rows)
    except ValueError as exc:
        raise DeviceError(str(exc))
    AUDIT.log(me(), "sites_list", count=len(out))
    if not r_demo():
        threading.Thread(target=SITES.check_all, daemon=True).start()
    return jsonify(sites=out)


@app.post("/api/sites/settings")
def sites_settings():
    """How often the major sites are pinged: every 1 to 5 minutes (the path is traced every 5 minutes)."""
    try:
        sec = SITES.set_interval((request.get_json(force=True) or {}).get("interval"))
    except ValueError as exc:
        raise DeviceError(str(exc))
    AUDIT.log(me(), "sites_interval", seconds=sec)
    return jsonify(interval=sec)


@app.post("/api/sites/run")
def sites_run():
    """Check every site now (with traces); at most once every 30 seconds."""
    if r_demo():
        return jsonify(started=True)
    return jsonify(started=SITES.check_now())


@app.post("/api/sites/check")
def sites_check():
    """Check any site now (the search on the Major sites map): delay, loss and path, from this server through the router."""
    host = (request.get_json(force=True) or {}).get("host")
    try:
        return jsonify(sitewatch.demo_lookup(host) if r_demo() else SITES.lookup(host))
    except ValueError as exc:
        raise DeviceError(str(exc))


IPAM = ipam.Plans(DATA_DIR)


def ipam_now():
    """This router's IP plan and the addresses in use on it now."""
    r = router()
    data, _ = VIEWCACHE.get(r, "interfaces")
    return netmap_key(r), IPAM.get(netmap_key(r)), ipam.used_from(data if isinstance(data, list) else (data or {}).get("interfaces", []))


def ipam_own():
    """The networks this router announces in BGP (its own address space), if BGP has been read."""
    try:
        data, _ = VIEWCACHE.get(router(), "bgp")
        return ipam.announced((data or {}).get("config"))
    except Exception:
        return []


@app.get("/api/ipam")
def ipam_get():
    """IP plan: blocks, segments, bookings, what is in use on the router, each block's numbers and free gaps, and advice."""
    key, plan, used = ipam_now()
    suggested = not plan["blocks"]
    if suggested:
        plan = {**plan, "blocks": [{"cidr": c, "name": "Announced by the router" if own else "Public block (found on the router)"} for own in [ipam_own()] for c in ipam.suggest_blocks(used, own)]}
    return jsonify(plan=plan, suggested=suggested, used=used, purposes=ipam.PURPOSES, stats=[ipam.stats(b["cidr"], plan, used) for b in plan["blocks"]],
                   findings=ipam.findings(plan, used))


@app.post("/api/ipam")
def ipam_save():
    """Save the blocks and segments (the bookings are kept as they are)."""
    key, cur, used = ipam_now()
    d = request.get_json(force=True) or {}
    try:
        plan = ipam.check_plan({"blocks": d.get("blocks"), "pools": d.get("pools"), "allocs": cur["allocs"]})
    except ValueError as exc:
        raise DeviceError(str(exc))
    IPAM.put(key, plan)
    AUDIT.log(me(), "ipam_plan", blocks=len(plan["blocks"]), segments=len(plan["pools"]))
    return jsonify(plan=plan)


@app.post("/api/ipam/allocate")
def ipam_allocate():
    """Book the next free subnet of a size in a segment or block: {where, plen, name, note}. Only the plan changes."""
    key, plan, used = ipam_now()
    d = request.get_json(force=True) or {}
    if not plan["blocks"]:
        plan["blocks"] = [{"cidr": c, "name": "Public block (found on the router)"} for c in ipam.suggest_blocks(used, ipam_own())]
    if not str(d.get("name") or "").strip():
        raise DeviceError("Say who or what the addresses are for.")
    try:
        a = ipam.allocate(plan, used, str(d.get("where") or ""), d.get("plen") or 30, d.get("name"), d.get("note"), me())
    except (ValueError, TypeError) as exc:
        raise DeviceError(str(exc))
    IPAM.put(key, plan)
    AUDIT.log(me(), "ipam_allocate", subnet=a["cidr"], name=a["name"])
    return jsonify(alloc=a, hosts=ipam.hosts(a["cidr"]))


@app.delete("/api/ipam/allocs/<aid>")
def ipam_free(aid):
    key, plan, used = ipam_now()
    gone = [a for a in plan["allocs"] if a["id"] == aid]
    plan["allocs"] = [a for a in plan["allocs"] if a["id"] != aid]
    IPAM.put(key, plan)
    AUDIT.log(me(), "ipam_free", subnet=gone[0]["cidr"] if gone else aid)
    return jsonify(ok=True)


@app.post("/api/ipam/layout")
def ipam_layout():
    """A suggested split of a block into segments (not saved): {block, regions}."""
    d = request.get_json(force=True) or {}
    try:
        return jsonify(pools=ipam.layout(d.get("block"), [str(x)[:30] for x in (d.get("regions") or [])]))
    except ValueError as exc:
        raise DeviceError(str(exc))


@app.post("/api/ipam/renumber")
def ipam_renumber():
    """Where each subnet would go in the plan (nothing is booked or changed): {moves: {subnet: segment id}}."""
    key, plan, used = ipam_now()
    return jsonify(moves=ipam.renumber(plan, used, (request.get_json(force=True) or {}).get("moves") or {}))


GEO_PATH = os.path.join(DATA_DIR, "netmap_places.json")
GEO_LOCK = threading.Lock()


def geo_load():
    try:
        with open(GEO_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


@app.get("/api/netmap/places")
def netmap_places():
    """Where each device of the map sits on the geographic NOC map (a town, set by hand), shared by everyone."""
    r = router()
    got = geo_load().get(netmap_key(r))
    if got is None and r.demo:      # the demo network spread over Kenya, so the map has something to show
        got = dict(DEMO_PLACES)
    return jsonify(places=got or {})


@app.post("/api/netmap/places")
def netmap_places_set():
    """Place devices: {"places": {node_id: "Town" or "" to go back to automatic}}. Only this app's data changes."""
    r = router()
    key, d = netmap_key(r), (request.get_json(force=True) or {}).get("places") or {}
    with GEO_LOCK:
        allp = geo_load()
        cur = allp.setdefault(key, dict(DEMO_PLACES) if r.demo else {})
        for k, v in list(d.items())[:2000]:
            v = str(v or "").strip()[:60]
            if v:
                cur[str(k)[:200]] = v
            else:
                cur.pop(str(k), None)
        tmp = GEO_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(allp, f, indent=1)
        os.replace(tmp, GEO_PATH)
    AUDIT.log(me(), "map_places", count=len(d))
    return jsonify(places=cur)


SITEREG = sitereg.Registry(DATA_DIR)


@app.get("/api/siteregistry")
def site_list():
    """Every site: its registration (and what is missing), the items watched there with up/down, alarms, and its saved routers."""
    return jsonify(sites=SITEREG.report(SAVED.list()), types=sitereg.TYPES, power=sitereg.POWER, kinds=sitereg.KINDS,
                   required=[k for k, _ in sitereg.REQUIRED], events=sitereg.EVENTS, unassigned=[r for r in SAVED.list() if not r.get("site")])


@app.post("/api/siteregistry")
def site_add():
    try:
        s = SITEREG.save(request.get_json(force=True) or {})
    except ValueError as exc:
        raise DeviceError(str(exc))
    AUDIT.log(me(), "site_added", site=s["code"], name=s["name"])
    return jsonify(site=s, missing=sitereg.missing(s))


@app.put("/api/siteregistry/<sid>")
def site_update(sid):
    try:
        s = SITEREG.save(request.get_json(force=True) or {}, sid)
    except ValueError as exc:
        raise DeviceError(str(exc))
    AUDIT.log(me(), "site_changed", site=s["code"], name=s["name"])
    return jsonify(site=s, missing=sitereg.missing(s))


@app.delete("/api/siteregistry/<sid>")
def site_delete(sid):
    try:
        s = SITEREG.delete(sid, attached=sum(1 for r in SAVED.list() if r.get("site") == sid))
    except ValueError as exc:
        raise DeviceError(str(exc))
    AUDIT.log(me(), "site_deleted", site=s["code"], name=s["name"])
    return jsonify(ok=True)


@app.post("/api/siteregistry/<sid>/assets")
def site_asset_add(sid):
    d = request.get_json(force=True) or {}
    try:
        a = SITEREG.save_asset(sid, d, str(d.get("id") or "") or None)
    except ValueError as exc:
        raise DeviceError(str(exc))
    AUDIT.log(me(), "site_item", site=sid, item=a["name"], kind=a["kind"])
    threading.Thread(target=SITEREG.check, daemon=True).start()
    return jsonify(asset=a)


@app.delete("/api/siteregistry/<sid>/assets/<aid>")
def site_asset_delete(sid, aid):
    try:
        SITEREG.delete_asset(sid, aid)
    except ValueError as exc:
        raise DeviceError(str(exc))
    AUDIT.log(me(), "site_item_deleted", site=sid, item=aid)
    return jsonify(ok=True)


@app.get("/api/siteregistry/<sid>/events")
def site_events(sid):
    return jsonify(events=SITEREG.events(sid, 200))


@app.post("/api/site-events")
def site_event():
    """An alarm from a site's power, door, camera or sensor system: {"site": "MBT-01" (code or id), "kind": "power|access|cctv|env|device",
    "event": "mains_fail|on_battery|battery_low|generator_on|mains_ok|door_forced|door_held|access_denied|door_open|door_closed|
    camera_offline|recording_stopped|disk_full|camera_ok|temp_high|water|smoke|env_ok|...", "detail": "", "asset": "UPS-1", "source": "APC"}.
    The system signs in with the header Authorization: Bearer <NE8000_SITES_TOKEN>; a signed-in admin or operator can post too."""
    d = request.get_json(force=True) or {}
    tok = os.environ.get("NE8000_SITES_TOKEN", "")
    if tok and hmac.compare_digest(request.headers.get("Authorization", "").encode(), ("Bearer " + tok).encode()):
        who = "site-feed"
    else:
        u = current_user()
        if not u:
            return jsonify(error="Please log in, or send the sites token."), 401
        if request.headers.get("X-Requested-With") != "ne8000":
            return jsonify(error="Missing request header."), 403
        if u["role"] == "viewer":
            return jsonify(error="Your account is read-only."), 403
        who = me()
    try:
        e = SITEREG.add_event(d.get("site"), d.get("kind"), d.get("event"), d.get("detail"), d.get("asset"), d.get("source") or who)
    except ValueError as exc:
        raise DeviceError(str(exc))
    AUDIT.log(who, "site_event", site=e["site"], kind=e["kind"], what=e["event"])
    return jsonify(event=e)


@app.post("/api/otdr")
def otdr_event():
    """A fibre event from an OTDR / fibre monitoring system, shown on the health page and the NOC map:
    {"link": "GigabitEthernet0/1/6.301" or a line label, "event": "break|loss|bend|reflectance|ok", "distance_km": 12.4,
     "loss_db": 1.8, "source": "EXFO FG-750", "note": "", "router": "<router address>"}. "ok" clears that route.
    A monitoring system signs in with the header Authorization: Bearer <NE8000_OTDR_TOKEN>; a signed-in admin can post too."""
    d = request.get_json(force=True) or {}
    tok = os.environ.get("NE8000_OTDR_TOKEN", "")
    if tok and hmac.compare_digest(request.headers.get("Authorization", "").encode(), ("Bearer " + tok).encode()):
        who = "otdr-feed"
        rid = str(d.get("router_id") or "") or next((x["id"] for x in SAVED.list() if x.get("host") == d.get("router")), "")
    else:
        u = current_user()
        if not u:
            return jsonify(error="Please log in, or send the OTDR token."), 401
        if request.headers.get("X-Requested-With") != "ne8000":
            return jsonify(error="Missing request header."), 403
        if u["role"] != "admin":
            return jsonify(error="Only admins can do this."), 403
        who, rid = me(), session.get("router_id", "")
    if not rid:
        raise DeviceError("Say which router the fibre belongs to (router: its address), or connect to a saved router.")
    try:
        row = HEALTH.otdr_add(rid, d)
    except (ValueError, TypeError) as exc:
        raise DeviceError(str(exc))
    AUDIT.log(who, "otdr_event", target=row["link"], event=row["event"], km=row["km"])
    return jsonify(event=row)


@app.post("/api/plans/auto")
def plan_auto():
    """Create a plan from the live network: kind 'discover' (draw what is there) or 'design'
    (carrier-grade review with proposed devices and draft tasks). Reads only."""
    r = router()
    kind = request.get_json(force=True).get("kind")
    eng = getattr(r, "billing", None)
    snap = netdesign.snapshot(r, ((eng.last if eng else None) or {}).get("rows"))
    stamp = time.strftime("%Y-%m-%d %H:%M")
    if kind == "discover":
        diagram, summary, _ = netdesign.discover(snap)
        p = PLANS.create(f"Network as built: {snap['sysname']} ({stamp})", summary, me(), diagram, [])
    elif kind == "design":
        diagram, desc, tasks, _ = netdesign.design(snap)
        p = PLANS.create(f"Carrier-grade design: {snap['sysname']} ({stamp})", desc, me(), diagram, tasks)
    else:
        raise DeviceError("Unknown kind.")
    AUDIT.log(me(), "plan_auto", kind=kind, plan=p["id"])
    return jsonify(id=p["id"])


@app.get("/api/plans/<pid>")
def plan_get(pid):
    router()
    return jsonify(PLANS.get(pid))


@app.put("/api/plans/<pid>")
def plan_save(pid):
    router()
    d = request.get_json(force=True)
    return jsonify(PLANS.save(pid, d, note=d.get("note", ""), author=me()))


@app.delete("/api/plans/<pid>")
def plan_delete(pid):
    router()
    AUDIT.log(me(), "plan_deleted", plan=pid, name=PLANS.get(pid)["name"])
    PLANS.delete(pid)
    return jsonify(ok=True)


@app.post("/api/plans/<pid>/duplicate")
def plan_duplicate(pid):
    router()
    return jsonify(PLANS.duplicate(pid, me()))


@app.post("/api/plans/<pid>/restore")
def plan_restore(pid):
    router()
    d = request.get_json(force=True)
    return jsonify(PLANS.restore(pid, d.get("index"), me()))


@app.get("/api/plans/<pid>/preview")
def plan_preview(pid):
    router()
    return jsonify(tasks=plans.preview(PLANS.get(pid)))


@app.post("/api/plans/<pid>/execute")
def plan_execute(pid):
    r = router()
    commit = bool(request.get_json(force=True).get("commit"))
    plan = PLANS.get(pid)
    if not plan["tasks"]:
        raise plans.PlanError("This plan has no tasks to execute.")
    AUDIT.log(me(), "plan_execute", plan=pid, name=plan["name"], host=r.host, demo=r.demo, commit=commit)
    run = plans.execute(plan, r, commit)
    run["by"] = me()
    return jsonify(plan=PLANS.add_run(pid, run), run=run)


def add_user_cli(name, role):
    import getpass
    pw = getpass.getpass(f"Password for {name}: ")
    if pw != getpass.getpass("Again: "):
        raise SystemExit("Passwords do not match.")
    if USERS.get(name):
        USERS.update(name, password=pw, role=role)
        print(f"Updated {name} ({role}).")
    else:
        USERS.add(name, pw, role)
        print(f"Added {name} ({role}).")


def first_run_admin():
    created = USERS.ensure_admin()
    if created and created[1]:
        print("=" * 64)
        print(f"  First start: created app login  {created[0]}  /  {created[1]}")
        print("  Sign in and change it under Manager > My account.")
        print("=" * 64, flush=True)


if "--add-user" not in sys.argv:
    first_run_admin()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="NE8000 M8 management GUI")
    parser.add_argument("--host", default=os.environ.get("NE8000_HOST", "127.0.0.1"), help="address to listen on (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=int(os.environ.get("NE8000_PORT", "5000")))
    parser.add_argument("--threads", type=int, default=int(os.environ.get("NE8000_THREADS", "16")))
    parser.add_argument("--dev", action="store_true", help="use the Flask development server")
    parser.add_argument("--add-user", metavar="NAME", help="create an app login (or reset its password) and exit")
    parser.add_argument("--role", default="admin", choices=auth.ROLES)
    args = parser.parse_args()
    if args.add_user:
        add_user_cli(args.add_user, args.role)
        raise SystemExit(0)
    if env_on("NE8000_HISTORY", "1"):
        HIST.start()   # 5-minute interface history for every saved router with an SNMP login
    if env_on("NE8000_HEALTH", "1"):
        HEALTH.start()
        ALERTS.start()      # alerts: send problems to Telegram, email, SMS, Slack/Teams/webhook every minute
        serverinfo.start(DATA_DIR)  # server health: CPU, memory and disk use every minute
        SITEREG.start()     # sites: ping the devices, power, door and camera systems registered at each, every minute
        SITES.start()   # major sites: ping and trace to Google, YouTube, Facebook... every 5 minutes
    if env_on("NE8000_CHANGES", "1"):
        TRACK.start()   # 24/7 change log over SNMP for every saved router with an SNMP login  # smart health check every 5 minutes (errors, drops, full links, optics, latency)
    if int(os.environ.get("NE8000_NETFLOW_PORT", "2055") or 0):
        try:            # per-address traffic from the routers' NetStream export (UDP)
            FLOWS.start(int(os.environ.get("NE8000_NETFLOW_PORT", "2055")), os.environ.get("NE8000_NETFLOW_BIND", "0.0.0.0"))
        except OSError as exc:
            print(f"NetFlow collector not started: {exc}", flush=True)
    print(f"Data folder: {DATA_DIR}")
    print(f"Open http://{'127.0.0.1' if args.host in ('0.0.0.0', '::') else args.host}:{args.port} in your browser", flush=True)
    try:
        if args.dev:
            raise ImportError
        from waitress import serve
    except ImportError:
        if BEHIND_PROXY:
            from werkzeug.middleware.proxy_fix import ProxyFix
            app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
        app.run(host=args.host, port=args.port, threaded=True)
    else:
        # one process with many threads: router sessions and graphs live in this process's memory
        proxy = dict(trusted_proxy=os.environ.get("NE8000_TRUSTED_PROXY", "*"), trusted_proxy_count=1,
                     trusted_proxy_headers={"x-forwarded-for", "x-forwarded-proto", "x-forwarded-host"},
                     clear_untrusted_proxy_headers=True) if BEHIND_PROXY else {}
        serve(app, host=args.host, port=args.port, threads=args.threads, ident="NE8000 Manager", **proxy)
