"""NE8000 Manager: a Winbox-style web GUI for Huawei NetEngine 8000 M8 routers.

Run:  python app.py                          (http://127.0.0.1:5000)
      python app.py --host 0.0.0.0 --port 8080   (reachable from your LAN)
      python app.py --add-user NAME --role admin  (create or reset an app login)

Settings for hosting come from environment variables (see README, "Hosting").
"""
import argparse
import os
import datetime
import ipaddress
import re
import sys
import time
import uuid

from flask import Flask, Response, jsonify, render_template, request, session

import actions
import auth
import billing
import adopt
import history
import ipdisabled
import netdesign
import netflow
import netmap
import plans
import routers
from device import DeviceError, RealRouter, is_read_only
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
FLOWS = netflow.FlowCollector(SAVED, HIST, default_sampling=os.environ.get("NE8000_NETFLOW_SAMPLING", "1000"))
NETMAP = netmap.NetMapStore(DATA_DIR)
NETMAP_CACHE = {}   # map key -> (time, response)
DISABLED_IPS = ipdisabled.DisabledIPs(DATA_DIR)
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
         "system", "hardware", "logs")


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
OPEN = {"index", "static", "auth_login", "auth_me", "healthz"}
# endpoints a viewer may POST to; everything else that changes something needs operator or admin
VIEWER_POST = {"connect", "connect_ssh", "disconnect", "cli", "run_action", "auth_logout", "auth_password"}
ADMIN_ONLY = {"users_list", "users_add", "users_update", "users_delete", "audit_log", "routers_add", "routers_update", "routers_delete",
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
    if request.path.startswith("/api/"):
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


@app.post("/api/routers")
def routers_add():
    r = SAVED.add(request.get_json(force=True))
    AUDIT.log(me(), "router_added", name=r["name"], host=r["host"], port=r["port"])
    return jsonify(routers=SAVED.list())


@app.put("/api/routers/<rid>")
def routers_update(rid):
    r = SAVED.update(rid, request.get_json(force=True))
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
                               "snmp_priv_proto": f.get("snmp_priv_proto"), "snmp_priv_key": f.get("snmp_priv_key")})
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
                AUDIT.log(me(), "router_connect_failed", host=host, ip=client_ip())
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
    except DeviceError:
        THROTTLE_ROUTER.fail(me())
        AUDIT.log(me(), "router_connect_failed", host=r.host, ip=client_ip())
        raise
    THROTTLE_ROUTER.ok(me())
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
    data = getattr(router(), name)()
    if name == "system" and current_user()["role"] == "viewer":
        data["snmp"] = "(hidden for read-only accounts)"
    return jsonify(data)


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


def do_action(data):
    built = actions.build(data.get("action", ""), data.get("params") or {})
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
    for secret in [str(v) for k, v in (data.get("params") or {}).items() if k in actions.SECRET_KEYS and v]:
        out = out.replace(secret, "********")
    return dict(output=out, lines=built["preview"])


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
    return jsonify(output=r.configure(lines, commit=bool(data.get("commit", True))))


@app.post("/api/save")
def save():
    r = router()
    AUDIT.log(me(), "save", host=r.host, demo=r.demo)
    return jsonify(output=r.execute("save"))


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
    ifs = {i["name"]: i for i in r.interfaces()} if names else {}
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
    if not FLOWS.routers.get(rid):
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


def netmap_key(r):
    return "demo" if r.demo else (session.get("router_id") or "host-" + r.host)


@app.get("/api/netmap")
def netmap_view():
    """The live as-built map of the connected router. Rebuilt at most once a minute unless refresh=1."""
    r = router()
    key = netmap_key(r)
    hit = NETMAP_CACHE.get(key)
    if hit and time.time() - hit[0] < 60 and request.args.get("refresh") != "1":
        return jsonify({**hit[1], "changes": NETMAP.load(key)["changes"][-100:][::-1]})
    eng = getattr(r, "billing", None)
    snap = netdesign.snapshot(r, ((eng.last if eng else None) or {}).get("rows"))
    diagram, summary, m = netdesign.discover(snap)
    rec = NETMAP.update(key, netmap.inventory(snap, m))
    out = {"key": key, "diagram": diagram, "summary": summary, "at": rec["at"], "warnings": snap["warnings"],
           "rates": isinstance(r, SnmpRouter) or r.demo}
    NETMAP_CACHE[key] = (time.time(), out)
    return jsonify({**out, "changes": rec["changes"][-100:][::-1]})


@app.get("/api/netmap/traffic")
def netmap_traffic():
    """Current rate (SNMP or demo) or utilisation of the interfaces on the map, and BGP session states."""
    r = router()
    hit = NETMAP_CACHE.get(netmap_key(r))
    if not hit:
        return jsonify(ifs={}, peers={})
    names = sorted({l["props"]["iface"] for l in hit[1]["diagram"]["links"] if l["props"].get("iface")})
    ifs = {i["name"]: i for i in r.interfaces()}
    series = r.traffic(names) if r.demo else {n: r.series("bps:" + n) for n in names} if isinstance(r, SnmpRouter) else {}
    out = {}
    for n in names:
        i, pts = ifs.get(n, {}), series.get(n) or []
        out[n] = {"phy": i.get("phy", ""), "in_util": i.get("in_util"), "out_util": i.get("out_util"),
                  "rx": pts[-1][1] if pts else None, "tx": pts[-1][2] if pts else None}
    peers = {}
    try:
        peers = {p["peer"]: p["state"] for p in r.bgp()["peers"]}
    except Exception:
        pass
    return jsonify(ifs=out, peers=peers, at=time.time())


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
