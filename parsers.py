"""Parsers that turn Huawei VRP8 (NetEngine 8000) CLI output into Python data.

Every parser is forgiving: lines it does not understand are skipped, so a
format difference on another software release yields an empty table rather
than an error. The GUI always offers the raw output next to parsed tables.
"""
import re

IPV4 = r"\d+\.\d+\.\d+\.\d+"


def first_int(pattern, text, flags=0):
    m = re.search(pattern, text, flags)
    return int(m.group(1)) if m else None


def first_str(pattern, text, flags=0):
    m = re.search(pattern, text, flags)
    return m.group(1).strip() if m else ""


# ---- system ------------------------------------------------------------------
def parse_version(text):
    model = re.search(r"HUAWEI\s+(\S+(?:\s+M\d+\w*)?)\s+uptime", text)
    ver = re.search(r"Version\s+([\d.]+)\s*\(([^)]+)\)", text)
    return {
        "model": model.group(1) if model else "NetEngine 8000",
        "uptime": first_str(r"uptime is (.+)", text),
        "vrp_version": ver.group(1) if ver else "",
        "software": ver.group(2) if ver else "",
        "patch": first_str(r"Patch Version\s*:\s*(\S+)", text),
    }


def parse_cpu(text):
    return (first_int(r"^\s*CPU Usage\s*:\s*(\d+)%", text, re.M)
            or first_int(r"CPU Using Percentage\s*:\s*(\d+)%", text)
            or first_int(r"five seconds:\s*(\d+)%", text))


def parse_memory(text):
    return {
        "percent": first_int(r"Memory Using Percentage(?: Is)?\s*:\s*(\d+)%", text),
        "total_kb": first_int(r"Total Memory Is\s*:\s*(\d+)", text),
        "used_kb": first_int(r"Total Memory Used Is\s*:\s*(\d+)", text),
    }


def parse_temperature(text):
    """`display temperature` -> [{slot, board, sensor, temp}]

    Two layouts are understood: a table with the slot in the first column, and the
    NE8000 M8 layout with a "Base-Board, Unit:C, Slot 9" heading per slot and rows of
    PCB  I2C Addr Chl Status Minor Major Fatal FanTMin FanTMax Temp(C)."""
    rows, slot = [], ""
    for line in text.splitlines():
        h = re.search(r"Unit:C,\s*Slot\s*(\d+)", line)
        if h:
            slot = h.group(1)
            continue
        m = re.match(r"^\s*(\d+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)\s*$", line)
        if m:
            rows.append({"slot": m.group(1), "board": m.group(2), "sensor": m.group(3), "status": m.group(4),
                         "temp": int(m.group(8)), "minor": int(m.group(6)), "major": int(m.group(7))})
            continue
        m = re.match(r"^\s*([A-Za-z]\S*)\s+(\d+)\s+(\d+)\s+(\d+)\s+([A-Za-z]+)\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)\s*$", line)
        if m:
            rows.append({"slot": slot, "board": m.group(1), "sensor": f"{m.group(2)}/{m.group(3)}/{m.group(4)}",
                         "status": m.group(5), "temp": int(m.group(11)), "minor": int(m.group(6)), "major": int(m.group(7))})
    return rows


def parse_device(text):
    rows = []
    for line in text.splitlines():
        m = re.match(r"^\s*(\d+)\s+(\S+)\s+(Present|Absent)\s+(\S+)\s+(\S+)\s+(\S+)", line)
        if m:
            rows.append({"slot": m.group(1), "type": m.group(2), "online": m.group(3),
                         "register": m.group(4), "status": m.group(5), "role": m.group(6)})
    return rows


def parse_simple_status(text):
    """Power / fan tables: rows that start with a slot id and contain a status word."""
    rows = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 3 and re.match(r"^(PWR|FAN|\d+)", parts[0]):
            rows.append(parts)
    return rows


def parse_local_users(text):
    rows = []
    for line in text.splitlines():
        m = re.match(r"^\s*(\S+)\s+(Active|Block|Blocked|Inactive)\s+(\S+)\s+(\S+)", line)
        if m and m.group(1) != "User":
            rows.append({"name": m.group(1), "state": m.group(2), "service": m.group(3), "level": m.group(4)})
    return rows


ALARM_LEVELS = {"1": "Critical", "2": "Major", "3": "Minor", "4": "Warning"}


