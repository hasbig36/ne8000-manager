"""End-to-end tests of the store with M-Pesa and DigitalOcean replaced by fakes.

Run from the repository root:  python -m pytest store/tests -q
"""
import importlib
import json
import os
import re
import sys
import tarfile
import io
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
STORE_DIR = os.path.dirname(HERE)
ROOT = os.path.dirname(STORE_DIR)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("STORE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("STORE_PUBLIC_URL", "https://billing.test")
    monkeypatch.setenv("STORE_ADMIN_PASSWORD", "admin-pass-123")
    monkeypatch.setenv("MPESA_CONSUMER_KEY", "k")
    monkeypatch.setenv("MPESA_CONSUMER_SECRET", "s")
    monkeypatch.setenv("MPESA_SHORTCODE", "174379")
    monkeypatch.setenv("MPESA_PASSKEY", "p")
    monkeypatch.setenv("MPESA_CALLBACK_SECRET", "cbsecret")
    monkeypatch.setenv("DO_TOKEN", "t")
    monkeypatch.setenv("BANK_ACCOUNT_NUMBER", "0123456789")
    monkeypatch.syspath_prepend(STORE_DIR)
    for m in ("app", "service", "pricing", "mpesa", "digitalocean", "db", "signing", "mailer"):
        sys.modules.pop(m, None)
    app_mod = importlib.import_module("app")
    store = app_mod.STORE

    fake = {"stk": [], "paid": set(), "droplets": []}

    def stk_push(phone, amount, ref, desc):
        cid = f"ws_CO_{len(fake['stk'])}"
        fake["stk"].append((cid, phone, amount, ref))
        return cid
    store.mpesa.stk_push = stk_push
    store.mpesa.query = lambda cid: ("paid", "The service request is processed successfully.") if cid in fake["paid"] else ("pending", "")
    store.do.prices = lambda: {}

    def create(name, slug, ud, tags=()):
        fake["droplets"].append((name, slug, ud))
        return "1001"
    store.do.create = create
    store.do.ip = lambda did: "203.0.113.7"
    import service
    monkeypatch.setattr(service.time, "sleep", lambda s: None)
    c = app_mod.app.test_client()
    c.fake, c.store, c.mod = fake, store, app_mod
    return c


def csrf(c, path):
    html = c.get(path).get_data(as_text=True)
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def buy(c, hosting="self", m8=3):
    tok = csrf(c, "/buy")
    r = c.post("/buy", data={"csrf": tok, "count_ne8000-m8": str(m8), "hosting": hosting, "name": "Jane Operator",
                             "company": "FastNet Ltd", "email": "jane@fastnet.test", "phone": "0712345678",
                             "password": "a-long-password", "terms": "1"})
    assert r.status_code == 302, r.get_data(as_text=True)
    return r.headers["Location"].rsplit("/", 1)[-1]


def test_pricing():
    sys.path.insert(0, STORE_DIR)
    import pricing
    assert pricing.licence_price(1) == 100
    assert pricing.licence_price(4) == 190
    q = pricing.quote(3, "hosted")
    assert q["server"]["slug"] == "s-2vcpu-4gb" and q["server"]["hosted_monthly"] == 31.2
    assert q["total_usd"] == 160 + 31.2
    with pytest.raises(pricing.PriceError):
        pricing.clean_devices([{"type": "ne8000-m8", "count": 0}])


def test_home_and_quote(client):
    assert b"Buy a licence" in client.get("/").data
    r = client.post("/api/quote", json={"devices": [{"type": "ne8000-m8", "count": 2}, {"type": "ne40e", "count": 1}], "hosting": "self"})
    assert r.json["licence_usd"] == 160 and r.json["hosting_usd"] == 0


def test_self_hosted_mpesa_flow_and_licence(client):
    ref = buy(client, "self", 3)
    page = client.get(f"/pay/{ref}").get_data(as_text=True)
    assert "USD 160.00" in page and "Send M-Pesa prompt" in page
    tok = csrf(client, f"/pay/{ref}")
    r = client.post(f"/pay/{ref}/mpesa", data={"csrf": tok, "phone": "0712 345 678"})
    assert r.status_code == 200
    cid, phone, amount, aref = client.fake["stk"][-1]
    assert phone == "254712345678" and aref == ref and amount == client.store.order_by_ref(ref)["amount_kes"]
    # a callback alone does not mark it paid while Daraja says it is pending
    body = {"Body": {"stkCallback": {"CheckoutRequestID": cid, "ResultCode": 0, "CallbackMetadata": {"Item": [{"Name": "MpesaReceiptNumber", "Value": "QK12ABC"}]}}}}
    client.post("/api/mpesa/callback/cbsecret", json=body)
    assert client.store.order_by_ref(ref)["status"] == "unpaid"
    assert client.post("/api/mpesa/callback/wrong", json=body).status_code == 404
    client.fake["paid"].add(cid)
    client.post("/api/mpesa/callback/cbsecret", json=body)
    o = client.store.order_by_ref(ref)
    assert o["status"] == "paid" and o["method"] == "mpesa"
    lic = client.store.licence(o["licence_id"])
    assert lic["status"] == "active" and lic["devices"] == 3 and lic["paid_until"] > time.time() + 29 * 86400
    acct = client.get("/account").get_data(as_text=True)
    assert lic["key"] in acct and "install.sh" in acct
    # emails: payment + install command
    out = os.listdir(os.path.join(client.store.data_dir, "outbox"))
    assert len(out) >= 2
    # installer and download need the key
    assert client.get("/api/install.sh").status_code == 400
    sh = client.get("/api/install.sh", headers={"X-Licence-Key": lic["key"]}).get_data(as_text=True)
    assert f"NE8000_LICENCE_KEY={lic['key']}" in sh and client.store.signer.public_b64 in sh
    tgz = client.get("/api/download", headers={"X-Licence-Key": lic["key"]}).data
    names = tarfile.open(fileobj=io.BytesIO(tgz)).getnames()
    assert "app.py" in names and "licence.py" in names and "install.sh" in names
    assert not any(n.startswith("store") or n.startswith(".git/") for n in names)
    # the signed licence verifies with the app's own checker
    token = client.get(f"/api/licence?key={lic['key']}").json["token"]
    sys.path.insert(0, ROOT)
    import licence as app_licence
    p = app_licence.verify(token, client.store.signer.public_b64)
    assert p["devices"] == 3 and p["key"] == lic["key"] and p["expires"] > time.time()


def test_app_enforces_device_count(client, tmp_path):
    ref = buy(client, "self", 2)
    o = client.store.order_by_ref(ref)
    client.store.confirm_bank(o["id"], "BANKREF1", "admin")
    lic = client.store.licence(o["licence_id"])
    token = client.store.token(lic["key"])
    sys.path.insert(0, ROOT)
    import licence as app_licence
    env = {"NE8000_LICENCE_KEY": lic["key"], "NE8000_LICENCE_SERVER": "https://billing.test",
           "NE8000_LICENCE_PUBKEY": client.store.signer.public_b64}
    L = app_licence.Licence(str(tmp_path), env)
    with pytest.raises(app_licence.LicenceError):
        L.check_add(0)                   # no licence fetched yet
    L._accept(token)
    L.check_add(0)
    L.check_add(1)
    with pytest.raises(app_licence.LicenceError, match="covers 2 devices"):
        L.check_add(2)
    # a token signed by someone else is refused
    other = app_licence.Licence(str(tmp_path), dict(env, NE8000_LICENCE_PUBKEY="A" * 43))
    with pytest.raises(app_licence.LicenceError):
        other._accept(token)
    # licence off: no limit
    app_licence.Licence(str(tmp_path), {}).check_add(500)


def test_hosted_bank_flow_provisions_and_emails_login(client):
    ref = buy(client, "hosted", 3)
    o = client.store.order_by_ref(ref)
    assert o["amount_usd"] == 160 + 31.2
    tok = csrf(client, f"/pay/{ref}")
    client.post(f"/pay/{ref}/bank", data={"csrf": tok})
    # admin confirms the transfer
    tok = csrf(client, "/admin/login")
    assert client.post("/admin/login", data={"csrf": tok, "password": "admin-pass-123"}).status_code == 302
    tok = csrf(client, "/admin")
    client.post(f"/admin/orders/{o['id']}/confirm", data={"csrf": tok, "reference": "EQ123"})
    for _ in range(50):
        srv = client.store.server_for_licence(o["licence_id"])
        if srv and srv["ip"]:
            break
        time.sleep(0.05)
    assert srv["droplet_id"] == "1001" and srv["status"] == "installing" and srv["ip"] == "203.0.113.7"
    name, slug, ud = client.fake["droplets"][-1]
    assert slug == "s-2vcpu-4gb" and "NE8000_LICENCE_KEY=NE8K-" in ud
    # the droplet reports back; the customer gets the login once and the password is wiped
    r = client.post(f"/api/provisioned/{srv['callback_token']}", json={"ip": "203.0.113.7", "url": "https://x"})
    assert r.status_code == 200
    srv = client.store.server_for_licence(o["licence_id"])
    assert srv["status"] == "ready" and srv["url"] == "https://203-0-113-7.sslip.io" and srv["admin_password"] == ""
    mails = [open(os.path.join(client.store.data_dir, "outbox", f), "rb").read().decode() for f in os.listdir(os.path.join(client.store.data_dir, "outbox"))]
    assert any("server is ready" in m and "203-0-113-7.sslip.io" in m for m in mails)
    assert client.post(f"/api/provisioned/{srv['callback_token']}", json={}).status_code == 400


def test_add_devices_and_renew(client):
    ref = buy(client, "self", 1)
    o = client.store.order_by_ref(ref)
    client.store.confirm_bank(o["id"], "R1", "admin")
    lid = o["licence_id"]
    tok = csrf(client, "/account")
    r = client.post(f"/account/licence/{lid}/devices", data={"csrf": tok, "added": "2"})
    ref2 = r.headers["Location"].rsplit("/", 1)[-1]
    o2 = client.store.order_by_ref(ref2)
    assert o2["amount_usd"] == 60
    client.store.confirm_bank(o2["id"], "R2", "admin")
    lic = client.store.licence(lid)
    assert lic["devices"] == 3
    before = lic["paid_until"]
    r = client.post(f"/account/licence/{lid}/renew", data={"csrf": tok})
    o3 = client.store.order_by_ref(r.headers["Location"].rsplit("/", 1)[-1])
    assert o3["amount_usd"] == 160
    client.store.confirm_bank(o3["id"], "R3", "admin")
    assert client.store.licence(lid)["paid_until"] > before + 29 * 86400


def test_daily_renewal_and_expiry(client):
    ref = buy(client, "self", 1)
    o = client.store.order_by_ref(ref)
    client.store.confirm_bank(o["id"], "R1", "admin")
    lid = o["licence_id"]
    client.store.db.run("UPDATE licences SET paid_until=? WHERE id=?", time.time() + 3 * 86400, lid)
    client.store.daily()
    assert client.store.db.one("SELECT * FROM orders WHERE licence_id=? AND kind='renew'", lid)["status"] == "unpaid"
    client.store.db.run("UPDATE licences SET paid_until=? WHERE id=?", time.time() - 8 * 86400, lid)
    client.store.daily()
    assert client.store.licence(lid)["status"] == "expired"


def test_other_customers_cannot_see_orders(client):
    ref = buy(client, "self", 1)
    client.post("/logout", data={"csrf": csrf(client, "/account")})
    assert client.get(f"/pay/{ref}").status_code == 302      # sign in first
    assert client.get("/admin").status_code == 302
    assert client.post("/buy", data={"hosting": "self"}).status_code == 400   # no CSRF token
