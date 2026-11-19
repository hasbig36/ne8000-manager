"""M-Pesa payments through Safaricom Daraja (Lipa na M-Pesa Online / STK push).

The buyer gets a prompt on their phone and enters their M-Pesa PIN. Daraja then calls
STORE_PUBLIC_URL/api/mpesa/callback/<secret>; before an order is marked paid the store asks
Daraja again (STK query) so a forged callback cannot mark an order paid.

Settings (environment): MPESA_ENV (sandbox | production), MPESA_CONSUMER_KEY, MPESA_CONSUMER_SECRET,
MPESA_SHORTCODE (paybill or the till's store number), MPESA_PASSKEY, MPESA_TILL (only for a till:
the till number, which makes it a Buy Goods payment), MPESA_CALLBACK_SECRET.
"""
import base64
import datetime
import json
import os
import re
import time
import urllib.request

HOSTS = {"sandbox": "https://sandbox.safaricom.co.ke", "production": "https://api.safaricom.co.ke"}


class MpesaError(Exception):
    pass


def normalise_phone(phone):
    """07xx / 01xx / +2547xx / 2547xx -> 2547xxxxxxxx."""
    p = re.sub(r"[\s\-()+]", "", str(phone or ""))
    if re.fullmatch(r"0[17]\d{8}", p):
        p = "254" + p[1:]
    elif re.fullmatch(r"[17]\d{8}", p):
        p = "254" + p
    if not re.fullmatch(r"254[17]\d{8}", p):
        raise MpesaError("Enter a Safaricom number like 0712 345 678.")
    return p


class Mpesa:
    def __init__(self, env=os.environ):
        self.env = env.get("MPESA_ENV", "sandbox")
        self.base = HOSTS.get(self.env, HOSTS["sandbox"])
        self.key = env.get("MPESA_CONSUMER_KEY", "")
        self.secret = env.get("MPESA_CONSUMER_SECRET", "")
        self.shortcode = env.get("MPESA_SHORTCODE", "")
        self.passkey = env.get("MPESA_PASSKEY", "")
        self.till = env.get("MPESA_TILL", "")
        self.callback_secret = env.get("MPESA_CALLBACK_SECRET", "")
        self.public_url = env.get("STORE_PUBLIC_URL", "").rstrip("/")
        self._token, self._token_exp = "", 0

    @property
    def configured(self):
        return all([self.key, self.secret, self.shortcode, self.passkey, self.callback_secret, self.public_url])

    def _http(self, method, path, body=None, auth=None):
        req = urllib.request.Request(self.base + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None)
        req.add_header("Authorization", auth or f"Bearer {self._access_token()}")
        if body is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            try:
                detail = json.load(exc).get("errorMessage", "")
            except Exception:
                detail = ""
            raise MpesaError(f"M-Pesa error {exc.code}: {detail or exc.reason}")
        except OSError as exc:
            raise MpesaError(f"Could not reach M-Pesa: {exc}")

    def _access_token(self):
        if self._token and time.time() < self._token_exp:
            return self._token
        basic = base64.b64encode(f"{self.key}:{self.secret}".encode()).decode()
        r = self._http("GET", "/oauth/v1/generate?grant_type=client_credentials", auth=f"Basic {basic}")
        self._token = r["access_token"]
        self._token_exp = time.time() + int(r.get("expires_in", 3599)) - 60
        return self._token

    def _password(self):
        ts = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=3))).strftime("%Y%m%d%H%M%S")
        return base64.b64encode(f"{self.shortcode}{self.passkey}{ts}".encode()).decode(), ts

    def stk_push(self, phone, amount_kes, reference, description):
        """Send the payment prompt. Returns the CheckoutRequestID."""
        if not self.configured:
            raise MpesaError("M-Pesa is not set up on this store yet. Please pay by bank transfer.")
        pw, ts = self._password()
        body = {
            "BusinessShortCode": self.shortcode, "Password": pw, "Timestamp": ts,
            "TransactionType": "CustomerBuyGoodsOnline" if self.till else "CustomerPayBillOnline",
            "Amount": int(amount_kes), "PartyA": phone, "PartyB": self.till or self.shortcode, "PhoneNumber": phone,
            "CallBackURL": f"{self.public_url}/api/mpesa/callback/{self.callback_secret}",
            "AccountReference": reference[:12], "TransactionDesc": description[:13],
        }
        r = self._http("POST", "/mpesa/stkpush/v1/processrequest", body)
        if str(r.get("ResponseCode")) != "0":
            raise MpesaError(r.get("CustomerMessage") or r.get("ResponseDescription") or "M-Pesa refused the request.")
        return r["CheckoutRequestID"]

    def query(self, checkout_id):
        """'paid', 'failed' or 'pending' for a prompt, straight from Daraja."""
        pw, ts = self._password()
        try:
            r = self._http("POST", "/mpesa/stkpushquery/v1/query",
                           {"BusinessShortCode": self.shortcode, "Password": pw, "Timestamp": ts,
                            "CheckoutRequestID": checkout_id})
        except MpesaError as exc:
            if "being processed" in str(exc).lower():
                return "pending", ""
            raise
        code = str(r.get("ResultCode", ""))
        if code == "0":
            return "paid", r.get("ResultDesc", "")
        if code == "":
            return "pending", ""
        return "failed", r.get("ResultDesc", "")


def parse_callback(data):
    """(checkout_id, result_code, receipt, amount) from Daraja's callback body."""
    cb = ((data or {}).get("Body") or {}).get("stkCallback") or {}
    items = {i.get("Name"): i.get("Value") for i in ((cb.get("CallbackMetadata") or {}).get("Item") or [])}
    return cb.get("CheckoutRequestID", ""), int(cb.get("ResultCode", -1)), str(items.get("MpesaReceiptNumber", "")), items.get("Amount")