def _parse_alarm_columns(text):
    """NE8000 M8 layout: Sequence AlarmId Level Date Time Description, with the time and
    the description wrapped over several lines in fixed-width columns."""
    rows, cur, dcol, tcol = [], None, 39, 28
    for line in text.splitlines():
        if line.startswith("Sequence") and "Description" in line:
            dcol, tcol = line.index("Description"), line.index("Date") if "Date" in line else 28
            continue
        m = re.match(r"^(\d+)\s+(0x[0-9A-Fa-f]+)\s+(\d)\s+(\d{4}-\d\d-\d\d)", line)
        if m:
            cur = {"seq": m.group(1), "id": m.group(2), "name": m.group(2), "level": ALARM_LEVELS.get(m.group(3), m.group(3)),
                   "state": "Active", "time": m.group(4), "description": line[dcol:].rstrip()}
            rows.append(cur)
        elif cur and line.strip() and not line.startswith("-") and len(line) > tcol and not line[:tcol].strip():
            t = line[tcol:dcol].strip()
            if t:
                cur["time"] += (" " if cur["time"].count(" ") == 0 else "") + t
            cur["description"] += line[dcol:].rstrip()
    for r in rows:
        r["description"] = r["description"].strip()
    return rows


def parse_alarms(text):
    if re.search(r"^Sequence\s+AlarmId", text, re.M):
        return _parse_alarm_columns(text)
    rows, cur = [], None
    for line in text.splitlines():
        m = re.match(r"^\s*(\d+)/(\w+)/([^/]+)/([^/]*)/(\S+?)/(\S+?)/(\w+)/(\w+)/?(.*)$", line)
        if m:
            cur = {"seq": m.group(1), "time": m.group(3), "id": m.group(5), "name": m.group(6),
                   "level": m.group(7), "state": m.group(8), "description": m.group(9).strip()}
            rows.append(cur)
        elif cur and line.startswith("    "):
            cur["description"] = (cur["description"] + " " + line.strip()).strip()
    return rows


def parse_logbuffer(text):
    rows = []
    for line in text.splitlines():
        m = re.match(r"^(\w{3}\s+\d+\s+\d{4}\s+[\d:.]+\S*|\d{4}-\d\d-\d\d\s+[\d:.]+\S*)\s+(\S+)\s+(%%\S+|\S+)\s*(.*)$", line)
        if m:
            tag, msg = m.group(3), m.group(4)
            if tag.startswith("%%") and ":" in tag:
                tag, rest = tag.split(":", 1)
                msg = (rest + " " + msg).strip()
                msg = re.sub(r"^CID=[^;]*;\s*", "", msg)
            sev = first_str(r"/(\d)/", tag)
            rows.append({"time": m.group(1), "host": m.group(2), "tag": tag, "severity": sev, "message": msg})
    return rows


# ---- interfaces --------------------------------------------------------------
IF_BRIEF_RE = re.compile(
    r"^(?P<name>\S+)\s+(?P<phy>\S+)\s+(?P<proto>\S+)\s+(?P<inuti>[\d.]+%|--)\s+(?P<oututi>[\d.]+%|--)\s+"
    r"(?P<inerr>\d+)\s+(?P<outerr>\d+)"
)


def _pct(v):
    try:
        return float(v.rstrip("%"))
    except ValueError:
        return 0.0


def parse_interface_brief(text):
    rows = []
    for line in text.splitlines():
        m = IF_BRIEF_RE.match(line.strip())
        if m and m.group("name") != "Interface":
            d = m.groupdict()
            rows.append({
                "name": d["name"], "phy": d["phy"], "protocol": d["proto"],
                "in_util": _pct(d["inuti"]), "out_util": _pct(d["oututi"]),
                "in_errors": int(d["inerr"]), "out_errors": int(d["outerr"]),
                "admin_down": d["phy"].startswith("*"),
                "type": interface_type(d["name"]),
                "parent": d["name"].split(".")[0] if "." in d["name"] else "",
            })
    return rows


def interface_type(name):
    n = name.lower()
    if "." in n:
        return "sub-interface"
    for prefix, t in (("vlanif", "vlanif"), ("loopback", "loopback"), ("null", "null"), ("eth-trunk", "eth-trunk"),
                      ("tunnel", "tunnel"), ("virtual-template", "virtual"), ("gigabitethernet0/0/0", "management")):
        if n.startswith(prefix):
            return t
    return "ethernet"


