"""Every change the GUI can make, as a function that validates its inputs and
returns the exact VRP8 command lines. The GUI always shows these lines before
they are sent, so nothing is applied that the operator has not seen.

mode "config": lines run in system-view and are then committed.
mode "exec":   a single user-view command (reset, clear) run directly.
"""
import ipaddress
import re


class ActionError(ValueError):
    pass


NAME_RE = re.compile(r"^[A-Za-z0-9/.:\-]{1,64}$")
WORD_RE = re.compile(r"^[A-Za-z0-9_.\-@]{1,64}$")
ACTIONS = {}


def action(name, mode="config"):
    def wrap(fn):
        ACTIONS[name] = (fn, mode)
        return fn
    return wrap


# ---- validators --------------------------------------------------------------
def s(p, key, default=""):
    v = p.get(key, default)
    return ("" if v is None else str(v)).strip()


def need(p, key, label=None):
    v = s(p, key)
    if not v:
        raise ActionError(f"{label or key} is required.")
    return v


def iface(p, key="interface"):
    v = need(p, key, "Interface")
    if not NAME_RE.match(v):
        raise ActionError(f"Invalid interface name: {v}")
    return v


def word(p, key, label=None, required=True):
    v = need(p, key, label) if required else s(p, key)
    if v and not WORD_RE.match(v):
        raise ActionError(f"{label or key} may only contain letters, digits, _ . - @")
    return v


def text_line(p, key, maxlen=242):
    v = s(p, key)
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in v) or len(v) > maxlen:
        raise ActionError(f"{key} must be one line, at most {maxlen} characters.")
    return v


def integer(p, key, lo, hi, label=None, required=True, default=None):
    raw = s(p, key)
    if not raw:
        if required and default is None:
            raise ActionError(f"{label or key} is required.")
        return default
    try:
        v = int(raw)
    except ValueError:
        raise ActionError(f"{label or key} must be a number.")
    if not lo <= v <= hi:
        raise ActionError(f"{label or key} must be between {lo} and {hi}.")
    return v


def ip_iface(p, key="address"):
    """'10.0.0.1/24' -> ('10.0.0.1', 24)"""
    v = need(p, key, "IP address")
    try:
        i = ipaddress.ip_interface(v if "/" in v else v + "/32")
    except ValueError:
        raise ActionError(f"Invalid IP/prefix: {v}")
    if i.version != 4:
        raise ActionError("Only IPv4 is supported by this form; use the config editor for IPv6.")
    return str(i.ip), i.network.prefixlen


def ip_addr(p, key, label=None, required=True):
    v = need(p, key, label) if required else s(p, key)
    if not v:
        return ""
    try:
        return str(ipaddress.ip_address(v))
    except ValueError:
        raise ActionError(f"Invalid IP address: {v}")


def prefix(p, key="prefix"):
    v = need(p, key, "Destination")
    try:
        n = ipaddress.ip_network(v, strict=False)
    except ValueError:
        raise ActionError(f"Invalid destination prefix: {v}")
    return str(n.network_address), n.prefixlen


def vlan_list(p, key="vlans"):
    v = need(p, key, "VLANs")
    out = []
    for part in re.split(r"[,\s]+", v):
        m = re.match(r"^(\d+)(?:-(\d+))?$", part)
        if not m:
            raise ActionError(f"Invalid VLAN list item: {part}")
        a, b = int(m.group(1)), int(m.group(2) or m.group(1))
        if not (1 <= a <= 4094 and 1 <= b <= 4094 and a <= b):
            raise ActionError("VLAN IDs must be 1-4094.")
        out.append(f"{a} to {b}" if a != b else str(a))
    return " ".join(out)


def asn(p, key, label="AS number"):
    v = need(p, key, label)
    if not re.match(r"^\d{1,10}(\.\d{1,5})?$", v):
        raise ActionError(f"Invalid {label}: {v}")
    return v


