"""Set up a new device plugged into one of the router's own ports, through the router.

Read-only on the router: nothing here changes the router's configuration.
  1. plan:  read the port's addresses, LLDP and whether the router's SSH client accepts new
            devices. The router can only reach a device in a network the port already has.
  2. look:  ping the candidates and read ARP on the port (display commands and ping only).
  3. check / run: log in to the device from the router's command line (stelnet, or telnet;
            the device's key is accepted for the session but never stored on the router) and
            drive it with the laptop tool's engine (setup-tool/session.py, configure.py).
A brand-new box on a port with no address needs the laptop tool instead.
While a step runs it holds the router's SSH line, so the GUI's own reads wait for it.
"""
import ipaddress
import os
import re
import sys
import threading
import time
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "setup-tool"))
import session as devsession        # noqa: E402
import configure as devconfigure    # noqa: E402

FACTORY_NETS = {"192.168.1.0/24": ["192.168.1.253", "192.168.1.1"], "192.168.0.0/24": ["192.168.0.1"]}    # tried when the port is in one
NAME_RE = re.compile(r"^[A-Za-z0-9/.:\-]{1,64}$")


class PortSetupError(ValueError):
    pass


def ssh_conn(r):
    """The netmiko connection of a RealRouter, or of the SSH half of an SNMP session."""
    target = getattr(r, "ssh", None) or r
    conn = getattr(target, "conn", None)
    if conn is None:
        raise PortSetupError("This needs the router's command line. Sign in with your SSH login first.")
    return conn


# ---- reaching the device through the router -------------------------------------------
class HopTransport:
    """The device's command line, opened from the router's (stelnet or telnet in user view)."""
    kind = "router"

    def __init__(self, conn, host, method="ssh", source=""):
        self.conn, self.logged_in, self.fingerprint = conn, False, ""
        self.home_prompt = (conn.base_prompt or "").strip("<>[]~*")
        try:
            conn.clear_buffer()
        except Exception:
            pass
        cmd = ("stelnet" if method == "ssh" else "telnet") + (f" -a {source}" if source else "") + f" {host}"
        conn.write_channel(cmd + conn.RETURN)

    def send(self, text):
        self.conn.write_channel(text.replace("\n", self.conn.RETURN))

    def recv(self):
        return self.conn.read_channel() or ""

    def close(self):
        """Leave the device and make sure the router's own prompt is back."""
        home = re.compile(r"<%s>\s*$" % re.escape(self.home_prompt))
        buf = ""
        # a newline first: "quit" at the router's own prompt would log the router line out
        for keys in ("\n", "quit\n", "\n", "quit\n", "\x0b", "\n", "\x03", "\n", "quit\n", "\n"):
            self.send(keys)
            end = time.time() + (2 if keys == "\n" else 4)
            while time.time() < end:
                buf += self.recv()
                if home.search(buf):
                    try:
                        self.conn.clear_buffer()
                    except Exception:
                        pass
                    return
                time.sleep(0.1)
        raise PortSetupError("The router's command line did not come back after leaving the device. Reconnect to the router.")


class ViaRouter:
    """configure.apply's way of reaching the device, through the router."""
    where = "The router"

    def __init__(self, conn, target):
        self.conn, self.target = conn, target

    def _open(self, host, method, username, password, new_password=""):
        sh = devsession.Shell(HopTransport(self.conn, host, method))
        try:
            sh.password_changed = sh.login(username, password, new_password)
            sh.cmd("screen-length 0 temporary", timeout=15)
        except Exception:
            try:
                sh.t.close()
            except Exception:
                pass
            raise
        return sh

    def first(self):
        t = self.target
        return self._open(t["host"], t.get("method") or "ssh", t.get("username") or "", t.get("password") or "", t.get("new_password") or "")

    def login(self, host, username, password):
        return self._open(host, "ssh", username, password)

    def reachable(self, host):
        out = self.conn.send_command(f"ping -c 2 -t 500 {host}", read_timeout=20)
        return bool(re.search(r"Reply from|bytes from", out)) and not re.search(r"100(\.0+)?% packet loss", out)


# ---- the plan ------------------------------------------------------------------------
def port_networks(row):
    out = []
    for c in [row.get("ip") or ""] + list(row.get("secondary") or []):
        try:
            itf = ipaddress.ip_interface(c)
        except ValueError:
            continue
        if itf.version == 4 and itf.network.prefixlen < 32:
            out.append(itf)
    return out