def parse_ip_brief(text):
    out = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[0] != "Interface" and (re.match(IPV4 + "/", parts[1]) or parts[1] == "unassigned"):
            out[parts[0]] = {"ip": parts[1] if parts[1] != "unassigned" else "",
                             "vpn": parts[4] if len(parts) > 4 and parts[4] != "--" else ""}
    return out


def parse_descriptions(text):
    out = {}
    for line in text.splitlines():
        m = re.match(r"^(\S+)\s+(\*?\^?\S*down|up)\s+(\S+)\s*(.*)$", line.strip())
        if m and m.group(1) not in ("Interface",):
            out[m.group(1)] = m.group(4).strip()
    return out


def parse_interface_detail(text):
    d = {
        "state": first_str(r"current state\s*:\s*(.+)", text, re.I),
        "protocol": first_str(r"Line protocol current state\s*:\s*(\S+)", text),
        "description": first_str(r"^\s*Description\s*:\s*(.*)$", text, re.M),
        "mtu": first_int(r"Maximum Transmit Unit is (\d+)", text),
        "mac": first_str(r"Hardware address is (\S+)", text),
        "ip": first_str(r"Internet Address is (\S+)", text),
        "speed": first_str(r"(?:Current BW|Port BW|Speed)\s*:\s*([\w.]+)", text),
        "duplex": first_str(r"Duplex\s*:\s*(\w+)", text),
        "last_up": first_str(r"Last physical up time\s*:\s*(.+)", text),
        "last_down": first_str(r"Last physical down time\s*:\s*(.+)", text),
        "in_rate_bps": first_int(r"input rate:?\s*(\d+) bits/sec", text),
        "out_rate_bps": first_int(r"output rate:?\s*(\d+) bits/sec", text),
        "in_pps": first_int(r"input rate:?\s*\d+ bits/sec,\s*(\d+) packets/sec", text),
        "out_pps": first_int(r"output rate:?\s*\d+ bits/sec,\s*(\d+) packets/sec", text),
        "vlan": first_str(r"(?:Encapsulation is 802\.1Q.*?VLAN ID|vlan-type dot1q)\s*:?\s*(\d+)", text, re.I),
    }
    for direction in ("Input", "Output"):
        key = direction.lower()
        d[key + "_packets"] = d[key + "_bytes"] = None
        m = re.search(direction + r":\s*(\d+) packets,\s*(\d+) bytes", text)
        if m:
            d[key + "_packets"], d[key + "_bytes"] = int(m.group(1)), int(m.group(2))
        else:
            m = re.search(direction + r":\s*(\d+) bytes,\s*(\d+) packets", text)
            if m:
                d[key + "_bytes"], d[key + "_packets"] = int(m.group(1)), int(m.group(2))
    d["crc_errors"] = first_int(r"CRC:\s*(\d+)", text)
    d["in_discards"] = first_int(r"Input.*?Discard:\s*(\d+)", text, re.S)
    return d


def vlan_from_config(lines):
    """The VLAN of a sub-interface from its config lines: "3099" for dot1q, "2046/7" for QinQ (pe-vid/ce-vid)."""
    for l in lines:
        m = re.match(r"^(?:vlan-type dot1q|dot1q termination vid|user-vlan)\s+(\d+)", l)
        if m:
            return m.group(1)
        m = re.match(r"^qinq termination pe-vid\s+(\d+)\s+ce-vid\s+(\d+(?:\s+to\s+\d+)?)", l)
        if m:
            return m.group(1) + "/" + m.group(2).replace(" to ", "-")
    return ""


def parse_config_blocks(text, keyword="interface"):
    """Split `display current-configuration` style text into {header: [lines]} for blocks starting with keyword."""
    blocks, cur = {}, None
    for line in text.splitlines():
        if line.startswith(keyword + " "):
            cur = line[len(keyword) + 1:].strip()
            blocks[cur] = []
        elif line.startswith("#") or (line and not line.startswith(" ")):
            cur = None
        elif cur is not None and line.strip():
            blocks[cur].append(line.strip())
    return blocks