# ---- interfaces --------------------------------------------------------------
@action("interface.set")
def interface_set(p):
    name = iface(p)
    lines = [f"interface {name}"]
    if "enabled" in p:
        lines.append("undo shutdown" if p["enabled"] in (True, "true", "1", 1) else "shutdown")
    if "description" in p:
        d = text_line(p, "description")
        lines.append(f"description {d}" if d else "undo description")
    if s(p, "mtu"):
        lines.append(f"mtu {integer(p, 'mtu', 46, 9600, 'MTU')}")
    if len(lines) == 1:
        raise ActionError("Nothing to change.")
    return lines


@action("interface.ip_add")
def interface_ip_add(p):
    name = iface(p)
    ip, plen = ip_iface(p)
    line = f"ip address {ip} {plen}" + (" sub" if p.get("secondary") in (True, "true", "1") else "")
    lines = [f"interface {name}"]
    vpn = word(p, "vpn", "VPN instance", required=False)
    if vpn:
        lines.append(f"ip binding vpn-instance {vpn}")
    return lines + [line]


@action("interface.ip_remove")
def interface_ip_remove(p):
    name = iface(p)
    ip, plen = ip_iface(p)
    return [f"interface {name}", f"undo ip address {ip} {plen}" + (" sub" if p.get("secondary") in (True, "true", "1") else "")]


@action("interface.reset_counters", mode="exec")
def interface_reset_counters(p):
    return [f"reset counters interface {iface(p)}"]


@action("interface.delete")
def interface_delete(p):
    name = iface(p)
    if "." not in name and not re.match(r"(?i)^(vlanif|loopback|tunnel|eth-trunk)", name):
        raise ActionError("Only sub-interfaces and logical interfaces can be deleted.")
    return [f"undo interface {name}"]


@action("loopback.create")
def loopback_create(p):
    n = integer(p, "number", 0, 1023, "Loopback number")
    ip, plen = ip_iface(p)
    return [f"interface LoopBack{n}", f"ip address {ip} {plen}"] + \
           ([f"description {text_line(p, 'description')}"] if s(p, "description") else [])


# ---- VLAN --------------------------------------------------------------------
@action("subif.create")
def subif_create(p):
    parent = iface(p, "parent")
    vid = integer(p, "vlan", 1, 4094, "VLAN ID")
    sub = integer(p, "number", 1, 4095, "Sub-interface number", required=False, default=vid)
    lines = [f"interface {parent}.{sub}"]
    encap = s(p, "encap") or "vlan-type"
    if encap == "termination":
        lines.append(f"dot1q termination vid {vid}")
    elif encap == "qinq":
        inner = integer(p, "inner_vlan", 1, 4094, "Inner VLAN")
        lines.append(f"qinq termination pe-vid {vid} ce-vid {inner}")
    else:
        lines.append(f"vlan-type dot1q {vid}")
    vpn = word(p, "vpn", "VPN instance", required=False)
    if vpn:
        lines.append(f"ip binding vpn-instance {vpn}")
    if s(p, "address"):
        ip, plen = ip_iface(p)
        lines.append(f"ip address {ip} {plen}")
    if encap in ("termination", "qinq") and s(p, "address"):
        lines.append("arp broadcast enable")
    if s(p, "description"):
        lines.append(f"description {text_line(p, 'description')}")
    return lines


@action("vlan.create")
def vlan_create(p):
    vid = integer(p, "vlan", 1, 4094, "VLAN ID")
    lines = [f"vlan {vid}"]
    if s(p, "description"):
        lines.append(f"description {text_line(p, 'description', 80)}")
    if s(p, "address"):
        ip, plen = ip_iface(p)
        lines += [f"interface Vlanif{vid}", f"ip address {ip} {plen}"]
    return lines


@action("vlan.delete")
def vlan_delete(p):
    vid = integer(p, "vlan", 1, 4094, "VLAN ID")
    lines = []
    if p.get("with_vlanif") in (True, "true", "1"):
        lines.append(f"undo interface Vlanif{vid}")
    return lines + [f"undo vlan {vid}"]


