"""Huawei Device Setup: find, check and set up a new Huawei device from a laptop.

Run it and it opens a page in the browser (http://127.0.0.1:<port>). Everything runs on the
laptop; nothing is sent anywhere else and no password is written to disk. Close the browser
tab or press Quit to stop it.
"""
import json
import re
import os
import secrets
import sys
import threading
import time
import traceback
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(1, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))     # adopt.py / actions.py

import adopt          # noqa: E402
import configure      # noqa: E402
import discover       # noqa: E402
import session        # noqa: E402

VERSION = "1.0.0"
TOKEN = secrets.token_urlsafe(18)
JOBS = {}
SECRET_RE = re.compile(r"(password irreversible-cipher|cipher)\s+\S+")
LAST_SEEN = [time.time()]
IDLE_EXIT = 15 * 60       # stop when no page has been open for this long

FACTORY_LOGINS = [
    {"models": "S-series switches (V200R010 and later)", "username": "admin", "password": "admin@huawei.com"},
    {"models": "NE / CE routers (VRP8)", "username": "root", "password": "Changeme_123"},
    {"models": "AR routers", "username": "admin", "password": "Admin@huawei"},
]


class Job:
    def __init__(self, fn, *args):
        self.id, self.lines, self.result, self.error, self.done, self.progress = uuid.uuid4().hex, [], None, None, False, ""
        threading.Thread(target=self._run, args=(fn, args), daemon=True).start()

    def log(self, text):
        self.lines.append(SECRET_RE.sub(r"\1 ********", str(text)))

    def _run(self, fn, args):
        try:
            self.result = fn(self, *args)
        except session.SessionError as exc:
            self.error = {"reason": exc.reason, "hint": exc.hint, "stage": exc.stage,
                          "need_new_password": isinstance(exc, session.NeedNewPassword)}
        except (adopt.AdoptError, ValueError) as exc:
            self.error = {"reason": str(exc), "hint": "", "stage": "input"}
        except Exception as exc:
            traceback.print_exc()
            self.error = {"reason": f"Unexpected error: {exc}", "hint": "", "stage": "internal"}
        self.done = True

    def view(self, since):
        return {"id": self.id, "done": self.done, "lines": self.lines[since:], "next": len(self.lines),
                "result": self.result, "error": self.error, "progress": self.progress}


def start(fn, *args):
    j = Job(fn, *args)
    JOBS[j.id] = j
    return j


# ---- jobs ----------------------------------------------------------------------------
def job_scan(job, extra):
    def prog(done, total):
        job.progress = f"{done} of {total} addresses"
    return discover.scan(extra, progress=prog)


def job_check(job, target):
    """Step by step, so a failure says exactly where it stopped."""
    steps = []
    add = lambda name, ok, detail="": (steps.append({"name": name, "ok": ok, "detail": detail}), job.log(("OK   " if ok else "FAIL ") + name + (f": {detail}" if detail else "")))
    m = target.get("method") or "ssh"
    if m != "console":
        host = session.valid_host(target.get("host"))
        add("Address is valid", True, host)
        same = [n for n in discover.local_networks() if _in(host, n["network"])]
        if same:
            add("Laptop is in the same network", True, f"{same[0]['adapter']} {same[0]['address']}/{same[0]['prefix']}")
        else:
            job.log(f"note: {host} is not in any of this laptop's networks; it will be reached through the default gateway")
        port = int(target.get("port") or (22 if m == "ssh" else 23))
        err = session.tcp_check(host, port)
        if err:
            add(f"{'SSH' if m == 'ssh' else 'Telnet'} port {port} answers", False, err.reason)
            alt = 23 if m == "ssh" else 22
            if err.stage == "port" or err.stage == "network":
                if session.tcp_check(host, alt, timeout=2) is None:
                    err.hint = f"Port {alt} ({'Telnet' if alt == 23 else 'SSH'}) is open on {host}: switch the connection method to " + \
                               ("Telnet." if alt == 23 else "SSH.")
            return {"connected": False, "steps": steps, "reason": err.reason, "hint": err.hint}
        add(f"{'SSH' if m == 'ssh' else 'Telnet'} port {port} answers", True)
    log = []
    try:
        sh = session.open_session(target, log)
    except session.NeedNewPassword as exc:
        add("Log in", False, exc.reason)
        return {"connected": False, "steps": steps, "reason": exc.reason, "hint": exc.hint, "need_new_password": True}
    except session.SessionError as exc:
        add("Log in", False, exc.reason)
        return {"connected": False, "steps": steps, "reason": exc.reason, "hint": exc.hint}
    try:
        add("Log in", True, ("password changed as asked; " if getattr(sh, "password_changed", False) else "") +
            (f"host key {sh.t.fingerprint}" if sh.t.fingerprint else f"via {sh.t.kind}"))
        try:
            info = session.identify(sh)
        except session.SessionError as exc:
            add("Identify the device", False, exc.reason)
            return {"connected": False, "steps": steps, "reason": exc.reason, "hint": exc.hint}
        add("Identify the device", True, f"{info['model'] or 'Huawei'}, VRP {info['version']}, name {info['sysname']}")
        return {"connected": True, "steps": steps, "device": info, "password_changed": getattr(sh, "password_changed", False)}
    finally:
        sh.close()


