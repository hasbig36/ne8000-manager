"""What the store does: orders, payments, licences, installs and renewals."""
import io
import json
import os
import secrets
import tarfile
import threading
import time
import traceback

import pricing
from db import DB, dumps, new_key, new_ref, now
from digitalocean import DigitalOcean, DOError, cloud_init
from mailer import Mailer
from mpesa import Mpesa, MpesaError, normalise_phone
from signing import GRACE_DAYS, Signer
from werkzeug.security import check_password_hash, generate_password_hash

PERIOD_DAYS = {"month": 30, "year": 365, "once": 365 * 100}
HERE = os.path.dirname(os.path.abspath(__file__))


class StoreError(Exception):
    pass


def period_label():
    return {"month": "per month", "year": "per year", "once": "one-time"}.get(pricing.PERIOD, "")


class Store:
    def __init__(self, data_dir, env=os.environ):
        self.data_dir = data_dir
        self.env = env
        os.makedirs(data_dir, mode=0o700, exist_ok=True)
        self.db = DB(os.path.join(data_dir, "store.db"))
        self.signer = Signer(data_dir)
        self.mpesa = Mpesa(env)
        self.do = DigitalOcean(env)
        self.mail = Mailer(data_dir, env)
        self.public_url = env.get("STORE_PUBLIC_URL", "http://127.0.0.1:8090").rstrip("/")
        self.app_dir = os.path.abspath(env.get("STORE_APP_DIR") or os.path.join(HERE, ".."))
        self.bank = {k: env.get(f"BANK_{k.upper()}", "") for k in ("name", "account_name", "account_number", "branch", "swift", "paybill")}
        self.name = env.get("STORE_NAME", "NE8000 Manager")

    # ------------------------------------------------------------------ quotes
    def quote(self, devices, hosting):
        return pricing.quote(devices, hosting, self.do.prices())

    # ---------------------------------------------------------------- customers
    def customer_by_email(self, email):
        return self.db.one("SELECT * FROM customers WHERE email=?", email.strip().lower())

    def customer(self, cid):
        return self.db.one("SELECT * FROM customers WHERE id=?", cid)

    def login(self, email, password):
        c = self.customer_by_email(email or "")
        if not c or not check_password_hash(c["password_hash"], password or ""):
            raise StoreError("Wrong email or password.")
        return c

    def _new_customer(self, d):
        email = (d.get("email") or "").strip().lower()
        name = (d.get("name") or "").strip()
        pw = d.get("password") or ""
        if not (3 <= len(email) <= 120 and "@" in email and "." in email.split("@")[-1]) or any(c.isspace() for c in email):
            raise StoreError("Enter a valid email address.")
        if not 2 <= len(name) <= 80:
            raise StoreError("Enter your name.")
        if len(pw) < 10:
            raise StoreError("Choose a password of at least 10 characters for your billing account.")
        if self.customer_by_email(email):
            raise StoreError("There is already an account with that email. Sign in to the billing site to add devices.")
        return self.db.run("INSERT INTO customers(email,name,company,phone,country,password_hash,created) VALUES(?,?,?,?,?,?,?)",
                           email, name, (d.get("company") or "").strip()[:120], (d.get("phone") or "").strip()[:30],
                           (d.get("country") or "").strip()[:60], generate_password_hash(pw), now())

    # ------------------------------------------------------------------- orders
    def checkout(self, d):
        """New customer + licence (pending) + first invoice."""
        items = pricing.clean_devices(d.get("devices"))
        total = sum(x["count"] for x in items)
        hosting = d.get("hosting")
        q = self.quote(total, hosting)
        with self.db.tx():
            cid = self._new_customer(d)
            lid = self.db.run("INSERT INTO licences(key,customer_id,devices,device_list,hosting,server_slug,hosted_monthly,status,created) "
                              "VALUES(?,?,?,?,?,?,?,?,?)", new_key(), cid, total, dumps(items), hosting,
                              q["server"]["slug"], q["server"]["hosted_monthly"] if hosting == "hosted" else 0, "pending", now())
            desc = f"NE8000 Manager licence, {total} device{'s' if total > 1 else ''}"
            if hosting == "hosted":
                desc += f", hosted server ({q['server']['label']})"
            oid = self._order(cid, lid, "new", desc, q["total_usd"])
        return self.order(oid)

    def _order(self, cid, lid, kind, desc, usd, added=0):
        return self.db.run("INSERT INTO orders(ref,customer_id,licence_id,kind,description,devices_added,amount_usd,amount_kes,status,created) "
                           "VALUES(?,?,?,?,?,?,?,?,?,?)", new_ref(), cid, lid, kind, desc, added, usd, pricing.to_kes(usd), "unpaid", now())

    def order(self, oid):
        return self.db.one("SELECT * FROM orders WHERE id=?", oid)

    def order_by_ref(self, ref):
        return self.db.one("SELECT * FROM orders WHERE ref=?", ref)

    def renewal_amount(self, lic):
        lic_usd = pricing.licence_price(lic["devices"]) if pricing.PERIOD != "once" else 0
        months = 12 if pricing.PERIOD == "year" else 1
        return round(lic_usd + lic["hosted_monthly"] * months, 2)

    def renew_order(self, lic):
        """The open renewal invoice for a licence, made if there is none."""
        open_ = self.db.one("SELECT * FROM orders WHERE licence_id=? AND kind='renew' AND status='unpaid'", lic["id"])
        if open_:
            return open_
        usd = self.renewal_amount(lic)
        if usd <= 0:
            raise StoreError("This licence does not need renewing.")
        what = "hosting" if pricing.PERIOD == "once" else "licence"
        oid = self._order(lic["customer_id"], lic["id"], "renew", f"Renewal of {what}, {lic['devices']} devices ({period_label()})", usd)
        return self.order(oid)

    def add_devices_order(self, lic, added):
        try:
            added = int(added)
        except (TypeError, ValueError):
            raise StoreError("Enter how many devices to add.")
        if not 1 <= added <= pricing.MAX_DEVICES - lic["devices"]:
            raise StoreError("Enter how many devices to add.")
        if lic["status"] != "active":
            raise StoreError("Renew the licence first, then add devices.")
        usd = pricing.extra_devices_price(added)
        new_total = lic["devices"] + added
        hosted_note = ""
        if lic["hosting"] == "hosted":
            srv = pricing.server_for(new_total, self.do.prices())
            if srv["slug"] != lic["server_slug"]:
                hosted_note = f"; server upgrade to {srv['label']}"
        oid = self._order(lic["customer_id"], lic["id"], "devices",
                          f"{added} more device{'s' if added > 1 else ''} (now {new_total}){hosted_note}", usd, added)
        return self.order(oid)

    # ----------------------------------------------------------------- payments
    def pay_mpesa(self, order, phone):
        if order["status"] != "unpaid":
            raise StoreError("This invoice is already paid.")
        try:
            phone = normalise_phone(phone)
            cid = self.mpesa.stk_push(phone, order["amount_kes"], order["ref"], "NE8000 licence")
        except MpesaError as exc:
            raise StoreError(str(exc))
        t = now()
        self.db.run("INSERT INTO payments(order_id,method,status,amount,currency,phone,checkout_id,created,updated) VALUES(?,?,?,?,?,?,?,?,?)",
                    order["id"], "mpesa", "pending", order["amount_kes"], "KES", phone, cid, t, t)
        return cid

    def mpesa_result(self, checkout_id):
        """Called on Daraja's callback and when the buyer's page polls: ask Daraja and settle the payment."""
        p = self.db.one("SELECT * FROM payments WHERE checkout_id=?", checkout_id)
        if not p or p["status"] != "pending":
            return p
        try:
            state, desc = self.mpesa.query(checkout_id)
        except MpesaError:
            return p
        if state == "pending":
            return p
        self.db.run("UPDATE payments SET status=?, note=?, updated=? WHERE id=?", state, desc[:200], now(), p["id"])
        if state == "paid":
            self.mark_paid(p["order_id"], "mpesa", desc[:60])
        return self.db.one("SELECT * FROM payments WHERE id=?", p["id"])

    def record_receipt(self, checkout_id, receipt):
        if receipt:
            self.db.run("UPDATE payments SET receipt=? WHERE checkout_id=?", receipt[:40], checkout_id)

    def choose_bank(self, order):
        if order["status"] != "unpaid":
            raise StoreError("This invoice is already paid.")
        self.db.run("UPDATE orders SET method='bank' WHERE id=?", order["id"])
        self.mail.send(self.env.get("STORE_ADMIN_EMAIL", "") or self.mail.sender, f"Bank transfer expected: {order['ref']}",
                       f"Invoice {order['ref']} ({order['description']}) will be paid by bank transfer: "
                       f"USD {order['amount_usd']:.2f} / KES {order['amount_kes']:,}.\n"
                       f"Confirm it in the admin page when the money arrives: {self.public_url}/admin")

    def confirm_bank(self, oid, reference, admin):
        o = self.order(oid)
        if not o or o["status"] != "unpaid":
            raise StoreError("That invoice is not waiting for payment.")
        t = now()
        self.db.run("INSERT INTO payments(order_id,method,status,amount,currency,receipt,note,created,updated) VALUES(?,?,?,?,?,?,?,?,?)",
                    oid, "bank", "paid", o["amount_usd"], "USD", (reference or "")[:60], f"confirmed by {admin}", t, t)
        self.mark_paid(oid, "bank", reference)

    def mark_paid(self, oid, method, receipt=""):
        with self.db.tx():
            o = self.order(oid)
            if not o or o["status"] != "unpaid":
                return
            self.db.run("UPDATE orders SET status='paid', method=?, paid=? WHERE id=?", method, now(), oid)
            lic = self.licence(o["licence_id"])
            days = PERIOD_DAYS.get(pricing.PERIOD, 30)
            if o["kind"] == "new":
                self.db.run("UPDATE licences SET status='active', paid_until=? WHERE id=?", now() + days * 86400, lic["id"])
            elif o["kind"] == "renew":
                start = max(lic["paid_until"], now())
                if pricing.PERIOD == "once":
                    days = 30          # one-time licence: renewals are monthly hosting
                self.db.run("UPDATE licences SET status='active', paid_until=? WHERE id=?", start + days * 86400, lic["id"])
            elif o["kind"] == "devices":
                total = lic["devices"] + o["devices_added"]
                items = json.loads(lic["device_list"])
                items.append({"type": "added", "count": o["devices_added"]})
                hosted = lic["hosted_monthly"]
                slug = lic["server_slug"]
                if lic["hosting"] == "hosted":
                    srv = pricing.server_for(total, self.do.prices())
                    hosted, slug = srv["hosted_monthly"], srv["slug"]
                self.db.run("UPDATE licences SET devices=?, device_list=?, hosted_monthly=?, server_slug=? WHERE id=?",
                            total, dumps(items), hosted, slug, lic["id"])
        self._after_payment(oid)

    def _after_payment(self, oid):
        o = self.order(oid)
        lic = self.licence(o["licence_id"])
        c = self.customer(o["customer_id"])
        self.mail.send(c["email"], f"Payment received: {o['ref']}",
                       f"Hello {c['name']},\n\nThank you. We received your payment for invoice {o['ref']}:\n"
                       f"{o['description']}\nUSD {o['amount_usd']:.2f} (KES {o['amount_kes']:,})\n\n"
                       f"Your account: {self.public_url}/account\n", bcc_admin=True)
        if o["kind"] == "new":
            if lic["hosting"] == "hosted":
                threading.Thread(target=self.provision, args=(lic["id"],), daemon=True).start()
            else:
                self._send_install(lic, c)
        elif o["kind"] == "devices" and lic["hosting"] == "hosted":
            srv = self.server_for_licence(lic["id"])
            if srv and srv["slug"] != lic["server_slug"]:
                self.mail.send(self.env.get("STORE_ADMIN_EMAIL", "") or self.mail.sender, f"Resize server for {lic['key']}",
                               f"{c['name']} now has {lic['devices']} devices. Resize droplet {srv['droplet_id']} "
                               f"from {srv['slug']} to {lic['server_slug']} in DigitalOcean.")

    # ----------------------------------------------------------------- licences
    def licence(self, lid):
        return self.db.one("SELECT * FROM licences WHERE id=?", lid)

    def licence_by_key(self, key):
        return self.db.one("SELECT * FROM licences WHERE key=?", (key or "").strip().upper())

    def token(self, key):
        lic = self.licence_by_key(key)
        if not lic:
            raise StoreError("Unknown licence key.")
        c = self.customer(lic["customer_id"])
        return self.signer.token(lic, c.get("company") or c["name"])

    def install_command(self, lic):
        return f"curl -fsSL -H 'X-Licence-Key: {lic['key']}' {self.public_url}/api/install.sh | sudo sh"

    def _send_install(self, lic, c):
        self.mail.send(c["email"], "Your NE8000 Manager licence and install command",
                       f"Hello {c['name']},\n\nYour licence is active for {lic['devices']} device(s).\n\n"
                       f"Licence key: {lic['key']}\n\n"
                       f"Install on your Ubuntu 22.04/24.04 server (it needs to reach your routers' management IPs):\n\n"
                       f"  {self.install_command(lic)}\n\n"
                       f"For HTTPS with your own domain add DOMAIN=your.domain before sh:\n"
                       f"  ... | sudo DOMAIN=nms.example.com sh\n\n"
                       f"The installer prints the first admin password at the end. Recommended server: "
                       f"{pricing.server_for(lic['devices'])['label']}.\n\n"
                       f"Manage your licence, add devices and renew: {self.public_url}/account\n", bcc_admin=True)

    def install_script(self, key):
        lic = self.licence_by_key(key)
        if not lic or lic["status"] != "active":
            raise StoreError("Unknown or inactive licence key.")
        return "\n".join([
            "#!/bin/sh",
            "# NE8000 Manager installer. Run as root on Ubuntu or Debian.",
            "set -e",
            "DIR=${NE8000_DIR:-/opt/ne8000}",
            'mkdir -p "$DIR" && cd "$DIR"',
            f"curl -fsS -H 'X-Licence-Key: {lic['key']}' {self.public_url}/api/download -o app.tar.gz",
            "tar xzf app.tar.gz && rm app.tar.gz",
            "if [ ! -f .env ]; then",
            "  cp .env.example .env",
            f"  echo 'NE8000_LICENCE_KEY={lic['key']}' >> .env",
            f"  echo 'NE8000_LICENCE_SERVER={self.public_url}' >> .env",
            f"  echo 'NE8000_LICENCE_PUBKEY={self.signer.public_b64}' >> .env",
            "fi",
            'DOMAIN="$DOMAIN" sh ./install.sh',
            "",
        ])

    def app_tarball(self, key):
        lic = self.licence_by_key(key)
        if not lic or lic["status"] not in ("active",):
            raise StoreError("Unknown or inactive licence key.")
        skip_dirs = {"store", ".git", "data", "plans", "__pycache__", ".venv"}
        skip_files = {".env", "users.json", "secret_key", "audit.log", "billing.json", "billing_state.json", "routers.json", "licence.json"}
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for root, dirs, files in os.walk(self.app_dir):
                dirs[:] = sorted(d for d in dirs if d not in skip_dirs and not d.startswith("data.backup"))
                for f in sorted(files):
                    if f in skip_files or f.endswith((".pyc", ".db", ".db-wal", ".db-shm")):
                        continue
                    full = os.path.join(root, f)
                    tar.add(full, arcname=os.path.relpath(full, self.app_dir))
        return buf.getvalue()

    # ------------------------------------------------------------------ hosting
    def server_for_licence(self, lid):
        return self.db.one("SELECT * FROM servers WHERE licence_id=? ORDER BY id DESC LIMIT 1", lid)

    def provision(self, lid):
        lic = self.licence(lid)
        t = now()
        pw = secrets.token_urlsafe(12).replace("-", "x").replace("_", "y")
        token = secrets.token_urlsafe(24)
        sid = self.db.run("INSERT INTO servers(licence_id,slug,region,status,admin_password,callback_token,created,updated) "
                          "VALUES(?,?,?,?,?,?,?,?)", lid, lic["server_slug"], self.do.region, "creating", pw, token, t, t)
        try:
            ud = cloud_init(self.public_url, lic["key"], self.signer.public_b64, pw, f"{self.public_url}/api/provisioned/{token}")
            did = self.do.create(f"ne8000-{lic['key'][5:10].lower()}-{lid}", lic["server_slug"], ud, tags=[f"licence-{lid}"])
            self.db.run("UPDATE servers SET droplet_id=?, status='installing', updated=? WHERE id=?", did, now(), sid)
            for _ in range(30):
                time.sleep(10)
                ip = self.do.ip(did)
                if ip:
                    self.db.run("UPDATE servers SET ip=?, updated=? WHERE id=?", ip, now(), sid)
                    break
        except Exception as exc:
            self.db.run("UPDATE servers SET status='failed', log=?, updated=? WHERE id=?", f"{exc}\n{traceback.format_exc()}"[-4000:], now(), sid)
            self.mail.send(self.env.get("STORE_ADMIN_EMAIL", "") or self.mail.sender, f"Hosted install failed for {lic['key']}",
                           f"Creating the server failed: {exc}\nRetry it in the admin page: {self.public_url}/admin")

    def provisioned(self, token, ip, url):
        srv = self.db.one("SELECT * FROM servers WHERE callback_token=? AND status='installing'", token or "-")
        if not srv:
            raise StoreError("Unknown server.")
        if srv["ip"] and ip != srv["ip"]:
            raise StoreError("Unknown server.")
        expect = f"https://{srv['ip'].replace('.', '-')}.sslip.io" if srv["ip"] else ""
        url = expect or url
        self.db.run("UPDATE servers SET status='ready', url=?, ip=COALESCE(NULLIF(ip,''),?), updated=? WHERE id=?", url, ip, now(), srv["id"])
        lic = self.licence(srv["licence_id"])
        c = self.customer(lic["customer_id"])
        self.mail.send(c["email"], "Your NE8000 Manager server is ready",
                       f"Hello {c['name']},\n\nYour hosted NE8000 Manager is installed and running.\n\n"
                       f"Address:  {url}\nUsername: admin\nPassword: {srv['admin_password']}\n\n"
                       f"Please change the password after your first sign-in (top right > Change password).\n"
                       f"Licence key: {lic['key']} ({lic['devices']} devices)\n\n"
                       f"Your server's IP is {ip}. Allow it to reach your routers' management addresses "
                       f"(SSH 22 and SNMP 161), and send NetStream to it on UDP 2055 if you use Traffic per address.\n\n"
                       f"Billing and renewals: {self.public_url}/account\n", bcc_admin=True)
        # the password was only needed for this email
        self.db.run("UPDATE servers SET admin_password='' WHERE id=?", srv["id"])

    # ----------------------------------------------------------------- renewals
    def daily(self):
        """Renewal invoices 7 days before the end of the period; expire licences after the grace days."""
        t = now()
        for lic in self.db.q("SELECT * FROM licences WHERE status='active'"):
            if self.renewal_amount(lic) <= 0:
                continue
            if lic["paid_until"] - t < 7 * 86400:
                had = self.db.one("SELECT id FROM orders WHERE licence_id=? AND kind='renew' AND status='unpaid'", lic["id"])
                o = self.renew_order(lic)
                if not had:
                    c = self.customer(lic["customer_id"])
                    end = time.strftime("%d %b %Y", time.gmtime(lic["paid_until"]))
                    self.mail.send(c["email"], f"Renewal invoice {o['ref']}",
                                   f"Hello {c['name']},\n\nYour NE8000 Manager licence runs until {end}. "
                                   f"Renewal: USD {o['amount_usd']:.2f} (KES {o['amount_kes']:,}).\n"
                                   f"Pay by M-Pesa or bank transfer: {self.public_url}/pay/{o['ref']}\n")
            if lic["paid_until"] + GRACE_DAYS * 86400 < t:
                self.db.run("UPDATE licences SET status='expired' WHERE id=?", lic["id"])

    def start(self):
        def loop():
            while True:
                try:
                    self.daily()
                    for p in self.db.q("SELECT checkout_id FROM payments WHERE status='pending' AND method='mpesa' AND created>?", now() - 86400):
                        self.mpesa_result(p["checkout_id"])
                except Exception:
                    traceback.print_exc()
                time.sleep(3600)
        threading.Thread(target=loop, daemon=True, name="store-daily").start()