# ---- VLAN --------------------------------------------------------------------
def parse_vlans(text):
    """`display vlan` -> [{id, status, ports, description}] (VRP VLAN table)."""
    rows, by_id = [], {}
    section = "main"
    for line in text.splitlines():
        if re.match(r"^\s*VID\s+Status\s+Property", line):
            section = "props"
            continue
        if re.match(r"^\s*VID\s+Type\s+Ports", line):
            section = "ports"
            continue
        m = re.match(r"^\s*(\d+)\s+(.*)$", line)
        if not m:
            if section == "ports" and rows and line.startswith(" " * 6) and line.strip():
                rows[-1]["ports"] += " " + line.strip()
            continue
        vid, rest = int(m.group(1)), m.group(2)
        if section == "ports":
            parts = rest.split(None, 1)
            row = {"id": vid, "type": parts[0] if parts else "", "ports": parts[1] if len(parts) > 1 else "",
                   "status": "", "description": ""}
            rows.append(row)
            by_id[vid] = row
        elif section == "props" and vid in by_id:
            parts = rest.split()
            by_id[vid]["status"] = parts[0] if parts else ""
            by_id[vid]["description"] = " ".join(parts[4:])
    return rows


# ---- IP ----------------------------------------------------------------------
ROUTE_RE = re.compile(
    r"^(?:(?P<dest>[\d.]+/\d+)\s+)?(?P<proto>[A-Za-z-]+)\s+(?P<pre>\d+)\s+(?P<cost>\d+)\s+"
    r"(?P<flags>[A-Za-z]+)\s+(?P<nh>\S+)\s+(?P<iface>\S+)"
)


def parse_routing_table(text):
    rows, last_dest = [], ""
    for line in text.splitlines():
        m = ROUTE_RE.match(line.strip())
        if not m:
            continue
        d = m.groupdict()
        dest = d["dest"] or last_dest
        last_dest = dest
        rows.append({"destination": dest, "protocol": d["proto"], "preference": int(d["pre"]),
                     "cost": int(d["cost"]), "flags": d["flags"], "next_hop": d["nh"], "interface": d["iface"]})
    return rows


def parse_route_stats(text):
    """`display ip routing-table statistics` -> {"total": N, "protocols": {"BGP": N, ...}} (None if not understood)."""
    total = first_int(r"Summary Prefixes\s*:\s*(\d+)", text)
    if total is None:
        return None
    protos = {}
    for line in text.splitlines():
        m = re.match(r"^\s*([A-Z][A-Z0-9-]*)\s+(\d+)\s+(\d+)\s+\d+", line)
        if m and m.group(1) not in ("TOTAL", "PROTO"):
            protos[m.group(1)] = int(m.group(2))
    return {"total": total, "protocols": protos}


def parse_static_routes(config_text):
    rows = []
    for line in config_text.splitlines():
        m = re.match(r"^ip route-static\s+(?:vpn-instance\s+(\S+)\s+)?(" + IPV4 + r")\s+(\S+)\s+(.*)$", line.strip())
        if m:
            body = re.split(r"\s+(?:preference|tag|description|bfd|track|permanent|no-advertise)\b", " " + m.group(4))[0].split()
            nh = next((t for t in body if re.match(IPV4 + "$", t)), "")
            iface = next((t for t in body if re.match(r"^[A-Za-z]", t)), "")
            rows.append({"vpn": m.group(1) or "", "prefix": m.group(2), "mask": m.group(3), "next_hop": nh,
                         "interface": iface, "preference": first_str(r"preference (\d+)", m.group(4)) or "60",
                         "description": first_str(r"description (.+)$", m.group(4)), "line": line.strip()})
    return rows


ARP_RE = re.compile(r"^(" + IPV4 + r")\s+([0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}|Incomplete)\s+(\d+\s+)?"
                    r"([A-Z]+(?:-\d*)?(?:\s+-(?=\s))?)\s+(\S+)(?:\s+(\S+))?\s*$")


def parse_arp(text):
    rows = []
    for line in text.splitlines():
        m = ARP_RE.match(line.strip())
        if m:
            rows.append({"ip": m.group(1), "mac": m.group(2), "expire": (m.group(3) or "").strip(),
                         "type": m.group(4).split()[0], "interface": m.group(5), "vpn": m.group(6) or "", "vlan": ""})
            continue
        v = re.match(r"^\s{20,}(\d+|-)/(\d+|-)(?=\s|$)", line)
        if v and rows and not rows[-1]["vlan"]:
            rows[-1]["vlan"] = v.group(1) + ("" if v.group(2) == "-" else "/" + v.group(2))
    return rows


