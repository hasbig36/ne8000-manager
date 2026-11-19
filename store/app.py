"""Yubo licence store: the public sales site and the billing site.

One small Flask app. With STORE_SALES_HOST and STORE_BILLING_HOST set (for example
ne8000.co.ke and billing.ne8000.co.ke) the sales pages answer on the first and the billing
pages and APIs on the second; without them everything answers on one address.

Run:  python app.py   (settings in the environment, see .env.example)
"""
import functools
import hmac
import os
import secrets
import time

from flask import Flask, Response, abort, jsonify, redirect, render_template, request, session, url_for

import pricing
from mpesa import parse_callback
from service import Store, StoreError, logo_mime, period_label

DATA_DIR = os.path.abspath(os.environ.get("STORE_DATA_DIR") or os.path.join(os.path.dirname(__file__), "data"))
SALES_HOST = os.environ.get("STORE_SALES_HOST", "").lower()
BILLING_HOST = os.environ.get("STORE_BILLING_HOST", "").lower()
ADMIN_PASSWORD = os.environ.get("STORE_ADMIN_PASSWORD", "")
BEHIND_PROXY = os.environ.get("STORE_BEHIND_PROXY", "0") in ("1", "true", "yes")

app = Flask(__name__)
STORE = Store(DATA_DIR)


def _secret_key():
    path = os.path.join(DATA_DIR, "session_key")
    if not os.path.exists(path):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_hex(32))
    with open(path) as f:
        return f.read().strip()


app.secret_key = _secret_key()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=BEHIND_PROXY,
                  PERMANENT_SESSION_LIFETIME=7 * 86400, MAX_CONTENT_LENGTH=1024 * 1024)
if BEHIND_PROXY:
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

SALES = {"home", "buy", "buy_post", "api_quote"}
SHARED = {"static", "healthz"}
FAILS = {}


@app.context_processor
def common():
    return {"store_name": STORE.name, "period": pricing.PERIOD, "period_label": period_label(),
            "first_usd": pricing.FIRST, "extra_usd": pricing.EXTRA, "markup": pricing.MARKUP,
            "kes_rate": pricing.KES_PER_USD, "csrf": csrf_token(), "me": current_customer(),
            "sales_url": f"https://{SALES_HOST}" if SALES_HOST else "", "billing_url": f"https://{BILLING_HOST}" if BILLING_HOST else "",
            "fmt_date": lambda t: time.strftime("%d %b %Y", time.gmtime(t)) if t else "-"}


@app.before_request
def route_host():
    if not (SALES_HOST and BILLING_HOST) or request.endpoint in SHARED:
        return None
    host = request.host.split(":")[0].lower()
    if request.endpoint in SALES and host != SALES_HOST:
        return redirect(f"https://{SALES_HOST}{request.full_path.rstrip('?')}")
    if request.endpoint not in SALES and host != BILLING_HOST:
        return redirect(f"https://{BILLING_HOST}{request.full_path.rstrip('?')}")
    return None


