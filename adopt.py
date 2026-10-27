"""Adopt new Huawei routers and switches.

1. Find them: a subnet scan (TCP connect to the SSH port and read the SSH banner; Huawei
   devices answer "SSH-2.0-HUAWEI-...") and the LLDP neighbours of every saved router that
   has an SNMP login (LLDP-MIB, including the neighbour's management address).
2. Build the initial configuration from a template (hostname, management address, admin
   user, SSH only, SNMPv3, NTP, syslog, banner). The admin sees and can edit every line.
3. Log in with the device's current (factory) login, check the platform, send the lines,
   commit (VRP8) and save. "Check only" sends the lines and aborts (VRP8 only).
Nothing here writes a password to disk. Only the SNMP login is kept, in routers.json.
"""
import concurrent.futures
import ipaddress
import re
import socket
import time

from actions import ActionError, NAME_RE, WORD_RE

MAX_HOSTS = 1024
LLDP_REM = "1.0.8802.1.1.2.1.4.1.1"          # .7 port id, .9 system name, .10 system description
LLDP_MAN = "1.0.8802.1.1.2.1.4.2.1.3"        # lldpRemManAddrIfSubtype, indexed by ...addrSubtype.len.addr
LLDP_LOC_PORT = "1.0.8802.1.1.2.1.3.7.1.3"
PHASE2 = "# --- applied last: new management address (the session may move to it) ---"
SNMP_AUTH_CLI = {"sha256": "sha2-256", "sha": "sha", "md5": "md5"}
SNMP_PRIV_CLI = {"aes128": "aes128", "aes256": "aes256", "des": "des56"}
PLATFORMS = {"vrp8": "NE / CE router (VRP8, two-stage commit)", "vrp5": "S switch (VRP5, applies each line at once)"}


class AdoptError(ValueError):
    pass


# ---- discovery -----------------------------------------------------------------
def ssh_banner(host, port=22, timeout=1.5):
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.settimeout(timeout)
            try:
                data = s.recv(200)
            except socket.timeout:
                data = b""
            return data.decode("latin-1", "replace").strip() or "(open, no banner)"
    except OSError:
        return None


def vendor_of(text):
    t = (text or "").lower()
    for key, name in (("huawei", "Huawei"), ("rosssh", "MikroTik"), ("mikrotik", "MikroTik"), ("cisco", "Cisco"),
                      ("juniper", "Juniper"), ("openssh", "Linux / other"), ("dropbear", "Embedded / other")):
        if key in t:
            return name
    return ""


def guess_platform(text):
    """'' when the text does not say (an SSH banner never does)."""
    t = (text or "").lower()
    if re.search(r"\bs\d{4}|quidway|version 5\.", t):
        return "vrp5"
    if re.search(r"\b(ne|ce|atn|cx)\d{2,}|version 8\.", t):
        return "vrp8"
    return ""


def scan(cidr, port, known, timeout=1.5, probe=ssh_banner):
    try:
        net = ipaddress.ip_network(cidr.strip(), strict=False)
    except ValueError:
        raise AdoptError("Enter a subnet like 10.10.0.0/24.")
    if net.version != 4:
        raise AdoptError("Only IPv4 subnets can be scanned.")
    hosts = [str(h) for h in (net.hosts() if net.num_addresses > 2 else net)]
    if len(hosts) > MAX_HOSTS:
        raise AdoptError(f"That subnet has {len(hosts)} addresses. Scan at most {MAX_HOSTS} (a /22) at a time.")
    with concurrent.futures.ThreadPoolExecutor(max_workers=128) as ex:
        banners = list(ex.map(lambda h: probe(h, port, timeout), hosts))
    rows = []
    for h, b in zip(hosts, banners):
        if b is None:
            continue
        v = vendor_of(b)
        rows.append({"host": h, "port": port, "source": "scan", "banner": b[:120], "vendor": v, "huawei": v == "Huawei",
                     "name": "", "platform": guess_platform(b), "saved": h in known, "via": ""})
    return rows


