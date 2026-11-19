"""Alerts: tell people the moment something goes wrong, and again when it is fixed.

Every minute the engine collects what is wrong now (the health check's findings for every saved router, routing
sessions that are down, site alarms, this server, and the user's own checks), matches each problem to the alert
rules, and sends it to the rules' channels: Telegram, email, SMS (Africa's Talking), Slack, Microsoft Teams, Discord
or any webhook. A problem is sent once, reminded while it lasts (unless acknowledged) and closed with a "resolved"
message. Problems that start together are grouped into one message per channel. Quiet hours hold back everything
but critical problems until they end.

Channel secrets (bot tokens, passwords, API keys, webhook URLs) are kept in the data folder (alerts.json, readable
by the app's user only) because the alerts must work around the clock; they are never sent back to the browser.
"""
import json
import os
import re
import smtplib
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from email.message import EmailMessage

KINDS = {
    "link_down": ("Link / port down", "A link with a description is enabled but down."),
    "flap": ("Link flapping", "A link went down and up again, more than once in a day."),
    "session_down": ("BGP / OSPF / IS-IS / LDP session down", "A routing session is not up."),
    "latency": ("High latency", "A ping target is much slower than its normal, or than its limit."),
    "loss": ("Packet loss", "A ping target is losing packets or not answering."),
    "capacity": ("Link full", "A link is congested or close to full."),
    "errors": ("Errors and drops", "A link has input errors or drops packets."),
    "optics": ("SFP light (optics)", "An SFP's light is too low, too high or falling."),
    "otdr": ("Fibre cut (OTDR)", "The OTDR reported a fibre event."),
    "router": ("Router (CPU, memory, alarms)", "The router itself: CPU, memory, temperature, power, fans."),
    "site": ("Sites: power, access, CCTV, devices", "Mains failure, on battery, door forced, camera offline, a site device down."),
    "server": ("This server", "The machine this app runs on: disk, memory, load."),
    "check": ("My checks (ping, port, web)", "The checks added on the Alerts page."),
}
HEALTH_MAP = {"down": "link_down", "flap": "flap", "latency": "latency", "distance": "latency", "loss": "loss",
              "congestion": "capacity", "capacity": "capacity", "errors": "errors", "out_drops": "errors", "in_drops": "errors",
              "crc": "errors", "optics": "optics", "otdr": "otdr"}
SEV = {"info": 0, "warning": 1, "critical": 2}
CHANNEL_TYPES = {
    "telegram": ("Telegram", [("token", "Bot token", True), ("chat_id", "Chat or group ID", False)]),
    "email": ("Email", [("host", "SMTP server", False), ("port", "Port", False), ("security", "Security", False), ("user", "User", False),
                        ("password", "Password / app password", True), ("sender", "From", False), ("to", "To (comma separated)", False)]),
    "sms_at": ("SMS (Africa's Talking)", [("username", "Username", False), ("api_key", "API key", True), ("to", "Phone numbers (comma separated)", False),
                                          ("sender_id", "Sender ID (optional)", False)]),
    "slack": ("Slack", [("url", "Incoming webhook URL", True)]),
    "teams": ("Microsoft Teams", [("url", "Incoming webhook URL", True)]),
    "discord": ("Discord", [("url", "Webhook URL", True)]),
    "webhook": ("Webhook (JSON)", [("url", "URL", True)]),
}
SECRET = {"token", "password", "api_key", "url"}
MASK = "••••"
CHECK_TYPES = {"ping": "Ping", "tcp": "TCP port", "http": "Web page"}


def default_rules():
    remind = {"link_down": 30, "session_down": 30, "site": 30, "check": 30}
    sev = {"capacity": "critical", "errors": "critical", "optics": "critical", "latency": "warning"}
    return [{"id": k, "kind": k, "enabled": True, "min": sev.get(k, "warning"), "channels": ["*"], "include": "", "exclude": "",
             "remind": remind.get(k, 0)} for k in KINDS]


def mask(v):
    v = str(v or "")
    return "" if not v else MASK + v[-4:] if len(v) > 8 else MASK


def _split(s):
    return [x.strip() for x in str(s or "").replace(";", ",").split(",") if x.strip()]


