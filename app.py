"""NE8000 Manager: a Winbox-style web GUI for Huawei NetEngine 8000 M8 routers.

Run:  python app.py                          (http://127.0.0.1:5000)
      python app.py --host 0.0.0.0 --port 8080   (reachable from your LAN)
      python app.py --add-user NAME --role admin  (create or reset an app login)

Settings for hosting come from environment variables (see README, "Hosting").
"""
import argparse
import os
import datetime
import re
import sys
import time
import uuid

from flask import Flask, Response, jsonify, render_template, request, session

import actions
import auth
import billing
import plans
from device import DeviceError, RealRouter, is_read_only

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
THROTTLE_IP = auth.Throttle(limit=5)        # per client address
THROTTLE_USER = auth.Throttle(limit=20)     # per username, high enough that strangers cannot easily lock out a real user
THROTTLE_ROUTER = auth.Throttle(limit=5)    # failed router SSH logins per app user (slows password guessing on the router)


def allowed_routers():
    """NE8000_ALLOWED_ROUTERS: comma-separated IPs, subnets or hostnames the app may connect to (empty = any)."""
    return [x.strip() for x in os.environ.get("NE8000_ALLOWED_ROUTERS", "").split(",") if x.strip()]


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
    raise DeviceError("This server is not allowed to connect to that router. Allowed: " + ", ".join(allow))

ROUTERS = {}   # browser session id -> Router. Credentials are never stored.
NAME_RE = re.compile(r"^[A-Za-z0-9/.:\-_]{1,64}$")

VIEWS = ("overview", "interfaces", "vlans", "qos", "arp", "neighbors", "vpn_instances", "bgp", "ospf", "mpls",
         "system", "hardware", "logs")


def router():
    r = ROUTERS.get(session.get("sid"))
    if r is None:
        raise DeviceError("Not connected.")
    return r


def same_router(r):
    return [x for x in ROUTERS.values() if x is not r and not x.demo and not r.demo and x.host == r.host]


def start_billing(r):
    """Only one billing loop per router, however many people are connected to it."""
    if not any(x.billing.thread and x.billing.thread.is_alive() and not x.billing.stop.is_set() for x in same_router(r)):
        r.billing.start()


def close_router(r):
    was_running = bool(getattr(r, "billing", None) and r.billing.thread and r.billing.thread.is_alive() and not r.billing.stop.is_set())
    if getattr(r, "billing", None):
        r.billing.close()
    r.close()
    others = same_router(r)
    if was_running and others:
        others[0].billing.start()


# ---- app login ---------------------------------------------------------------------
OPEN = {"index", "static", "auth_login", "auth_me", "healthz"}
# endpoints a viewer may POST to; everything else that changes something needs operator or admin
VIEWER_POST = {"connect", "disconnect", "cli", "run_action", "auth_logout", "auth_password"}


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
    if request.endpoint in ("users_list", "users_add", "users_update", "users_delete", "audit_log") and u["role"] != "admin":
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
    return jsonify(user={"username": u["username"], "role": u["role"]}, allow_demo=ALLOW_DEMO,
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
    if data.get("demo"):
        if not ALLOW_DEMO:
            raise DeviceError("Demo mode is turned off on this server.")
        from demo import DemoRouter
        r = DemoRouter((data.get("host") or "").strip() or "demo")
    else:
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
        THROTTLE_ROUTER.check(me())
        try:
            r = RealRouter(host, data.get("username", ""), data.get("password", ""), port)
        except DeviceError:
            THROTTLE_ROUTER.fail(me())
            AUDIT.log(me(), "router_connect_failed", host=host, ip=client_ip())
            raise
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
    ROUTERS[session["sid"]] = r
    start_billing(r)
    AUDIT.log(me(), "router_connect", host=r.host, demo=r.demo, ip=client_ip())
    return jsonify(ok=True, host=r.host, demo=r.demo)


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
    return jsonify(connected=r is not None, host=r.host if r else None, demo=r.demo if r else False)


@app.get("/api/view/<name>")
def view(name):
    if name not in VIEWS:
        raise DeviceError("Unknown view.")
    return jsonify(getattr(router(), name)())


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
    return jsonify(router().routes(safe_name(request.args.get("vpn") or None, "VPN instance")))


@app.get("/api/bgp/routes")
def bgp_routes():
    peer = request.args.get("peer") or None
    if peer and not re.match(r"^[\d.:a-fA-F]+$", peer):
        raise DeviceError("Invalid peer.")
    return jsonify(router().bgp_routes(peer, request.args.get("which", "received")))


@app.post("/api/action")
def run_action():
    data = request.get_json(force=True)
    built = actions.build(data.get("action", ""), data.get("params") or {})
    if data.get("preview"):
        return jsonify(lines=built["preview"], mode=built["mode"])
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
    return jsonify(output=out, lines=built["preview"])


@app.post("/api/cli")
def cli():
    command = (request.get_json(force=True).get("command") or "").strip()
    if not is_read_only(command):
        raise DeviceError("The console runs display, ping and tracert only. Use Config editor for changes.")
    if current_user()["role"] == "viewer" and "configuration" in command.lower():
        raise auth.AuthError("Viewers cannot read the full configuration.")
    return jsonify(output=router().run(command, read_timeout=120))


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