def _addr_from_index(idx):
    """lldpRemManAddrTable index: timeMark.localPort.remIndex.addrSubtype.addrLen.addr..."""
    p = idx.split(".")
    if len(p) >= 9 and p[3] == "1" and p[4] == "4":
        return ".".join(p[5:9]), ".".join(p[:3])
    return None, None


def lldp_candidates(client, router_name, known):
    # small GETBULK requests: LLDP rows carry long descriptions and big responses are dropped
    loc = {i: _txt(v) for i, v in client.walk(LLDP_LOC_PORT, 500, reps=10).items()}
    names, descs, ports = (client.walk(LLDP_REM + c, 500, reps=10) for c in (".9", ".10", ".7"))
    addrs = {}
    for idx in client.walk(LLDP_MAN, 1000, reps=10):
        a, key = _addr_from_index(idx)
        if a:
            addrs.setdefault(key, a)
    rows = []
    for idx in set(names) | set(descs):
        desc, name = _txt(descs.get(idx)), _txt(names.get(idx))
        parts = idx.split(".")
        a = addrs.get(idx, "")
        v = vendor_of(desc) or vendor_of(name)
        rows.append({"host": a, "port": 22, "source": "lldp", "banner": desc[:120], "vendor": v or "unknown", "huawei": v == "Huawei",
                     "name": name, "platform": guess_platform(desc), "saved": bool(a) and a in known,
                     "via": f"{router_name} {loc.get(parts[1], parts[1]) if len(parts) > 1 else ''} → {_txt(ports.get(idx))}".strip()})
    return rows


def _txt(v):
    if v is None:
        return ""
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace").strip("\x00").strip()
    return str(v)


# ---- template --------------------------------------------------------------------
def _ip(v, label):
    try:
        return str(ipaddress.ip_address(v.strip()))
    except ValueError:
        raise AdoptError(f"{label}: '{v}' is not an IPv4 address.")


def _secret(v, label, lo=8):
    v = v or ""
    if not lo <= len(v) <= 64 or any(ord(c) < 33 or c in "\"'?" for c in v):
        raise AdoptError(f"{label} must be {lo} to 64 characters with no spaces, quotes or '?'.")
    return v