@app.after_request
def headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    resp.headers.setdefault("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; "
                            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
    return resp


# ----------------------------------------------------------------------------- helpers
def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(24)
    return session["csrf"]


def check_csrf():
    sent = request.form.get("csrf") or request.headers.get("X-CSRF") or ""
    if not hmac.compare_digest(sent, session.get("csrf", "") or "-"):
        abort(400, "The form expired. Go back and try again.")


def throttle(key):
    t = time.time()
    hits = [x for x in FAILS.get(key, []) if t - x < 900]
    FAILS[key] = hits
    if len(hits) >= 8:
        abort(429, "Too many attempts. Wait 15 minutes.")


def read_logo():
    f = request.files.get("logo")
    return f.read(512 * 1024 + 1) if f and f.filename else b""


def client_ip():
    return request.remote_addr or "-"


def current_customer():
    cid = session.get("cid")
    return STORE.customer(cid) if cid else None


def customer_required(f):
    @functools.wraps(f)
    def wrapper(*a, **kw):
        if not current_customer():
            return redirect(url_for("login", next=request.path))
        return f(*a, **kw)
    return wrapper


def admin_required(f):
    @functools.wraps(f)
    def wrapper(*a, **kw):
        if not (ADMIN_PASSWORD and session.get("admin")):
            return redirect(url_for("admin_login"))
        return f(*a, **kw)
    return wrapper


def own_order(ref):
    o = STORE.order_by_ref(ref)
    me = current_customer()
    if not o or not (session.get("admin") or (me and o["customer_id"] == me["id"])):
        abort(404)
    return o


def own_licence(lid):
    lic = STORE.licence(lid)
    me = current_customer()
    if not lic or not me or lic["customer_id"] != me["id"]:
        abort(404)
    return lic


@app.errorhandler(StoreError)
def store_error(exc):
    if request.path.startswith("/api/"):
        return jsonify(error=str(exc)), 400
    return render_template("message.html", title="Something is not right", text=str(exc)), 400


@app.errorhandler(pricing.PriceError)
def price_error(exc):
    return store_error(StoreError(str(exc)))


# ----------------------------------------------------------------------------- sales site
@app.get("/healthz")
def healthz():
    return "ok"


@app.get("/")
def home():
    if BILLING_HOST and request.host.split(":")[0].lower() == BILLING_HOST:
        return redirect(url_for("account"))
    sizes = [{"max": m, "label": lbl, "hosted": pricing.server_for(m, STORE.do.prices())["hosted_monthly"]}
             for m, _, lbl, _ in pricing.SIZES]
    return render_template("home.html", sizes=sizes, device_types=pricing.DEVICE_TYPES)


@app.get("/buy")
def buy():
    return render_template("buy.html", device_types=pricing.DEVICE_TYPES, max_devices=pricing.MAX_DEVICES,
                           mpesa=STORE.mpesa.configured, hosting_ready=STORE.do.configured)


@app.post("/api/quote")
def api_quote():
    d = request.get_json(force=True, silent=True) or {}
    try:
        n = sum(x["count"] for x in pricing.clean_devices(d.get("devices")))
    except pricing.PriceError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(STORE.quote(n, d.get("hosting") or "self"))


@app.post("/buy")
def buy_post():
    check_csrf()
    throttle(("buy", client_ip()))
    FAILS.setdefault(("buy", client_ip()), []).append(time.time())
    f = request.form
    devices = [{"type": t, "count": f.get(f"count_{t}", "0") or "0"} for t in pricing.DEVICE_TYPES]
    if f.get("hosting") == "hosted" and not STORE.do.configured:
        raise StoreError("Hosting by us is not available yet. Choose your own server, or contact us.")
    if not f.get("terms"):
        raise StoreError("Please accept the licence terms.")
    o = STORE.checkout({"devices": devices, "hosting": f.get("hosting"), "email": f.get("email"), "name": f.get("name"),
                        "company": f.get("company"), "phone": f.get("phone"), "country": f.get("country"),
                        "password": f.get("password"), "brand": f.get("brand"), "logo": read_logo()})
    session.clear()
    session.permanent = True
    session["cid"] = o["customer_id"]
    return redirect(url_for("pay", ref=o["ref"]))


# ----------------------------------------------------------------------------- billing site
@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    if request.method == "POST":
        check_csrf()
        throttle(("login", client_ip()))
        try:
            c = STORE.login(request.form.get("email"), request.form.get("password"))
        except StoreError as exc:
            FAILS.setdefault(("login", client_ip()), []).append(time.time())
            error = str(exc)
        else:
            session.clear()
            session.permanent = True
            session["cid"] = c["id"]
            nxt = request.args.get("next") or ""
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("account"))
    return render_template("login.html", error=error)


@app.post("/logout")
def logout():
    check_csrf()
    session.clear()
    return redirect(url_for("login"))


@app.get("/account")
@customer_required
def account():
    me = current_customer()
    lics = STORE.db.q("SELECT * FROM licences WHERE customer_id=? ORDER BY id", me["id"])
    for lic in lics:
        lic["server"] = STORE.server_for_licence(lic["id"])
        lic["renewal_usd"] = STORE.renewal_amount(lic)
        if lic["hosting"] == "self" and lic["status"] == "active":
            lic["install"] = STORE.install_command(lic)
    orders = STORE.db.q("SELECT * FROM orders WHERE customer_id=? ORDER BY id DESC", me["id"])
    return render_template("account.html", licences=lics, orders=orders, extra=pricing.EXTRA)