def plan(r, port):
    """Read-only: what is on this port, and can the router reach it as the router is now."""
    if not NAME_RE.match(port or "") or "." in port:
        raise PortSetupError("Pick a physical port.")
    rows = {x["name"]: x for x in r.interfaces()}
    row = rows.get(port)
    if not row or row.get("type") not in ("ethernet", "management"):
        raise PortSetupError(f"{port} is not a physical port on this router.")
    nets = port_networks(row)
    checks = []
    add = lambda name, ok, detail="": checks.append({"name": name, "ok": ok, "detail": detail})
    add("The port has an address the router can reach the device from", bool(nets),
        ", ".join(str(n) for n in nets) or "no IP address on this port")
    first_time = bool(re.search(r"^\s*ssh client first-time enable", r.run("display current-configuration | include first-time"), re.M))
    add("The router's SSH client accepts a new device (ssh client first-time enable)", first_time,
        "" if first_time else "not set: use Telnet from the router, or the laptop tool")
    lldp = lldp_on(r, port)
    own = {str(n.ip) for n in nets}
    candidates = []
    if lldp.get("address") and any(ipaddress.ip_address(lldp["address"]) in n.network for n in nets):
        candidates.append(lldp["address"])
    for n in nets:
        for f in FACTORY_NETS.get(str(n.network), []):
            if f not in own:
                candidates.append(f)
    blockers = [] if nets else ["This port has no IP address, so the router cannot reach a device on it without a change to the router. "
                                "Set up a brand-new box with the laptop tool, or give the port an address first."]
    return {"port": port, "ok": bool(nets), "blockers": blockers, "checks": checks, "networks": [str(n) for n in nets],
            "own": sorted(own), "lldp": lldp, "candidates": list(dict.fromkeys(candidates)), "first_time": first_time,
            "description": row.get("description") or "", "state": row.get("phy") or ""}


def lldp_on(r, port):
    try:
        text = r.run(f"display lldp neighbor interface {port}", read_timeout=20)
    except Exception:
        return {}
    g = lambda pat: (re.search(pat, text, re.I | re.M) or [None, ""])[1].strip()
    return {"name": g(r"System name\s*:\s*(.+)$"), "description": g(r"System description\s*:\s*(.+)$")[:160],
            "address": g(r"Management address\s*(?:value)?\s*:\s*(\d+\.\d+\.\d+\.\d+)"), "port": g(r"Port ID\s*:\s*(.+)$")}


def find(r, conn, plan_):
    """From the router: ping the candidate addresses and read ARP on the port."""
    found = []
    for a in plan_["candidates"]:
        out = conn.send_command(f"ping -c 2 -t 300 {a}", read_timeout=15)
        if re.search(r"Reply from|bytes from", out):
            found.append(a)
    arp = conn.send_command(f"display arp interface {plan_['port']}", read_timeout=15)
    for m in re.finditer(r"^(\d+\.\d+\.\d+\.\d+)\s+([0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4})", arp, re.M | re.I):
        if m.group(1) not in found and m.group(1) not in plan_.get("own", []):
            found.append(m.group(1))
    return found


# ---- jobs ----------------------------------------------------------------------------
SECRET_RE = re.compile(r"(password irreversible-cipher|cipher)\s+\S+")


class Job:
    def __init__(self, owner, fn):
        self.id, self.owner, self.lines, self.result, self.error, self.done = uuid.uuid4().hex, owner, [], None, None, False
        self.started = time.time()
        threading.Thread(target=self._run, args=(fn,), daemon=True).start()

    def log(self, text):
        self.lines.append(SECRET_RE.sub(r"\1 ********", str(text)))

    def _run(self, fn):
        try:
            self.result = fn(self)
        except devsession.SessionError as exc:
            self.error = {"reason": exc.reason, "hint": exc.hint, "need_new_password": isinstance(exc, devsession.NeedNewPassword)}
        except Exception as exc:
            self.error = {"reason": str(exc), "hint": ""}
        self.done = True

    def view(self, since):
        return {"done": self.done, "lines": self.lines[since:], "next": len(self.lines), "result": self.result, "error": self.error}


JOBS = {}
STATE = {}       # router session id -> {"plan": ..., "open": bool}


def start(owner, fn):
    for k in [k for k, j in JOBS.items() if j.done and time.time() - j.started > 3600]:
        JOBS.pop(k, None)
    j = Job(owner, fn)
    JOBS[j.id] = j
    return j.id


def locked(r, fn):
    """Run fn(conn) holding the router's SSH line, so nothing else talks on it meanwhile."""
    with r.lock:
        conn = ssh_conn(r)
        try:
            return fn(conn)
        finally:
            r.cache.clear()