class Alerts:
    def __init__(self, data_dir, sources=None, ping=None):
        self.path = os.path.join(data_dir, "alerts.json")
        self.state_path = os.path.join(data_dir, "alerts_state.json")
        self.sources = sources or (lambda: [])
        self.ping = ping
        self.lock = threading.RLock()
        self.cfg = self._load(self.path, {})
        self.cfg.setdefault("channels", [])
        self.cfg.setdefault("checks", [])
        self.cfg.setdefault("quiet", {"on": False, "from": "22:00", "to": "06:00"})
        self.cfg.setdefault("tz", 180)
        rules = {r["kind"]: r for r in self.cfg.get("rules", [])}
        self.cfg["rules"] = [rules.get(r["kind"], r) for r in default_rules()]
        st = self._load(self.state_path, {})
        self.active = st.get("active", {})
        self.history = st.get("history", [])
        self.check_state = {}
        self.last_run = None
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.thread = None

    # ---- storage
    @staticmethod
    def _load(path, empty):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return empty

    @staticmethod
    def _write(path, data):
        tmp = path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, path)

    def _save(self):
        self._write(self.path, self.cfg)

    def _save_state(self):
        self._write(self.state_path, {"active": self.active, "history": self.history[-1000:]})

    # ---- what the page sees (secrets masked)
    def public(self):
        with self.lock:
            chans = [{**c, "config": {k: (mask(v) if k in SECRET else v) for k, v in c.get("config", {}).items()}} for c in self.cfg["channels"]]
            checks = [{**c, "state": self.check_state.get(c["id"])} for c in self.cfg["checks"]]
            active = sorted(self.active.values(), key=lambda a: (-SEV.get(a["severity"], 0), -a["first"]))
            return {"channels": chans, "rules": self.cfg["rules"], "checks": checks, "quiet": self.cfg["quiet"], "tz": self.cfg["tz"],
                    "active": active, "history": list(reversed(self.history[-300:])), "last_run": self.last_run,
                    "kinds": {k: {"label": v[0], "help": v[1]} for k, v in KINDS.items()},
                    "channel_types": {k: {"label": v[0], "fields": [{"key": f, "label": l, "secret": s} for f, l, s in v[1]]} for k, v in CHANNEL_TYPES.items()},
                    "check_types": CHECK_TYPES}

    # ---- channels
    def _clean_channel(self, d, old=None):
        t = d.get("type")
        if t not in CHANNEL_TYPES:
            raise ValueError("Pick the kind of channel.")
        cfg, oldcfg = {}, (old or {}).get("config", {})
        for key, label, secret in CHANNEL_TYPES[t][1]:
            v = str((d.get("config") or {}).get(key, "")).strip()
            if secret and (v.startswith(MASK) or not v) and old and old.get("type") == t:
                v = oldcfg.get(key, "")           # unchanged secret: keep the stored one
            cfg[key] = v
        need = {"telegram": ["token", "chat_id"], "email": ["host", "to"], "sms_at": ["username", "api_key", "to"]}.get(t, ["url"])
        miss = [dict((f, l) for f, l, _ in CHANNEL_TYPES[t][1])[k] for k in need if not cfg.get(k)]
        if miss:
            raise ValueError("Fill in: " + ", ".join(miss) + ".")
        if t == "email":
            cfg["port"] = cfg.get("port") or "587"
            if not cfg["port"].isdigit():
                raise ValueError("The port is a number, e.g. 587 or 465.")
            cfg["security"] = cfg.get("security") if cfg.get("security") in ("starttls", "ssl", "none") else "starttls"
        if "url" in cfg and not cfg["url"].startswith(("https://", "http://")):
            raise ValueError("The URL starts with https://")
        return {"id": (old or {}).get("id") or uuid.uuid4().hex[:10], "type": t,
                "name": str(d.get("name") or "").strip()[:60] or CHANNEL_TYPES[t][0], "enabled": d.get("enabled", True) is not False,
                "min": d.get("min") if d.get("min") in SEV else "info", "config": cfg}

    def save_channel(self, d, cid=None):
        with self.lock:
            old = next((c for c in self.cfg["channels"] if c["id"] == cid), None) if cid else None
            if cid and not old:
                raise ValueError("That channel is gone.")
            c = self._clean_channel(d, old)
            if old:
                self.cfg["channels"][self.cfg["channels"].index(old)] = c
            else:
                self.cfg["channels"].append(c)
            self._save()
            return c

    def delete_channel(self, cid):
        with self.lock:
            self.cfg["channels"] = [c for c in self.cfg["channels"] if c["id"] != cid]
            for r in self.cfg["rules"]:
                r["channels"] = [x for x in r["channels"] if x != cid]
            self._save()

    def test_channel(self, d, cid=None):
        with self.lock:
            old = next((c for c in self.cfg["channels"] if c["id"] == cid), None) if cid else None
        c = self._clean_channel(d, old) if d.get("type") else old
        if not c:
            raise ValueError("That channel is gone.")
        text = f"✅ Test from NE8000 Manager\nThis channel ({c['name']}) works. Alerts will arrive here.\n{self._fmt_time(time.time())}"
        ok, err = self._send(c, text, "Test from NE8000 Manager", {"type": "test"})
        self._hist({"type": "test", "title": "Test message", "channels": [{"id": c["id"], "name": c["name"], "ok": ok, "error": err}]})
        return ok, err

    # ---- rules, settings, checks
    def save_rules(self, rows):
        with self.lock:
            ids = {c["id"] for c in self.cfg["channels"]}
            by = {r.get("kind"): r for r in rows if isinstance(r, dict)}
            out = []
            for r in self.cfg["rules"]:
                n = by.get(r["kind"])
                if n:
                    r = {**r, "enabled": bool(n.get("enabled")), "min": n.get("min") if n.get("min") in SEV else r["min"],
                         "channels": [x for x in n.get("channels", []) if x == "*" or x in ids],
                         "include": str(n.get("include") or "")[:200], "exclude": str(n.get("exclude") or "")[:200],
                         "remind": max(0, min(1440, int(n.get("remind") or 0)))}
                out.append(r)
            self.cfg["rules"] = out
            self._save()
            return out

    def save_settings(self, d):
        with self.lock:
            q = d.get("quiet") or {}
            for k in ("from", "to"):
                v = str(q.get(k) or self.cfg["quiet"][k])
                if len(v) != 5 or v[2] != ":" or not (v[:2] + v[3:]).isdigit() or int(v[:2]) > 23 or int(v[3:]) > 59:
                    raise ValueError("Quiet hours are times like 22:00.")
                self.cfg["quiet"][k] = v
            self.cfg["quiet"]["on"] = bool(q.get("on"))
            if "tz" in d:
                self.cfg["tz"] = max(-720, min(840, int(d["tz"])))
            self._save()

    def save_check(self, d, cid=None):
        t = d.get("type")
        if t not in CHECK_TYPES:
            raise ValueError("Pick ping, TCP port or web page.")
        target = str(d.get("target") or "").strip()
        if not target:
            raise ValueError("Fill in the address" + (" (https://…)" if t == "http" else "") + ".")
        if t == "http" and not target.startswith(("http://", "https://")):
            raise ValueError("A web page check needs a URL starting with https://")
        if t != "http" and any(ch in target for ch in " /:"):
            raise ValueError("Give only the address or name, e.g. 102.216.117.2 or radius.example.com.")
        port = int(d.get("port") or 0)
        if t == "tcp" and not 0 < port < 65536:
            raise ValueError("Give the TCP port, e.g. 22, 443, 8728.")
        row = {"id": cid or uuid.uuid4().hex[:10], "type": t, "name": str(d.get("name") or target).strip()[:60], "target": target, "port": port,
               "ms": float(d["ms"]) if d.get("ms") not in (None, "") else None, "loss": float(d["loss"]) if d.get("loss") not in (None, "") else None,
               "severity": d.get("severity") if d.get("severity") in ("warning", "critical") else "critical", "enabled": d.get("enabled", True) is not False}
        with self.lock:
            rows = self.cfg["checks"]
            i = next((n for n, c in enumerate(rows) if c["id"] == cid), None)
            if cid and i is None:
                raise ValueError("That check is gone.")
            if i is None:
                rows.append(row)
            else:
                rows[i] = row
                self.check_state.pop(cid, None)
            self._save()
        self.wake.set()
        return row

    def delete_check(self, cid):
        with self.lock:
            self.cfg["checks"] = [c for c in self.cfg["checks"] if c["id"] != cid]
            self.check_state.pop(cid, None)
            self._save()

    def ack(self, key, who):
        with self.lock:
            a = self.active.get(key)
            if not a:
                raise ValueError("That alert has already cleared.")
            a["acked_by"], a["acked_at"] = who, time.time()
            self._hist({"type": "ack", "key": key, "kind": a["kind"], "severity": a["severity"], "title": a["title"], "who": who})
            self._save_state()

    # ---- the user's own checks
    def _run_check(self, c):
        st = self.check_state.get(c["id"]) or {"fails": 0}
        res = {"time": time.time()}
        try:
            if c["type"] == "ping":
                r = self.ping(c["target"]) if self.ping else {"error": "ping not available"}
                res.update({k: r.get(k) for k in ("avg", "loss", "error")})
                if r.get("error"):
                    res["problem"] = r["error"]
                elif r.get("avg") is None:
                    res["problem"] = "no reply"
                elif c.get("loss") is not None and (r.get("loss") or 0) >= c["loss"]:
                    res["problem"] = f"{r['loss']}% loss (limit {c['loss']:g}%)"
                elif c.get("ms") is not None and r["avg"] > c["ms"]:
                    res["problem"] = f"{r['avg']:.1f} ms (limit {c['ms']:g} ms)"
            elif c["type"] == "tcp":
                t0 = time.time()
                with socket.create_connection((c["target"], c["port"]), timeout=5):
                    res["avg"] = (time.time() - t0) * 1000
                if c.get("ms") is not None and res["avg"] > c["ms"]:
                    res["problem"] = f"answered in {res['avg']:.0f} ms (limit {c['ms']:g} ms)"
            else:
                t0 = time.time()
                req = urllib.request.Request(c["target"], headers={"User-Agent": "NE8000-Manager-check"})
                try:
                    with urllib.request.urlopen(req, timeout=10) as r:
                        res["status"] = r.status
                except urllib.error.HTTPError as e:
                    res["status"] = e.code
                res["avg"] = (time.time() - t0) * 1000
                if res["status"] >= 400:
                    res["problem"] = f"the page answers {res['status']}"
                elif c.get("ms") is not None and res["avg"] > c["ms"]:
                    res["problem"] = f"took {res['avg']:.0f} ms (limit {c['ms']:g} ms)"
        except Exception as exc:          # refused, timed out, no such name ...
            res["problem"] = re.sub(r"\[Errno -?\d+\]\s*", "", str(getattr(exc, "reason", None) or exc)) or exc.__class__.__name__
        res["fails"] = st["fails"] + 1 if res.get("problem") else 0
        res["since"] = st.get("since") if res.get("problem") and st.get("problem") else (res["time"] if res.get("problem") else None)
        self.check_state[c["id"]] = res
        return res

    def _checks(self):
        out = []
        for c in [c for c in self.cfg["checks"] if c.get("enabled", True)]:
            r = self._run_check(c)
            if r.get("problem") and r["fails"] >= 2:          # two checks in a row: a blip is not an alert
                where = c["target"] + (f":{c['port']}" if c["type"] == "tcp" else "")
                out.append({"key": "check:" + c["id"], "kind": "check", "severity": c.get("severity", "critical"),
                            "title": f"{c['name']}: {r['problem']}", "detail": f"{CHECK_TYPES[c['type']]} check of {where}",
                            "target": where, "where": "", "since": r.get("since")})
        return out

    # ---- the engine
    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._loop, daemon=True, name="alerts")
        self.thread.start()

    def run_now(self):
        self.wake.set()

    def _loop(self):
        self.stop.wait(30)
        while not self.stop.is_set():
            try:
                self.cycle()
            except Exception as exc:       # keep alerting whatever one source did
                print(f"alerts: {exc}", flush=True)
            self.wake.wait(60)
            self.wake.clear()

    def _quiet(self, now):
        q = self.cfg["quiet"]
        if not q.get("on"):
            return False
        m = int((now / 60 + self.cfg["tz"]) % 1440)
        a, b = (int(q[k][:2]) * 60 + int(q[k][3:]) for k in ("from", "to"))
        return a <= m < b if a < b else m >= a or m < b

    @staticmethod
    def _match(rule, p):
        text = " ".join(str(p.get(k) or "") for k in ("title", "detail", "target", "where", "description")).lower()
        inc, exc = [x.lower() for x in _split(rule.get("include"))], [x.lower() for x in _split(rule.get("exclude"))]
        return (not inc or any(x in text for x in inc)) and not any(x in text for x in exc)

    def cycle(self, now=None):
        now = now or time.time()
        probs = []
        try:
            probs += self.sources() or []
        except Exception as exc:
            print(f"alerts sources: {exc}", flush=True)
        probs += self._checks()
        with self.lock:
            self.last_run = now
            chans = {c["id"]: c for c in self.cfg["channels"] if c.get("enabled", True)}
            quiet = self._quiet(now)
            out = {}                 # channel id -> [(type, alert)]
            seen = set()
            for p in probs:
                rules = [r for r in self.cfg["rules"] if r["enabled"] and r["kind"] == p["kind"]
                         and SEV.get(p["severity"], 0) >= SEV[r["min"]] and self._match(r, p)]
                if not rules:
                    continue
                key = p["key"]
                seen.add(key)
                to = sorted({cid for r in rules for cid in (list(chans) if "*" in r["channels"] else r["channels"]) if cid in chans})
                remind = min([r["remind"] for r in rules if r["remind"]] or [0])
                a = self.active.get(key)
                if not a:
                    a = self.active[key] = {**p, "first": p.get("since") or now, "seen": now, "sent_at": None, "sent_sev": None,
                                            "remind_at": None, "channels": [], "acked_by": None}
                    self._hist({"type": "opened", "key": key, "kind": p["kind"], "severity": p["severity"], "title": p["title"], "detail": p.get("detail", ""), "where": p.get("where", "")})
                else:
                    a.update({k: p.get(k, a.get(k)) for k in ("severity", "title", "detail", "target", "where", "description")})
                    a["seen"] = now
                held = quiet and p["severity"] != "critical"
                if held or not to:
                    continue
                if a["sent_at"] is None or SEV.get(p["severity"], 0) > SEV.get(a["sent_sev"], 0):
                    kind = "problem" if a["sent_at"] is None else "worse"
                    a.update(sent_at=now, sent_sev=p["severity"], remind_at=now, channels=sorted(set(a["channels"]) | set(to)))
                    for cid in to:
                        out.setdefault(cid, []).append((kind, dict(a)))
                elif remind and not a.get("acked_by") and now - (a["remind_at"] or now) >= remind * 60:
                    a["remind_at"] = now
                    for cid in to:
                        out.setdefault(cid, []).append(("reminder", dict(a)))
            for key in [k for k in self.active if k not in seen]:
                a = self.active.pop(key)
                self._hist({"type": "resolved", "key": key, "kind": a["kind"], "severity": a["severity"], "title": a["title"],
                            "where": a.get("where", ""), "after": now - a["first"]})
                if a["sent_at"]:
                    for cid in a["channels"]:
                        if cid in chans:
                            out.setdefault(cid, []).append(("resolved", {**a, "ended": now}))
            self._save_state()
        for cid, items in out.items():
            self._deliver(chans[cid], items)

    # ---- messages
    def _fmt_time(self, t):
        return time.strftime("%d %b %H:%M", time.gmtime(t + self.cfg["tz"] * 60))

    @staticmethod
    def _dur(s):
        s = int(max(0, s))
        return f"{s // 86400} d {s % 86400 // 3600} h" if s >= 86400 else f"{s // 3600} h {s % 3600 // 60} min" if s >= 3600 else f"{max(1, s // 60)} min"

    def _line(self, kind, a):
        icon = {"problem": "🔴" if a["severity"] == "critical" else "🟠", "worse": "🔴", "reminder": "⏰", "resolved": "✅"}[kind]
        word = {"problem": "PROBLEM", "worse": "NOW CRITICAL", "reminder": "STILL", "resolved": "RESOLVED"}[kind]
        where = f" @ {a['where']}" if a.get("where") else ""
        head = f"{icon} {word} [{a['severity']}] {a['title']}{where}"
        if kind == "resolved":
            return head + f"\n   cleared after {self._dur(a['ended'] - a['first'])} (since {self._fmt_time(a['first'])})"
        tail = [a.get("detail") or ""]
        if a.get("description") and a["description"] not in (a.get("detail") or "") and a["description"] not in a["title"]:
            tail.append(a["description"])
        tail.append(f"since {self._fmt_time(a['first'])}" + (f", {self._dur(time.time() - a['first'])}" if kind == "reminder" else ""))
        return head + "\n   " + " · ".join(x for x in tail if x)

    def _deliver(self, ch, items):
        lines = [self._line(k, a) for k, a in items[:15]]
        if len(items) > 15:
            lines.append(f"…and {len(items) - 15} more (Alerts page)")
        crit = sum(1 for k, a in items if k != "resolved" and a["severity"] == "critical")
        res = sum(1 for k, _ in items if k == "resolved")
        subject = (f"{len(items)} alerts" if len(items) > 1 else items[0][1]["title"]) + (f" ({crit} critical)" if crit else "") \
            + (" resolved" if res == len(items) else "")
        text = "NE8000 Manager\n" + "\n".join(lines)
        if SEV.get(ch.get("min", "info"), 0) > max(SEV.get(a["severity"], 0) for _, a in items):
            return
        ok, err = self._send(ch, text, subject, {"alerts": [{"type": k, **{x: a.get(x) for x in ("key", "kind", "severity", "title", "detail", "where", "target", "first")}} for k, a in items]})
        with self.lock:
            for k, a in items:
                self._hist({"type": k, "key": a["key"], "kind": a["kind"], "severity": a["severity"], "title": a["title"], "where": a.get("where", ""),
                            "channels": [{"id": ch["id"], "name": ch["name"], "ok": ok, "error": err}]})
            self._save_state()

    def _hist(self, e):
        self.history.append({"ts": time.time(), **e})
        del self.history[:-1000]

    # ---- senders
    def _send(self, ch, text, subject, payload):
        c, t = ch["config"], ch["type"]
        try:
            if t == "telegram":
                self._post(f"https://api.telegram.org/bot{c['token']}/sendMessage",
                           {"chat_id": c["chat_id"], "text": text, "disable_web_page_preview": True})
            elif t == "email":
                m = EmailMessage()
                m["Subject"] = "[NE8000] " + subject
                m["From"] = c.get("sender") or c.get("user") or "ne8000@localhost"
                m["To"] = ", ".join(_split(c["to"]))
                m.set_content(text)
                port = int(c.get("port") or 587)
                if c.get("security") == "ssl":
                    s = smtplib.SMTP_SSL(c["host"], port, timeout=15, context=ssl.create_default_context())
                else:
                    s = smtplib.SMTP(c["host"], port, timeout=15)
                    if c.get("security", "starttls") == "starttls":
                        s.starttls(context=ssl.create_default_context())
                with s:
                    if c.get("user"):
                        s.login(c["user"], c.get("password", ""))
                    s.send_message(m)
            elif t == "sms_at":
                host = "api.sandbox.africastalking.com" if c["username"] == "sandbox" else "api.africastalking.com"
                form = {"username": c["username"], "to": ",".join(_split(c["to"])), "message": text[:640]}
                if c.get("sender_id"):
                    form["from"] = c["sender_id"]
                req = urllib.request.Request(f"https://{host}/version1/messaging", data=urllib.parse.urlencode(form).encode(),
                                             headers={"apiKey": c["api_key"], "Accept": "application/json",
                                                      "Content-Type": "application/x-www-form-urlencoded"})
                with urllib.request.urlopen(req, timeout=15) as r:
                    body = json.loads(r.read() or b"{}")
                recips = (body.get("SMSMessageData") or {}).get("Recipients") or []
                bad = [x for x in recips if str(x.get("status", "")).lower() not in ("success", "sent", "submitted")]
                if not recips or bad:
                    raise RuntimeError((body.get("SMSMessageData") or {}).get("Message") or (bad[0].get("status") if bad else "no recipients"))
            elif t == "slack":
                self._post(c["url"], {"text": text})
            elif t == "teams":
                self._post(c["url"], {"text": text.replace("\n", "  \n")})
            elif t == "discord":
                self._post(c["url"], {"content": text[:1900]})
            else:
                self._post(c["url"], {"source": "ne8000-manager", "subject": subject, "text": text, **payload})
            return True, ""
        except Exception as exc:
            msg = str(exc)
            if isinstance(exc, urllib.error.HTTPError):
                try:
                    msg = f"HTTP {exc.code}: {exc.read()[:200].decode('utf-8', 'replace')}"
                except Exception:
                    msg = f"HTTP {exc.code}"
            for k in SECRET:          # never echo a secret back in an error
                if c.get(k):
                    msg = msg.replace(c[k], MASK)
            return False, msg[:300]

    @staticmethod
    def _post(url, body):
        req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json", "User-Agent": "NE8000-Manager"})
        with urllib.request.urlopen(req, timeout=15) as r:
            r.read()
