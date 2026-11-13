"""Set up a new device plugged into one of the router's own ports, through the router.

The router reaches the new device for us:
  1. plan:  check the port is free, that the factory network (192.168.1.0/24) is not routed
            anywhere on the router, read LLDP on the port, and list the router lines needed:
            a temporary address in the factory network on the port (and, if asked, the port's
            permanent address for the device's management network), plus
            "ssh client first-time enable" so the router's SSH client accepts a new device.
  2. open:  apply those lines (committed, not saved) and look for the device from the router.
  3. check / apply: log in to the device from the router's command line (stelnet, or telnet)
            and drive it with the laptop tool's engine (setup-tool/session.py, configure.py):
            forced password change, initial configuration, commit, save, new admin login test,
            then the new management address.
  4. close: remove the temporary address and anything else that was only for the setup.
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

FACTORY_NETS = {"192.168.1.0/24": ["192.168.1.253", "192.168.1.1"], "192.168.0.0/24": ["192.168.0.1"]}
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
def plan(r, port, factory_net, router_addr=""):
    if not NAME_RE.match(port or "") or "." in port:
        raise PortSetupError("Pick a physical port.")
    if factory_net not in FACTORY_NETS:
        raise PortSetupError("Pick one of the factory networks.")
    rows = {x["name"]: x for x in r.interfaces()}
    row = rows.get(port)
    if not row or row.get("type") not in ("ethernet", "management"):
        raise PortSetupError(f"{port} is not a physical port on this router.")
    net = ipaddress.ip_network(factory_net)
    temp = ipaddress.ip_interface(f"{list(net.hosts())[9]}/{net.prefixlen}")      # 192.168.1.10/24
    checks, blockers = [], []
    add = lambda name, ok, detail="": checks.append({"name": name, "ok": ok, "detail": detail}) or (None if ok else blockers.append(f"{name}: {detail}"))
    subs = [n for n in rows if n.startswith(port + ".")]
    add("Port has no sub-interfaces", not subs, ", ".join(subs[:5]) + (" …" if len(subs) > 5 else ""))
    add("Port is a routed (layer 3) port", not row.get("l2"), "it is a switch port (portswitch)" if row.get("l2") else "")
    has_ip = row.get("ip") and row["ip"] not in ("unassigned", "")
    add("Port has no IP address yet", not has_ip, f"it has {row.get('ip')}: it is in use" if has_ip else "")
    perm = None
    if router_addr.strip():
        try:
            perm = ipaddress.ip_interface(router_addr.strip())
        except ValueError:
            raise PortSetupError("Router port address: use address/prefix, e.g. 10.10.5.1/30.")
        if perm.version != 4 or perm.network.prefixlen >= 32 or perm.ip in (perm.network.network_address, perm.network.broadcast_address):
            raise PortSetupError("Router port address: a usable IPv4 address with its prefix, e.g. 10.10.5.1/30.")
        if perm.network.overlaps(net):
            raise PortSetupError("Router port address must not be in the factory network.")
    for n in [net] + ([perm.network] if perm else []):
        routes = used_routes(r, n)
        label = "factory network" if n == net else "management network"
        add(f"The {label} {n} is not used on the router", not routes,
            ", ".join(f"{x['destination']} via {x['interface'] or x['next_hop']}" for x in routes[:3]))
    lldp = lldp_on(r, port)
    first_time = bool(re.search(r"^\s*ssh client first-time enable", r.run("display current-configuration | include first-time"), re.M))
    lines = [f"interface {port}"]
    if "*down" in (row.get("phy") or "") or row.get("admin_down"):
        lines.append(" undo shutdown")
    if perm:
        lines += [f" ip address {perm.ip} {perm.network.netmask}", f" ip address {temp.ip} {temp.network.netmask} sub"]
    else:
        lines.append(f" ip address {temp.ip} {temp.network.netmask}")
    lines.append("quit")
    undo = [f"interface {port}", f" undo ip address {temp.ip} {temp.network.netmask}" + (" sub" if perm else ""), "quit"]
    if not first_time:
        lines.append("ssh client first-time enable")
        undo.append("undo ssh client first-time enable")
    candidates = ([lldp["address"]] if lldp.get("address") else []) + FACTORY_NETS[factory_net]
    return {"port": port, "network": factory_net, "ok": not blockers, "blockers": blockers, "checks": checks, "lines": lines, "undo": undo,
            "temp": str(temp), "permanent": str(perm) if perm else "", "lldp": lldp, "candidates": list(dict.fromkeys(candidates)),
            "description": row.get("description") or "", "state": row.get("phy") or ""}


def used_routes(r, net):
    """Routes on the router that overlap `net`: more specific ones inside it, and the route that
    covers it now (anything but the default route)."""
    from parsers import parse_routing_table
    rows = parse_routing_table(r.run(f"display ip routing-table {net.network_address} {net.prefixlen} longer-match", read_timeout=30))
    rows += parse_routing_table(r.run(f"display ip routing-table {next(net.hosts())}", read_timeout=30))
    seen, out = set(), []
    for x in rows:
        if x["destination"].startswith("0.0.0.0/0") or x["destination"] in seen:
            continue
        seen.add(x["destination"])
        out.append(x)
    return out


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
    temp_ip = plan_["temp"].split("/")[0]
    for m in re.finditer(r"^(\d+\.\d+\.\d+\.\d+)\s+([0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4})", arp, re.M | re.I):
        if m.group(1) not in found and m.group(1) != temp_ip:
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