def build(f, platform):
    """Initial configuration lines from the template fields. Raises AdoptError on bad input."""
    if platform not in PLATFORMS:
        raise AdoptError("Pick the platform.")
    g = lambda k: str(f.get(k) or "").strip()
    host = g("hostname")
    if not re.match(r"^[A-Za-z0-9_.\-]{1,64}$", host):
        raise AdoptError("Hostname: 1 to 64 letters, digits, _ . -")
    lines = [f"sysname {host}"]
    user = g("admin_user")
    if not WORD_RE.match(user or "-") or not user:
        raise AdoptError("Admin user: letters, digits, _ . - @")
    pw = _secret(f.get("admin_password"), "Admin password")
    lines += ["aaa", f" local-user {user} password irreversible-cipher {pw}", f" local-user {user} service-type ssh",
              f" local-user {user} level 3" if platform == "vrp8" else f" local-user {user} privilege level 15", "quit"]
    lines += ["stelnet server enable", f"ssh user {user}", f"ssh user {user} authentication-type password", f"ssh user {user} service-type stelnet",
              "user-interface vty 0 4", " authentication-mode aaa", " protocol inbound ssh" if f.get("ssh_only", True) else " protocol inbound all", "quit"]
    if f.get("ssh_only", True):
        lines.append("undo telnet server enable")
    su = g("snmp_user")
    if su:
        if not WORD_RE.match(su):
            raise AdoptError("SNMP user: letters, digits, _ . - @")
        ap, pp = g("snmp_auth_proto") or "sha256", g("snmp_priv_proto") or "aes128"
        if ap not in SNMP_AUTH_CLI or pp not in SNMP_PRIV_CLI:
            raise AdoptError("Unsupported SNMP protocol.")
        ak, pk = _secret(f.get("snmp_auth_key"), "SNMP authentication password"), _secret(f.get("snmp_priv_key"), "SNMP privacy password")
        acl = g("snmp_acl_source")
        if acl:
            src = _ip(acl, "SNMP allowed source")
            lines += ["acl number 2099", f" rule 5 permit source {src} 0", "quit"]
        lines += ["snmp-agent", "snmp-agent sys-info version v3", "snmp-agent mib-view included NE8000-ALL iso",
                  "snmp-agent group v3 NE8000MON privacy read-view NE8000-ALL" + (" acl 2099" if acl else ""),
                  f"snmp-agent usm-user v3 {su} group NE8000MON",
                  f"snmp-agent usm-user v3 {su} authentication-mode {SNMP_AUTH_CLI[ap]} cipher {ak}",
                  f"snmp-agent usm-user v3 {su} privacy-mode {SNMP_PRIV_CLI[pp]} cipher {pk}"]
    for k, cmd in (("ntp", "ntp-service unicast-server {}"), ("syslog", "info-center loghost {}")):
        for v in re.split(r"[,\s]+", g(k)):
            if v:
                lines.append(cmd.format(_ip(v, k.upper())))
    banner = g("banner")
    if banner:
        if len(banner) > 200 or any(ord(c) < 32 for c in banner) or '"' in banner:
            raise AdoptError("Banner: one line, up to 200 characters, no double quotes.")
        lines.append(f'header login information "{banner}"')
    gw = g("gateway")
    if gw:
        lines.append(f"ip route-static 0.0.0.0 0.0.0.0 {_ip(gw, 'Gateway')}")
    mif, mip = g("mgmt_interface"), g("mgmt_ip")
    if mip:
        try:
            ifc = ipaddress.ip_interface(mip)
        except ValueError:
            raise AdoptError("Management IP: use address/prefix, e.g. 10.10.0.21/24.")
        if not mif or not NAME_RE.match(mif):
            raise AdoptError("Management interface is required with a management IP (e.g. Vlanif10 or MEth0/0/0).")
        phase = []
        vlan = g("mgmt_vlan")
        if vlan:
            if not vlan.isdigit() or not 1 <= int(vlan) <= 4094:
                raise AdoptError("Management VLAN must be 1 to 4094.")
            phase.append(f"vlan batch {vlan}")
        phase += [f"interface {mif}", f" ip address {ifc.ip} {ifc.network.netmask}", "quit"]
        lines += [PHASE2] + phase
    return lines


def split_phases(text):
    """Edited textarea -> (phase1 lines, phase2 lines). Comment lines are dropped."""
    p1, p2, cur = [], [], None
    cur = p1
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.strip() == PHASE2.strip():
            cur = p2
            continue
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if any(ord(c) < 32 for c in line) or len(line) > 400:
            raise AdoptError("Each command must be one line of at most 400 characters.")
        if line.strip().split()[0] in ("return", "reboot", "reset", "delete", "format", "startup", "undo-all"):
            raise AdoptError(f"'{line.strip()}' is not allowed in an initial configuration.")
        cur.append(line)
    if not p1 and not p2:
        raise AdoptError("There are no commands to send.")
    return p1, p2


def new_address(p2):
    for l in p2:
        m = re.match(r"^\s*ip address (\S+) ", l)
        if m:
            return m.group(1)
    return ""


# ---- apply -----------------------------------------------------------------------
def ssh_connect(host, port, username, password):
    from netmiko import ConnectHandler
    return ConnectHandler(device_type="huawei", host=host, port=int(port), username=username, password=password,
                          timeout=30, conn_timeout=15, fast_cli=False)


def _send(conn, line, log):
    out = conn.send_command_timing(line, strip_prompt=False, strip_command=False, read_timeout=30, cmd_verify=False)
    if re.search(r"\[Y/N\]|\(y/n\)|\[y/n\]", out, re.I):
        out += conn.send_command_timing("Y", strip_prompt=False, strip_command=False, read_timeout=60, cmd_verify=False)
    log.append(f"> {line.strip()}\n{out.rstrip()}")
    return out