def _in(host, net):
    import ipaddress
    try:
        return ipaddress.ip_address(host) in ipaddress.ip_network(net)
    except ValueError:
        return False


def job_check_config(job, target, text):
    return configure.check_only(target, text, job.log)


def job_apply(job, target, fields, text):
    return configure.apply(target, fields, text, job.log)


def job_finish(job, host, username, password):
    return configure.finish_at(session.valid_host(host), username, password, job.log)


# ---- http ----------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "HuaweiSetup"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(data)

    def _ok_origin(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost") and self.headers.get("X-Token") == TOKEN

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            with open(os.path.join(HERE, "web", "index.html"), "rb") as f:
                return self._send(200, f.read().replace(b"__TOKEN__", TOKEN.encode()).replace(b"__VERSION__", VERSION.encode()),
                                  "text/html; charset=utf-8")
        if not path.startswith("/api/") or not self._ok_origin():
            return self._send(404, {"error": "not found"})
        LAST_SEEN[0] = time.time()
        if path == "/api/info":
            return self._send(200, {"networks": discover.local_networks(), "factory": discover.FACTORY,
                                    "logins": FACTORY_LOGINS, "serial_ports": session.list_serial_ports(),
                                    "platforms": adopt.PLATFORMS, "version": VERSION})
        if path == "/api/job":
            q = dict(p.split("=", 1) for p in self.path.split("?", 1)[-1].split("&") if "=" in p)
            j = JOBS.get(q.get("id", ""))
            if not j:
                return self._send(404, {"error": "no such job"})
            return self._send(200, j.view(int(q.get("since", "0") or 0)))
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._ok_origin():
            return self._send(403, {"error": "forbidden"})
        LAST_SEEN[0] = time.time()
        n = int(self.headers.get("Content-Length") or 0)
        try:
            d = json.loads(self.rfile.read(min(n, 1_000_000)) or b"{}")
        except ValueError:
            return self._send(400, {"error": "bad request"})
        path = self.path.split("?")[0]
        t = d.get("target") or {}
        try:
            if path == "/api/scan":
                discover.scan_targets([], d.get("extra") or "")       # validate before starting
                return self._send(200, {"job": start(job_scan, d.get("extra") or "").id})
            if path == "/api/check":
                return self._send(200, {"job": start(job_check, t).id})
            if path == "/api/build":
                lines = configure.build(d.get("fields") or {}, d.get("platform") or "", t.get("method") or "ssh")
                return self._send(200, {"text": "\n".join(lines)})
            if path == "/api/check-config":
                configure.split_phases(d.get("text") or "")
                return self._send(200, {"job": start(job_check_config, t, d.get("text") or "").id})
            if path == "/api/apply":
                configure.split_phases(d.get("text") or "")
                return self._send(200, {"job": start(job_apply, t, d.get("fields") or {}, d.get("text") or "").id})
            if path == "/api/finish":
                return self._send(200, {"job": start(job_finish, d.get("host"), d.get("username"), d.get("password")).id})
            if path == "/api/quit":
                self._send(200, {"ok": True})
                threading.Timer(0.3, lambda: os._exit(0)).start()
                return
        except (adopt.AdoptError, ValueError) as exc:
            return self._send(400, {"error": str(exc)})
        return self._send(404, {"error": "not found"})


def watchdog():
    while True:
        time.sleep(30)
        if time.time() - LAST_SEEN[0] > IDLE_EXIT and not any(not j.done for j in JOBS.values()):
            os._exit(0)


def main():
    port = int(os.environ.get("HUAWEI_SETUP_PORT") or 0)
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    print(f"Huawei Device Setup {VERSION} is running at {url}\nClose this window (or press Quit on the page) to stop it.", flush=True)
    threading.Thread(target=watchdog, daemon=True).start()
    if not os.environ.get("HUAWEI_SETUP_NO_BROWSER"):
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