@app.post("/account/licence/<int:lid>/renew")
@customer_required
def renew(lid):
    check_csrf()
    return redirect(url_for("pay", ref=STORE.renew_order(own_licence(lid))["ref"]))


@app.post("/account/licence/<int:lid>/devices")
@customer_required
def add_devices(lid):
    check_csrf()
    return redirect(url_for("pay", ref=STORE.add_devices_order(own_licence(lid), request.form.get("added"))["ref"]))


@app.post("/account/licence/<int:lid>/brand")
@customer_required
def set_brand(lid):
    check_csrf()
    STORE.set_brand(own_licence(lid), request.form.get("brand"), read_logo())
    return redirect(url_for("account"))


@app.get("/account/licence/<int:lid>/logo")
@customer_required
def licence_logo(lid):
    lic = own_licence(lid)
    if not lic.get("logo"):
        abort(404)
    return Response(lic["logo"], mimetype=logo_mime(lic["logo"]))


@app.get("/pay/<ref>")
@customer_required
def pay(ref):
    o = own_order(ref)
    lic = STORE.licence(o["licence_id"])
    return render_template("pay.html", o=o, lic=lic, bank=STORE.bank, mpesa=STORE.mpesa.configured,
                           me=current_customer(), quote=pricing.server_for(lic["devices"]) if lic else None,
                           sandbox=STORE.mpesa.env != "production")


@app.post("/pay/<ref>/mpesa")
@customer_required
def pay_mpesa(ref):
    check_csrf()
    throttle(("stk", client_ip()))
    FAILS.setdefault(("stk", client_ip()), []).append(time.time())
    o = own_order(ref)
    cid = STORE.pay_mpesa(o, request.form.get("phone"))
    return render_template("waiting.html", o=o, checkout_id=cid)


@app.get("/api/payment/<checkout_id>")
@customer_required
def payment_status(checkout_id):
    p = STORE.db.one("SELECT * FROM payments WHERE checkout_id=?", checkout_id)
    if not p:
        abort(404)
    own_order(STORE.order(p["order_id"])["ref"])
    if p["status"] == "pending" and time.time() - p["created"] > 15:
        p = STORE.mpesa_result(checkout_id)
    return jsonify(status=p["status"], note=p.get("note") or "")


@app.post("/pay/<ref>/bank")
@customer_required
def pay_bank(ref):
    check_csrf()
    o = own_order(ref)
    STORE.choose_bank(o)
    return redirect(url_for("pay", ref=ref, bank=1))


# ----------------------------------------------------------------------------- APIs for installs and providers
@app.get("/api/licence")
def api_licence():
    throttle(("lic", client_ip()))
    try:
        return jsonify(token=STORE.token(request.args.get("key", "")))
    except StoreError:
        FAILS.setdefault(("lic", client_ip()), []).append(time.time())
        raise


@app.get("/api/brand-logo")
def api_brand_logo():
    throttle(("lic", client_ip()))
    try:
        data = STORE.brand_logo(request.args.get("key", ""))
    except StoreError:
        FAILS.setdefault(("lic", client_ip()), []).append(time.time())
        raise
    return Response(data, mimetype=logo_mime(data))


@app.get("/api/pubkey")
def api_pubkey():
    return jsonify(pubkey=STORE.signer.public_b64)


@app.get("/api/install.sh")
def api_install():
    throttle(("lic", client_ip()))
    try:
        return Response(STORE.install_script(request.headers.get("X-Licence-Key", "")), mimetype="text/x-shellscript")
    except StoreError:
        FAILS.setdefault(("lic", client_ip()), []).append(time.time())
        raise


@app.get("/api/download")
def api_download():
    throttle(("lic", client_ip()))
    try:
        data = STORE.app_tarball(request.headers.get("X-Licence-Key", ""))
    except StoreError:
        FAILS.setdefault(("lic", client_ip()), []).append(time.time())
        raise
    return Response(data, mimetype="application/gzip", headers={"Content-Disposition": "attachment; filename=ne8000-manager.tar.gz"})