def detect(version_text):
    m = re.search(r"VRP \(R\) software, Version (\d+)", version_text)
    if m:
        return "vrp8" if int(m.group(1)) >= 8 else "vrp5"
    return ""


def _model(ver):
    for l in ver.splitlines():
        m = re.match(r"(?i)^\s*(huawei\s+\S+.*?)\s+uptime is", l)
        if m:
            return m.group(1)
    return ""


def identify(host, port, username, password, connect=ssh_connect):
    """Log in with the device's current login and read the platform and model."""
    try:
        conn = connect(host, port, username, password)
    except Exception as exc:
        raise AdoptError(f"Could not log in to {host}:{port}: {exc}")
    try:
        ver = conn.send_command_timing("display version", read_timeout=30)
    finally:
        try:
            conn.disconnect()
        except Exception:
            pass
    found = detect(ver)
    if not found:
        raise AdoptError("Logged in, but this does not look like a Huawei VRP device (display version shows no VRP version).")
    m = re.search(r"VRP \(R\) software, Version (\S+(?: \([^)]*\))?)", ver)
    return {"platform": found, "model": _model(ver), "version": m.group(1) if m else ""}


def apply(host, port, username, password, text, platform, commit, final_login, connect=ssh_connect):
    """Push the reviewed lines. Returns {ok, platform, log, host, saved, warnings}."""
    p1, p2 = split_phases(text)
    log, warnings = [], []
    try:
        conn = connect(host, port, username, password)
    except Exception as exc:
        raise AdoptError(f"Could not log in to {host}:{port}: {exc}")
    try:
        ver = conn.send_command_timing("display version", read_timeout=30)
        found = detect(ver)
        model = _model(ver)
        if not found:
            raise AdoptError("This does not look like a Huawei VRP device (display version gave no VRP version).")
        if found != platform:
            raise AdoptError(f"The device runs {'VRP8' if found == 'vrp8' else 'VRP5'} but the template is for "
                             f"{'VRP8' if platform == 'vrp8' else 'VRP5'}. Change the platform and review the commands again.")
        if not commit and platform != "vrp8":
            raise AdoptError("VRP5 switches apply each line at once, so there is no check-only mode. Review the lines and apply.")
        _send(conn, "system-view", log)
        errors = []
        for l in p1 + (p2 if not commit else []):
            out = _send(conn, l, log)
            if "Error:" in out:
                errors.append(f"{l.strip()}: {out[out.index('Error:'):].splitlines()[0]}")
        if not commit:
            _send(conn, "abort", log)
            _send(conn, "return", log)
            return {"ok": not errors, "checked": True, "errors": errors, "platform": found, "model": model, "log": "\n".join(log)}
        if errors and platform == "vrp8":
            _send(conn, "abort", log)
            raise AdoptError("The device rejected some lines, so nothing was committed:\n" + "\n".join(errors))
        if errors:
            warnings.append("Some lines were rejected (VRP5 applies the others at once):\n" + "\n".join(errors))
        if platform == "vrp8":
            out = _send(conn, "commit", log)
            if "Error:" in out:
                raise AdoptError("Commit failed:\n" + out)
        _send(conn, "return", log)
        _send(conn, "save", log)
        final_host = host
        if p2:
            _send(conn, "system-view", log)
            try:
                for l in p2:
                    _send(conn, l, log)
                if platform == "vrp8":
                    _send(conn, "commit", log)
                _send(conn, "return", log)
                _send(conn, "save", log)
            except Exception as exc:     # expected when the session's own address changed
                log.append(f"(session ended while moving the management address: {exc})")
            na = new_address(p2)
            if na and na != host:
                try:
                    c2 = connect(na, port, *final_login)
                    _send(c2, "save", log)
                    c2.disconnect()
                    final_host = na
                except Exception as exc:
                    warnings.append(f"Could not log in at the new address {na} ({exc}). The device is saved at {host}; check it and edit the address under Manager > Routers.")
        return {"ok": True, "platform": found, "model": model, "host": final_host, "warnings": warnings, "log": "\n".join(log)}
    finally:
        try:
            conn.disconnect()
        except Exception:
            pass