@action("vlan.port")
def vlan_port(p):
    name = iface(p)
    mode = s(p, "mode") or "trunk"
    lines = [f"interface {name}", "portswitch"]
    if mode == "access":
        vid = integer(p, "vlan", 1, 4094, "VLAN ID")
        lines += ["port link-type access", f"port default vlan {vid}"]
    elif mode == "trunk":
        lines += ["port link-type trunk", f"port trunk allow-pass vlan {vlan_list(p)}"]
        if s(p, "pvid"):
            lines.append(f"port trunk pvid vlan {integer(p, 'pvid', 1, 4094, 'Native VLAN')}")
    elif mode == "trunk_remove":
        lines = [f"interface {name}", f"undo port trunk allow-pass vlan {vlan_list(p)}"]
    elif mode == "routed":
        lines = [f"interface {name}", "undo portswitch"]
    else:
        raise ActionError("Unknown port mode.")
    return lines


# ---- QoS / queues ------------------------------------------------------------
@action("qos.car")
def qos_car(p):
    name = iface(p)
    direction = s(p, "direction") or "inbound"
    if direction not in ("inbound", "outbound"):
        raise ActionError("Direction must be inbound or outbound.")
    cir = integer(p, "cir", 8, 400_000_000, "CIR (kbit/s)")
    pir = integer(p, "pir", cir, 400_000_000, "PIR (kbit/s)", required=False)
    line = f"qos car cir {cir}" + (f" pir {pir}" if pir else "") + " green pass yellow pass red discard " + direction
    return [f"interface {name}", line]


@action("qos.car_remove")
def qos_car_remove(p):
    name = iface(p)
    direction = s(p, "direction") or "inbound"
    if direction not in ("inbound", "outbound"):
        raise ActionError("Direction must be inbound or outbound.")
    return [f"interface {name}", f"undo qos car {direction}"]


def _dirs(p):
    d = s(p, "direction") or "both"
    if d not in ("both", "inbound", "outbound"):
        raise ActionError("Direction must be both, inbound or outbound.")
    return ["inbound", "outbound"] if d == "both" else [d]


@action("qos.profile_apply")
def qos_profile_apply(p):
    """Bind a QoS profile (its CAR rate) to an interface, like the router's reseller sub-interfaces."""
    name, prof = iface(p), word(p, "profile", "Profile")
    out = [f"interface {name}"]
    for d in _dirs(p):
        cur = p.get("current") if isinstance(p.get("current"), dict) else {}
        old = s(p, "old_" + d) or str((cur.get(name) or {}).get(d) or "")
        if old and old != prof:
            out.append(f"undo qos-profile {word({'x': old}, 'x', 'Current profile')} {d} identifier none")
        out.append(f"qos-profile {prof} {d} identifier none")
    return out


@action("qos.profile_remove")
def qos_profile_remove(p):
    name, prof = iface(p), word(p, "profile", "Profile")
    return [f"interface {name}"] + [f"undo qos-profile {prof} {d} identifier none" for d in _dirs(p)]


@action("qos.profile_create")
def qos_profile_create(p):
    """A QoS profile with one CAR rate. Rates in kbit/s; burst sizes default to the router's."""
    prof = word(p, "name", "Profile name")
    cir = integer(p, "cir", 8, 400_000_000, "CIR (kbit/s)")
    pir = integer(p, "pir", cir, 400_000_000, "PIR (kbit/s)", required=False)
    return [f"qos-profile {prof}", f"car cir {cir}" + (f" pir {pir}" if pir else "") + " green pass yellow pass red discard", "quit"]


@action("qos.shaping")
def qos_shaping(p):
    name = iface(p)
    mbps = integer(p, "mbps", 1, 400_000, "Shaping rate (Mbit/s)")
    return [f"interface {name}", f"port shaping {mbps}"]


@action("qos.shaping_remove")
def qos_shaping_remove(p):
    return [f"interface {iface(p)}", "undo port shaping"]


@action("qos.queue")
def qos_queue(p):
    """Per-queue scheduling on a physical port (port-queue)."""
    name = iface(p)
    q = s(p, "queue").lower()
    if q not in ("be", "af1", "af2", "af3", "af4", "ef", "cs6", "cs7"):
        raise ActionError("Queue must be one of be, af1-af4, ef, cs6, cs7.")
    sched = s(p, "schedule") or "wfq"
    if sched not in ("wfq", "pq", "lpq"):
        raise ActionError("Schedule must be wfq, pq or lpq.")
    line = f"port-queue {q} {sched}"
    if sched == "wfq" and s(p, "weight"):
        line += f" weight {integer(p, 'weight', 1, 100, 'Weight')}"
    if s(p, "shaping_pct"):
        line += f" shaping shaping-percentage {integer(p, 'shaping_pct', 1, 100, 'Shaping %')}"
    return [f"interface {name}", line + " outbound"]