@app.post("/api/mpesa/callback/<secret>")
def mpesa_callback(secret):
    if not STORE.mpesa.callback_secret or not hmac.compare_digest(secret, STORE.mpesa.callback_secret):
        abort(404)
    checkout_id, code, receipt, _amount = parse_callback(request.get_json(force=True, silent=True))
    if checkout_id:
        STORE.record_receipt(checkout_id, receipt if code == 0 else "")
        STORE.mpesa_result(checkout_id)      # asks Daraja itself before trusting the result
    return jsonify(ResultCode=0, ResultDesc="Accepted")


@app.post("/api/provisioned/<token>")
def provisioned(token):
    d = request.get_json(force=True, silent=True) or {}
    STORE.provisioned(token, str(d.get("ip", ""))[:45], str(d.get("url", ""))[:200])
    return jsonify(ok=True)


# ----------------------------------------------------------------------------- admin
@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    error = "" if ADMIN_PASSWORD else "Set STORE_ADMIN_PASSWORD on the server to use the admin pages."
    if request.method == "POST" and ADMIN_PASSWORD:
        check_csrf()
        throttle(("admin", client_ip()))
        if hmac.compare_digest(request.form.get("password", ""), ADMIN_PASSWORD):
            session.clear()
            session["admin"] = True
            return redirect(url_for("admin"))
        FAILS.setdefault(("admin", client_ip()), []).append(time.time())
        error = "Wrong password."
    return render_template("admin_login.html", error=error)


@app.get("/admin")
@admin_required
def admin():
    orders = STORE.db.q("SELECT o.*, c.name, c.email, c.company FROM orders o JOIN customers c ON c.id=o.customer_id ORDER BY o.id DESC LIMIT 300")
    lics = STORE.db.q("SELECT l.*, c.name, c.email, c.company FROM licences l JOIN customers c ON c.id=l.customer_id ORDER BY l.id DESC")
    servers = STORE.db.q("SELECT s.id, s.licence_id, s.droplet_id, s.slug, s.region, s.ip, s.url, s.status, s.log, s.updated, l.key "
                         "FROM servers s JOIN licences l ON l.id=s.licence_id ORDER BY s.id DESC")
    payments = STORE.db.q("SELECT p.*, o.ref FROM payments p JOIN orders o ON o.id=p.order_id ORDER BY p.id DESC LIMIT 200")
    paid = STORE.db.one("SELECT COALESCE(SUM(amount_usd),0) AS usd, COUNT(*) AS n FROM orders WHERE status='paid' AND paid>?", time.time() - 30 * 86400)
    setup = {"M-Pesa": STORE.mpesa.configured, "DigitalOcean": STORE.do.configured, "Email (SMTP)": bool(STORE.mail.host),
             "Bank details": bool(STORE.bank["account_number"])}
    return render_template("admin.html", orders=orders, licences=lics, servers=servers, payments=payments, paid=paid, setup=setup,
                           pubkey=STORE.signer.public_b64)


@app.post("/admin/orders/<int:oid>/confirm")
@admin_required
def admin_confirm(oid):
    check_csrf()
    STORE.confirm_bank(oid, request.form.get("reference", ""), "admin")
    return redirect(url_for("admin"))


@app.post("/admin/licences/<int:lid>/provision")
@admin_required
def admin_provision(lid):
    check_csrf()
    lic = STORE.licence(lid)
    if not lic or lic["hosting"] != "hosted" or lic["status"] != "active":
        raise StoreError("Only active hosted licences get a server.")
    import threading
    threading.Thread(target=STORE.provision, args=(lid,), daemon=True).start()
    return redirect(url_for("admin"))


@app.post("/admin/logout")
def admin_logout():
    check_csrf()
    session.clear()
    return redirect(url_for("admin_login"))


if __name__ == "__main__":
    STORE.start()
    host = os.environ.get("STORE_HOST", "127.0.0.1")
    port = int(os.environ.get("STORE_PORT", "8090"))
    print(f"Licence store on http://{host}:{port}  (data: {DATA_DIR})", flush=True)
    if os.environ.get("STORE_DEV"):
        app.run(host=host, port=port, debug=False)
    else:
        from waitress import serve
        serve(app, host=host, port=port, threads=8)