# ---- demo ------------------------------------------------------------------------
DEMO_DEVICES = {
    "192.168.50.11": ("SSH-2.0-HUAWEI-1.5", "vrp5", "HUAWEI S5735-L48T4X-A1"),
    "192.168.50.12": ("SSH-2.0-HUAWEI-1.5", "vrp8", "HUAWEI NE8000 M1C"),
    "192.168.50.20": ("SSH-2.0-ROSSSH", "", ""),
    "192.168.50.30": ("SSH-2.0-OpenSSH_8.9p1 Ubuntu-3", "", ""),
}


def demo_probe(h, port, timeout):
    d = DEMO_DEVICES.get(h)
    return d[0] if d else None


def demo_lldp(known):
    rows = [{"host": "192.168.50.12", "port": 22, "source": "lldp", "banner": "Huawei Versatile Routing Platform Software VRP (R) software, Version 8.220 (NE8000 M1C)",
             "vendor": "Huawei", "huawei": True, "name": "HUAWEI", "platform": "vrp8", "saved": "192.168.50.12" in known, "via": "demo GigabitEthernet0/1/4 → GigabitEthernet0/2/0"},
            {"host": "10.10.1.2", "port": 22, "source": "lldp", "banner": "MikroTik RouterOS 7.14", "vendor": "MikroTik", "huawei": False,
             "name": "Customer-A-CCR", "platform": "vrp8", "saved": False, "via": "demo GigabitEthernet0/1/0 → ether1"}]
    return rows


class DemoConn:
    """Pretends to be a factory-fresh Huawei device for demo mode."""
    moved = {}          # new management address -> original demo address

    def __init__(self, host, username, password):
        moved = host in self.moved
        host = self.moved.get(host, host)
        if host not in DEMO_DEVICES or not DEMO_DEVICES[host][1]:
            raise OSError("connection refused (demo: try 192.168.50.11 or 192.168.50.12)")
        if not moved and password != "Admin@huawei.com":
            raise OSError("authentication failed (demo factory password: Admin@huawei.com)")
        self.host, (_, self.platform, self.model) = host, DEMO_DEVICES[host]
        self.view = "user"

    def send_command_timing(self, line, **kw):
        l = line.strip()
        p = "<HUAWEI>" if self.view == "user" else "[~HUAWEI]"
        if l == "display version":
            v = "8.220 (NE8000 V800R022C00SPC600)" if self.platform == "vrp8" else "5.170 (S5735 V200R022C00SPC500)"
            return f"Huawei Versatile Routing Platform Software\nVRP (R) software, Version {v}\n{self.model} uptime is 0 days, 0 hours, 12 minutes\n{p}"
        if l == "system-view":
            self.view = "system"
            return "Enter system view, return user view with return command.\n[~HUAWEI]"
        if l in ("return", "abort"):
            self.view = "user"
            return "<HUAWEI>"
        if l == "save":
            return "Warning: The current configuration will be written to the device. Continue? [Y/N]:"
        if l == "Y":
            return "Info: Save the configuration successfully.\n<HUAWEI>"
        if l == "commit":
            return "[~HUAWEI]"
        if l.startswith("undo telnet") and self.platform == "vrp8":
            return "Warning: Telnet will be disabled. Continue? [Y/N]:"
        m = re.match(r"^ip address (\S+) ", l)
        if m:
            self.moved[m.group(1)] = self.host
        if l.startswith("bogus"):
            return "               ^\nError: Unrecognized command found at '^' position.\n[*HUAWEI]"
        return "[*HUAWEI]"

    def disconnect(self):
        pass


def demo_connect(host, port, username, password):
    time.sleep(0.2)
    return DemoConn(host, username, password)