@action("qos.policy_bind")
def qos_policy_bind(p):
    name = iface(p)
    policy = word(p, "policy", "Traffic policy")
    direction = s(p, "direction") or "inbound"
    if direction not in ("inbound", "outbound"):
        raise ActionError("Direction must be inbound or outbound.")
    return [f"interface {name}", f"traffic-policy {policy} {direction}"]


@action("qos.policy_unbind")
def qos_policy_unbind(p):
    direction = s(p, "direction") or "inbound"
    if direction not in ("inbound", "outbound"):
        raise ActionError("Direction must be inbound or outbound.")
    return [f"interface {iface(p)}", f"undo traffic-policy {direction}"]


@action("qos.simple_policy")
def qos_simple_policy(p):
    """Classifier (ACL match) + behavior (CAR) + policy in one go, like a MikroTik simple queue with a target."""
    name = word(p, "name", "Policy name")
    acl = integer(p, "acl", 3000, 3999, "Advanced ACL number")
    src = s(p, "source")
    dst = s(p, "destination")
    cir = integer(p, "cir", 8, 400_000_000, "CIR (kbit/s)")
    rule = "rule 5 permit ip"
    for label, val in (("source", src), ("destination", dst)):
        if val:
            try:
                net = ipaddress.ip_network(val, strict=False)
            except ValueError:
                raise ActionError(f"Invalid {label} prefix: {val}")
            rule += f" {label} {net.network_address} {net.hostmask}"
    return [f"acl number {acl}", rule, "quit",
            f"traffic classifier {name}_c", f"if-match acl {acl}", "quit",
            f"traffic behavior {name}_b", f"car cir {cir} green pass yellow pass red discard", "quit",
            f"traffic policy {name}", f"classifier {name}_c behavior {name}_b", "quit"]


# ---- IP / routing ------------------------------------------------------------
@action("route.static_add")
def route_static_add(p):
    net, plen = prefix(p)
    nh = ip_addr(p, "next_hop", "Next hop", required=False)
    out_if = s(p, "interface")
    if out_if and not NAME_RE.match(out_if):
        raise ActionError("Invalid interface.")
    if not nh and not out_if:
        raise ActionError("Give a next hop, an outgoing interface, or both.")
    vpn = word(p, "vpn", "VPN instance", required=False)
    line = "ip route-static " + (f"vpn-instance {vpn} " if vpn else "") + f"{net} {plen}"
    line += (f" {out_if}" if out_if else "") + (f" {nh}" if nh else "")
    if s(p, "preference"):
        line += f" preference {integer(p, 'preference', 1, 255, 'Preference')}"
    if s(p, "description"):
        line += f" description {text_line(p, 'description', 80)}"
    return [line]


@action("route.static_remove")
def route_static_remove(p):
    line = need(p, "line", "Route")
    if not line.startswith("ip route-static ") or "\n" in line:
        raise ActionError("Invalid route line.")
    line = re.sub(r"\s+description\s+.*$", "", line)
    return ["undo " + line]


@action("arp.static_add")
def arp_static_add(p):
    ip = ip_addr(p, "ip", "IP")
    mac = need(p, "mac", "MAC").lower().replace(":", "").replace("-", "").replace(".", "")
    if not re.match(r"^[0-9a-f]{12}$", mac):
        raise ActionError("Invalid MAC address.")
    mac = f"{mac[0:4]}-{mac[4:8]}-{mac[8:12]}"
    line = f"arp static {ip} {mac}"
    vpn = word(p, "vpn", "VPN instance", required=False)
    if vpn:
        line += f" vpn-instance {vpn}"
    return [line]


@action("arp.static_remove")
def arp_static_remove(p):
    return [f"undo arp static {ip_addr(p, 'ip', 'IP')}"]