def parse_lldp(text):
    rows = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 4 or not re.match(r"^[A-Za-z0-9][\w-]*\d(/\d+)*(\.\d+)?$", parts[0]) or parts[0].lower().startswith("local"):
            continue
        if parts[1].isdigit():
            rows.append({"local": parts[0], "exptime": parts[1], "neighbor_port": parts[2], "neighbor": " ".join(parts[3:])})
        elif parts[-1].isdigit():
            rows.append({"local": parts[0], "exptime": parts[-1], "neighbor_port": parts[-2], "neighbor": " ".join(parts[1:-2])})
    return rows


def parse_vpn_instances(text):
    rows = []
    for line in text.splitlines():
        m = re.match(r"^\s*(\S+)\s+(\d+:\d+|<not set>|--)\s+(IPv4|IPv6|IPv4\s*&?\s*IPv6|\S+)\s*$", line)
        if m and m.group(1) not in ("VPN-Instance", "Name"):
            rows.append({"name": m.group(1), "rd": m.group(2), "family": m.group(3)})
    return rows


# ---- routing protocols -------------------------------------------------------
BGP_RE = re.compile(
    r"^(?P<peer>[\d.:a-fA-F]+)\s+(?P<v>\d)\s+(?P<as>[\d.]+)\s+(?P<rcv>\d+)\s+(?P<sent>\d+)\s+(?P<q>\d+)\s+"
    r"(?P<updown>\S+)\s+(?P<state>\S+)\s+(?P<pref>\d+)"
)


def parse_bgp_summary(text):
    return {"router_id": first_str(r"BGP local router ID\s*:\s*(\S+)", text),
            "local_as": first_str(r"Local AS number\s*:\s*(\S+)", text),
            "total": first_int(r"Total number of peers\s*:\s*(\d+)", text),
            "established": first_int(r"Peers in established state\s*:\s*(\d+)", text)}


def parse_bgp_peers(text):
    rows = []
    for line in text.splitlines():
        m = BGP_RE.match(line.strip())
        if m:
            d = m.groupdict()
            rows.append({"peer": d["peer"], "as": d["as"], "msg_rcvd": int(d["rcv"]), "msg_sent": int(d["sent"]),
                         "out_q": int(d["q"]), "up_down": d["updown"], "state": d["state"], "prefixes": int(d["pref"])})
    return rows


def parse_bgp_routes(text):
    rows = []
    for line in text.splitlines():
        m = re.match(r"^\s*([*>dhis ]{1,6}?)\s*(" + IPV4 + r"/\d+)\s+(" + IPV4 + r")\s+(\d*)\s*(\d*)\s*(\d*)\s*(.*)$", line)
        if m:
            rows.append({"status": m.group(1).strip(), "network": m.group(2), "next_hop": m.group(3),
                         "med": m.group(4), "local_pref": m.group(5), "pref_val": m.group(6),
                         "path": m.group(7).strip()})
    return rows


def parse_ospf_peers(text):
    rows, process = [], ""
    for line in text.splitlines():
        s = line.strip()
        pm = re.search(r"OSPF Process (\d+)", s)
        if pm:
            process = pm.group(1)
            continue
        m = re.match(r"^(" + IPV4 + r")\s+(\S+)\s+(" + IPV4 + r")\s+(\S+)", s)
        if m:
            rows.append({"process": process, "area": m.group(1), "interface": m.group(2),
                         "neighbor": m.group(3), "state": m.group(4)})
    return rows


def parse_ospf_interfaces(text):
    rows, area = [], ""
    for line in text.splitlines():
        am = re.search(r"Area:\s*(" + IPV4 + ")", line)
        if am:
            area = am.group(1)
            continue
        m = re.match(r"^\s*(\S+)\s+(" + IPV4 + r")\s+(\S+)\s+(\S+)\s+(\d+)\s+(\d+)\s+(" + IPV4 + r")\s+(" + IPV4 + r")", line)
        if m:
            rows.append({"area": area, "interface": m.group(1), "ip": m.group(2), "type": m.group(3), "state": m.group(4),
                         "cost": int(m.group(5)), "priority": int(m.group(6)), "dr": m.group(7), "bdr": m.group(8)})
    return rows


