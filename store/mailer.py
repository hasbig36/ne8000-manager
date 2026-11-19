"""Email to buyers (login details, licence keys, invoices).

Settings: SMTP_HOST, SMTP_PORT (587), SMTP_USER, SMTP_PASSWORD, SMTP_FROM, SMTP_TLS (starttls | ssl | none).
Without SMTP_HOST the emails are written to <data dir>/outbox/ instead, so nothing is lost.
"""
import os
import secrets
import smtplib
import ssl
import time
from email.message import EmailMessage


class Mailer:
    def __init__(self, data_dir, env=os.environ):
        self.host = env.get("SMTP_HOST", "")
        self.port = int(env.get("SMTP_PORT", "587"))
        self.user = env.get("SMTP_USER", "")
        self.password = env.get("SMTP_PASSWORD", "")
        self.sender = env.get("SMTP_FROM", self.user or "no-reply@localhost")
        self.tls = env.get("SMTP_TLS", "starttls")
        self.bcc = env.get("STORE_ADMIN_EMAIL", "")
        self.outbox = os.path.join(data_dir, "outbox")

    def send(self, to, subject, body, bcc_admin=False):
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = self.sender, to, subject
        if bcc_admin and self.bcc:
            msg["Bcc"] = self.bcc
        msg.set_content(body)
        if not self.host:
            os.makedirs(self.outbox, mode=0o700, exist_ok=True)
            path = os.path.join(self.outbox, f"{int(time.time() * 1000)}-{secrets.token_hex(4)}.eml")
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(bytes(msg))
            return
        ctx = ssl.create_default_context()
        if self.tls == "ssl":
            s = smtplib.SMTP_SSL(self.host, self.port, context=ctx, timeout=30)
        else:
            s = smtplib.SMTP(self.host, self.port, timeout=30)
            if self.tls == "starttls":
                s.starttls(context=ctx)
        with s:
            if self.user:
                s.login(self.user, self.password)
            s.send_message(msg)