@action("vpn.create")
def vpn_create(p):
    name = word(p, "name", "VPN instance name")
    rd = need(p, "rd", "Route distinguisher")
    if not re.match(r"^[\d.]+:\d+$", rd):
        raise ActionError("RD must look like 65000:100 or 1.1.1.1:100.")
    lines = [f"ip vpn-instance {name}", "ipv4-family", f"route-distinguisher {rd}"]
    for key, kw in (("import_rt", "import-extcommunity"), ("export_rt", "export-extcommunity")):
        v = s(p, key)
        if v:
            if not re.match(r"^[\d.]+:\d+(\s+[\d.]+:\d+)*$", v):
                raise ActionError("Route targets must look like 65000:100.")
            lines.append(f"vpn-target {v} {kw}")
    return lines


@action("vpn.delete")
def vpn_delete(p):
    return [f"undo ip vpn-instance {word(p, 'name', 'VPN instance name')}"]


# ---- BGP ---------------------------------------------------------------------
def _bgp_head(p):
    return f"bgp {asn(p, 'local_as', 'Local AS')}"


@action("bgp.peer_add")
def bgp_peer_add(p):
    peer = ip_addr(p, "peer", "Peer address")
    lines = [_bgp_head(p), f"peer {peer} as-number {asn(p, 'remote_as', 'Remote AS')}"]
    if s(p, "description"):
        lines.append(f"peer {peer} description {text_line(p, 'description', 80)}")
    if s(p, "connect_interface"):
        lines.append(f"peer {peer} connect-interface {iface(p, 'connect_interface')}")
    if s(p, "ebgp_multihop"):
        lines.append(f"peer {peer} ebgp-max-hop {integer(p, 'ebgp_multihop', 1, 255, 'EBGP multihop')}")
    if s(p, "password"):
        pw = need(p, "password")
        if " " in pw or len(pw) > 255:
            raise ActionError("Password must not contain spaces.")
        lines.append(f"peer {peer} password cipher {pw}")
    lines += ["ipv4-family unicast", f"peer {peer} enable"]
    if s(p, "route_policy_in"):
        lines.append(f"peer {peer} route-policy {word(p, 'route_policy_in')} import")
    if s(p, "route_policy_out"):
        lines.append(f"peer {peer} route-policy {word(p, 'route_policy_out')} export")
    if s(p, "route_limit"):
        lines.append(f"peer {peer} route-limit {integer(p, 'route_limit', 1, 4294967295, 'Route limit')}")
    if p.get("next_hop_local") in (True, "true", "1"):
        lines.append(f"peer {peer} next-hop-local")
    return lines


@action("bgp.peer_remove")
def bgp_peer_remove(p):
    return [_bgp_head(p), f"undo peer {ip_addr(p, 'peer', 'Peer address')}"]


@action("bgp.peer_enable")
def bgp_peer_enable(p):
    peer = ip_addr(p, "peer", "Peer address")
    on = p.get("enabled") in (True, "true", "1")
    return [_bgp_head(p), f"undo peer {peer} ignore" if on else f"peer {peer} ignore"]


@action("bgp.network_add")
def bgp_network_add(p):
    net, plen = prefix(p)
    return [_bgp_head(p), "ipv4-family unicast", f"network {net} {plen}"]


@action("bgp.network_remove")
def bgp_network_remove(p):
    net, plen = prefix(p)
    return [_bgp_head(p), "ipv4-family unicast", f"undo network {net} {plen}"]


@action("bgp.reset", mode="exec")
def bgp_reset(p):
    peer = ip_addr(p, "peer", "Peer address")
    soft = s(p, "soft")
    if soft in ("import", "export"):
        return [f"refresh bgp {peer} {soft}"]
    return [f"reset bgp {peer}"]


# ---- OSPF --------------------------------------------------------------------
@action("ospf.network_add")
def ospf_network_add(p):
    proc = integer(p, "process", 1, 65535, "Process")
    area = need(p, "area", "Area")
    if not re.match(r"^(\d+|\d+\.\d+\.\d+\.\d+)$", area):
        raise ActionError("Area must be a number or dotted format.")
    net = ipaddress.ip_network(need(p, "prefix", "Network"), strict=False)
    return [f"ospf {proc}", f"area {area}", f"network {net.network_address} {net.hostmask}"]