def parse_ldp_sessions(text):
    rows = []
    for line in text.splitlines():
        m = re.match(r"^\s*(" + IPV4 + r":\d+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)", line)
        if m:
            rows.append({"peer": m.group(1), "state": m.group(2), "lam": m.group(3), "role": m.group(4),
                         "age": m.group(5), "keepalive": m.group(6)})
    return rows


# ---- QoS ---------------------------------------------------------------------
def parse_qos_from_config(blocks):
    """From interface config blocks, pull out rate limits / shaping / traffic-policy bindings."""
    rows = []
    for iface, lines in blocks.items():
        for l in lines:
            m = re.match(r"^qos car cir (\d+)(?: pir (\d+))?.*?\b(inbound|outbound)\b", l)
            if m:
                rows.append({"interface": iface, "kind": "rate-limit", "direction": m.group(3),
                             "cir_kbps": int(m.group(1)), "pir_kbps": int(m.group(2)) if m.group(2) else None, "line": l})
            m = re.match(r"^port shaping (\d+)", l)
            if m:
                rows.append({"interface": iface, "kind": "shaping", "direction": "outbound",
                             "cir_kbps": int(m.group(1)) * 1000, "pir_kbps": None, "line": l})
            m = re.match(r"^qos-profile (\S+) (inbound|outbound)\b", l)
            if m:
                rows.append({"interface": iface, "kind": "profile", "direction": m.group(2), "profile": m.group(1),
                             "cir_kbps": None, "pir_kbps": None, "line": l})
            m = re.match(r"^traffic-policy (\S+) (inbound|outbound)", l)
            if m:
                rows.append({"interface": iface, "kind": "policy", "direction": m.group(2), "policy": m.group(1),
                             "cir_kbps": None, "pir_kbps": None, "line": l})
    return rows


def parse_qos_profiles(text):
    """`display current-configuration configuration qos-profile` -> [{name, cir_kbps, pir_kbps, cbs, pbs, lines}]."""
    rows, cur = [], None
    for line in text.splitlines():
        m = re.match(r"^qos-profile (\S+)\s*$", line)
        if m:
            cur = {"name": m.group(1), "cir_kbps": None, "pir_kbps": None, "cbs": None, "pbs": None, "lines": []}
            rows.append(cur)
            continue
        if cur is None:
            continue
        if not line.startswith(" "):
            cur = None
            continue
        t = line.strip()
        cur["lines"].append(t)
        m = re.match(r"^car cir (\d+)(?: pir (\d+))?(?: cbs (\d+))?(?: pbs (\d+))?", t)
        if m and cur["cir_kbps"] is None:
            cur["cir_kbps"], cur["pir_kbps"] = int(m.group(1)), int(m.group(2)) if m.group(2) else None
            cur["cbs"], cur["pbs"] = int(m.group(3)) if m.group(3) else None, int(m.group(4)) if m.group(4) else None
    return rows


def parse_port_queue(text):
    """`display port-queue statistics interface X outbound` -> per-queue pass/discard counters and rates."""
    rows, cur, label = [], None, ""
    for line in text.splitlines():
        t = line.strip()
        m = re.match(r"^\[?(be|af1|af2|af3|af4|ef|cs6|cs7)\]?$", t, re.I)
        if m:
            cur = {"queue": m.group(1).lower(), "pass_packets": 0, "pass_bytes": 0, "discard_packets": 0,
                   "discard_bytes": 0, "pass_bps": 0, "discard_bps": 0}
            rows.append(cur)
            continue
        if not cur:
            continue
        low = t.lower()
        if low.endswith(":"):
            label = low
            continue
        pk = re.match(r"^(\d+)\s*packets,\s*(\d+)\s*bytes", t)
        rt = re.match(r"^(\d+)\s*pps,\s*(\d+)\s*bps", t)
        if pk and "total pass" in label:
            cur["pass_packets"], cur["pass_bytes"] = int(pk.group(1)), int(pk.group(2))
        elif pk and "total discard" in label:
            cur["discard_packets"], cur["discard_bytes"] = int(pk.group(1)), int(pk.group(2))
        elif rt and "pass rate" in label:
            cur["pass_bps"] = int(rt.group(2))
        elif rt and "discard rate" in label:
            cur["discard_bps"] = int(rt.group(2))
    return rows