@action("ospf.network_remove")
def ospf_network_remove(p):
    proc = integer(p, "process", 1, 65535, "Process")
    area = need(p, "area", "Area")
    net = ipaddress.ip_network(need(p, "prefix", "Network"), strict=False)
    return [f"ospf {proc}", f"area {area}", f"undo network {net.network_address} {net.hostmask}"]


@action("ospf.interface_cost")
def ospf_interface_cost(p):
    return [f"interface {iface(p)}", f"ospf cost {integer(p, 'cost', 1, 65535, 'Cost')}"]


@action("ospf.reset", mode="exec")
def ospf_reset(p):
    return [f"reset ospf {integer(p, 'process', 1, 65535, 'Process')} process"]


# ---- system ------------------------------------------------------------------
@action("system.sysname")
def system_sysname(p):
    return [f"sysname {word(p, 'name', 'Name')}"]


@action("system.ntp_add")
def system_ntp_add(p):
    return [f"ntp-service unicast-server {ip_addr(p, 'server', 'NTP server')}"]


@action("system.ntp_remove")
def system_ntp_remove(p):
    return [f"undo ntp-service unicast-server {ip_addr(p, 'server', 'NTP server')}"]


@action("system.snmp_community")
def system_snmp_community(p):
    name = need(p, "community", "Community")
    if " " in name:
        raise ActionError("Community must not contain spaces.")
    access = s(p, "access") or "read"
    if access not in ("read", "write"):
        raise ActionError("Access must be read or write.")
    return ["snmp-agent", "snmp-agent sys-info version v2c v3", f"snmp-agent community {access} cipher {name}"]


@action("system.syslog_host")
def system_syslog_host(p):
    return [f"info-center loghost {ip_addr(p, 'host', 'Syslog server')}"]


@action("user.add")
def user_add(p):
    name = word(p, "name", "Username")
    pw = need(p, "password", "Password")
    if " " in pw or len(pw) < 8:
        raise ActionError("Password must be at least 8 characters with no spaces.")
    level = integer(p, "level", 0, 3, "Level", required=False, default=3)
    return ["aaa", f"local-user {name} password irreversible-cipher {pw}", f"local-user {name} level {level}",
            f"local-user {name} service-type ssh", "quit", f"ssh user {name}",
            f"ssh user {name} authentication-type password", f"ssh user {name} service-type stelnet"]


@action("user.remove")
def user_remove(p):
    name = word(p, "name", "Username")
    return ["aaa", f"undo local-user {name}", "quit", f"undo ssh user {name}"]


@action("user.password")
def user_password(p):
    name = word(p, "name", "Username")
    pw = need(p, "password", "Password")
    if " " in pw or len(pw) < 8:
        raise ActionError("Password must be at least 8 characters with no spaces.")
    return ["aaa", f"local-user {name} password irreversible-cipher {pw}"]


SECRET_KEYS = ("password", "community")


def build(name, params):
    if name not in ACTIONS:
        raise ActionError(f"Unknown action: {name}")
    fn, mode = ACTIONS[name]
    lines = fn(params or {})
    secrets_ = [str(params[k]) for k in SECRET_KEYS if params.get(k)]
    masked = []
    for line in lines:
        for sec in secrets_:
            line = line.replace(sec, "********")
        masked.append(line)
    return {"lines": lines, "preview": masked, "mode": mode}


@action("custom.lines")
def custom_lines(p):
    """Free-form configuration lines (used by network plans)."""
    lines = [l.rstrip() for l in s(p, "lines").splitlines() if l.strip() and not l.strip().startswith("#")]
    if any(any(ord(ch) < 32 and ch != "\t" or ord(ch) == 127 for ch in l) for l in lines):
        raise ActionError("Configuration lines may not contain control characters.")
    if not lines:
        raise ActionError("Enter at least one configuration line.")
    if any(l.strip().lower() in ("return", "save", "reboot") or l.strip().lower().startswith(("reboot", "reset saved")) for l in lines):
        raise ActionError("return, save, reboot and reset saved-configuration are not allowed in a plan.")
    return lines
