"""Network plans from the live network.

discover(): draws the network the connected router sees: BGP upstreams and peers (grouped by
AS), the ports they use, LLDP neighbours, and every named customer/reseller sub-interface with
its VLAN, address, QoS profile and Splynx customer.

design(): reviews that picture against carrier-grade practice (redundant edge, IGP + MPLS core,
dual-homed aggregation, out-of-band management, BGP safety) and returns a proposed target
design and draft tasks. Nothing is sent to a router: tasks only run if someone executes the plan.
Everything read here is display/SNMP only.
"""
import ipaddress
import re
import time

import parsers as P

COL, ROW, NODE_W = 215, 95, 190


def _try(fn, default, warnings, what):
    try:
        return fn()
    except Exception as exc:     # a view that needs SSH on an SNMP-only session, etc.
        warnings.append(f"{what}: {str(exc)[:120]}")
        return default


def snapshot(r, billing_rows=None):
    """Everything discover() and design() need, read once from the router."""
    w = []
    ov = _try(r.overview, {}, w, "overview")
    s = {"host": r.host, "sysname": ov.get("sysname") or r.host, "model": ov.get("model", ""),
         "alarms": (ov.get("alarms") or {}).get("total", 0), "warnings": w}
    s["interfaces"] = _try(r.interfaces, [], w, "interfaces")
    s["bgp"] = _try(r.bgp, {"peers": [], "local_as": ""}, w, "BGP")
    s["lldp"] = _try(r.neighbors, {"lldp": []}, w, "LLDP")["lldp"]
    q = _try(r.qos, None, w, "QoS")
    s["qos_known"] = q is not None
    q = q or {"rules": []}
    s["qos"] = {}
    for rule in q.get("rules", []):
        if rule.get("kind") == "profile" and rule.get("direction") == "outbound":
            s["qos"][rule["interface"]] = {"profile": rule.get("profile", ""), "kbps": rule.get("pir_kbps") or rule.get("cir_kbps")}
        elif rule.get("kind") == "rate-limit" and rule.get("direction") == "outbound":
            s["qos"].setdefault(rule["interface"], {"profile": "CAR", "kbps": rule.get("cir_kbps")})
    s["isis"] = _try(lambda: r.isis()["configured"], None, w, "IS-IS")
    s["ospf"] = _try(lambda: len(r.ospf()["peers"]), None, w, "OSPF")
    s["ldp"] = _try(lambda: len(r.mpls()["ldp"]), None, w, "MPLS LDP")
    s["billing"] = {x["interface"]: x for x in (billing_rows or []) if x.get("interface")}
    return s


def _net(ip):
    try:
        return ipaddress.ip_interface(ip).network
    except ValueError:
        return None


def _model(snap):
    """Group the live data into upstream ASes, port groups and customers."""
    ifs = snap["interfaces"]
    by_name = {i["name"]: i for i in ifs}
    local_as = str(snap["bgp"].get("local_as") or "")
    nets = [(i["name"], _net(i["ip"])) for i in ifs if i.get("ip")]
    def port_of(peer):
        try:
            a = ipaddress.ip_address(peer)
        except ValueError:
            return ""
        best = None
        for name, n in nets:
            if n and a in n and (best is None or n.prefixlen > best[1].prefixlen):
                best = (name, n)
        return best[0] if best else ""
    upstreams, ibgp = {}, []
    for p in snap["bgp"].get("peers", []):
        port = port_of(p["peer"])
        if p["as"] == local_as:
            ibgp.append({**p, "port": port})
            continue
        u = upstreams.setdefault(p["as"], {"as": p["as"], "peers": [], "ports": set()})
        u["peers"].append({**p, "port": port})
        if port:
            u["ports"].add(port)
    bgp_ports = {p["port"] for u in upstreams.values() for p in u["peers"]} | {p["port"] for p in ibgp}
    lldp = {}
    for n in snap["lldp"]:
        lldp.setdefault(P.isis_key(n["local"]), []).append(n)
    groups, customers, unnamed = {}, [], 0
    for i in ifs:
        if i.get("type") != "sub-interface" or i["name"] in bgp_ports:
            continue
        b = snap["billing"].get(i["name"])
        if not (i.get("description") or b):
            unnamed += 1
            continue
        parent = i.get("parent") or i["name"].split(".")[0]
        groups.setdefault(parent, [])
        q = snap["qos"].get(i["name"]) or {}
        c = {"interface": i["name"], "parent": parent, "label": (b or {}).get("name") or i["description"], "description": i.get("description", ""),
             "vlan": i.get("vlan") or i["name"].split(".")[-1], "ip": i.get("ip", ""), "state": i.get("phy", ""),
             "profile": q.get("profile", ""), "kbps": q.get("kbps"), "splynx_id": str((b or {}).get("id") or ""),
             "splynx_status": (b or {}).get("status", ""), "util": max(i.get("in_util") or 0, i.get("out_util") or 0)}
        groups[parent].append(c)
        customers.append(c)
    # physical ports that matter: carry customers, BGP, or LLDP neighbours
    for i in ifs:
        if i.get("type") in ("ethernet", "eth-trunk") and (P.isis_key(i["name"]) in lldp) and i["name"] not in groups:
            groups[i["name"]] = []
    return {"by_name": by_name, "local_as": local_as, "upstreams": upstreams, "ibgp": ibgp, "groups": groups,
            "customers": customers, "unnamed": unnamed, "lldp": lldp}


def _kind(name, desc=""):
    t = (name + " " + desc).lower()
    if re.search(r"\bsw|switch|qfx|crs|ce\d|s\d{4}", t):
        return "switch"
    if re.search(r"olt|ma56", t):
        return "olt"
    return "router"


def discover(snap):
    m = _model(snap)
    nodes, links = [], []
    def node(nid, typ, label, x, y, **props):
        nodes.append({"id": nid, "type": typ, "label": label[:80], "x": x, "y": y, "props": {k: str(v) for k, v in props.items() if v not in ("", None)}})
    def link(a, b, **props):
        links.append({"id": f"l{len(links) + 1}", "from": a, "to": b, "props": {k: str(v) for k, v in props.items() if v not in ("", None)}})
    # layout: one block per port group, customers in rows of up to 4 under their group
    # groups with customers get a block below the router; ports with only an LLDP neighbour sit beside it
    blocks, side, x = [], [], 40
    for parent, cs in sorted(m["groups"].items(), key=lambda kv: (-len(kv[1]), kv[0])):
        if not cs and len(m["lldp"].get(P.isis_key(parent), [])) == 1:
            side.append(parent)
            continue
        cols = max(1, min(3, len(cs)))
        blocks.append((parent, cs, x, cols))
        x += cols * COL + 40
    width = max(x, 900)
    rx = width / 2 - NODE_W / 2
    node("r", "ne8000", f"{snap['sysname']} ({snap['model']})" if snap["model"] else snap["sysname"], rx, 210,
         mgmt_ip=snap["host"], local_as=m["local_as"], existing="yes")
    ups = sorted(m["upstreams"].values(), key=lambda u: -sum(p["prefixes"] or 0 for p in u["peers"]))
    ibgp_n = 1 if m["ibgp"] else 0
    ucols = max(1, int(width // COL))
    total = len(ups) + ibgp_n
    step = width / max(1, min(total, ucols))
    urow = lambda k: (k % ucols, k // ucols)      # wrap the upstream row when there are many peers
    for k, u in enumerate(ups):
        up = sum(1 for p in u["peers"] if p["state"] == "Established")
        nid = f"u{k}"
        node(nid, "upstream", f"AS{u['as']}", 20 + urow(k)[0] * step, 30 + urow(k)[1] * 80, bgp_as=u["as"], peer_ip=", ".join(p["peer"] for p in u["peers"])[:200],
             location=f"{up}/{len(u['peers'])} sessions up", existing="yes",
             prefixes=sum(p["prefixes"] or 0 for p in u["peers"]))
        ports = sorted(u["ports"])
        link(nid, "r", label=", ".join(ports)[:60] or "multihop", router_port=ports[0] if len(ports) == 1 else "", existing="yes",
             state="up" if up else "down")
    if m["ibgp"]:
        nid = "ibgp"
        node(nid, "router", f"iBGP peers (AS{m['local_as']})", 20 + urow(len(ups))[0] * step, 30 + urow(len(ups))[1] * 80, peer_ip=", ".join(p["peer"] for p in m["ibgp"])[:200],
             location=f"{sum(1 for p in m['ibgp'] if p['state'] == 'Established')}/{len(m['ibgp'])} up", existing="yes")
        link(nid, "r", label="iBGP", existing="yes")
    for k, parent in enumerate(side):
        nb = m["lldp"][P.isis_key(parent)][0]
        pi = m["by_name"].get(parent, {})
        sx = rx + (1 if k % 2 else -1) * (COL + 30) * (k // 2 + 1)
        node(f"s{k}", _kind(nb["neighbor"]), nb["neighbor"], max(0, sx), 210 + ((k // 2) % 2) * 0, location=f"LLDP on {parent} → {nb['neighbor_port']}", existing="yes")
        link("r", f"s{k}", label=parent, router_port=parent, existing="yes", state=pi.get("phy", ""))
    for g, (parent, cs, bx, cols) in enumerate(blocks):
        pi = m["by_name"].get(parent, {})
        nb = m["lldp"].get(P.isis_key(parent), [])
        gx = bx + (cols * COL - 25) / 2 - NODE_W / 2
        gid = f"g{g}"
        if len(nb) == 1:
            node(gid, _kind(nb[0]["neighbor"]), nb[0]["neighbor"], gx, 380, location=f"LLDP on {parent} → {nb[0]['neighbor_port']}", existing="yes")
        else:
            node(gid, "switch", pi.get("description") or parent, gx, 380,
                 location=f"{parent} · {len(cs)} links" + (f" · {len(nb)} LLDP" if nb else ""), existing="yes")
        link("r", gid, label=parent, router_port=parent, existing="yes", state=pi.get("phy", ""),
             notes=f"in {pi.get('in_util', '')}% / out {pi.get('out_util', '')}%" if pi.get("in_util") is not None else "")
        y = 380 + 120
        if len(nb) > 1:     # several neighbours on one port (an exchange or a shared segment): a row under the port
            ncols = max(cols, 1)
            for j, n in enumerate(nb):
                nid = f"n{g}_{j}"
                node(nid, _kind(n["neighbor"]), n["neighbor"], bx + (j % ncols) * COL, y + (j // ncols) * ROW,
                     location=f"LLDP → {n['neighbor_port']}", existing="yes")
                link(gid, nid, label="LLDP", existing="yes")
            y += ((len(nb) - 1) // ncols + 1) * ROW + 25
        for k, c in enumerate(sorted(cs, key=lambda c: c["label"].lower())):
            cid = f"c{g}_{k}"
            node(cid, "reseller", c["label"], bx + (k % cols) * COL, y + (k // cols) * ROW, splynx_id=c["splynx_id"],
                 location=c["profile"] or c["ip"], status=c["splynx_status"], interface=c["interface"], existing="yes")
            link(gid, cid, label=f"VLAN {c['vlan']}" + (f" · {c['profile']}" if c["profile"] else ""), router_port=parent, vlan=c["vlan"],
                 address=c["ip"], state=c["state"], existing="yes")
    extra = ((total - 1) // ucols) * 80 if total else 0
    for n in nodes:
        if n["type"] != "upstream" and n["id"] != "ibgp":
            n["y"] += extra
    summary = (f"Drawn from {snap['sysname']} ({snap['host']}) on {time.strftime('%Y-%m-%d %H:%M')}: "
               f"{len(ups)} upstream/peer AS, {len(m['groups'])} port groups, {len(m['customers'])} customer links"
               + (f", {m['unnamed']} unnamed sub-interfaces left out" if m["unnamed"] else "")
               + (f". Not available on this connection: {'; '.join(snap['warnings'])}" if snap["warnings"] else "") + ".")
    return {"nodes": nodes, "links": links}, summary, m


def _net_from_loopback(ip):
    try:
        a = ipaddress.ip_interface(ip).ip
    except ValueError:
        return ""
    digits = "".join(f"{int(o):03d}" for o in str(a).split("."))
    return f"49.0001.{digits[0:4]}.{digits[4:8]}.{digits[8:12]}.00"


def design(snap):
    """Findings, a proposed carrier-grade target design, and draft tasks."""
    diagram, _, m = discover(snap)
    nodes, links = diagram["nodes"], diagram["links"]
    find, rec, buy, tasks = [], [], [], []
    ups = m["upstreams"]
    up_ports = sorted({p for u in ups.values() for p in u["ports"]})
    phys = lambda n: n.split(".")[0]
    up_phys = sorted({phys(p) for p in up_ports})
    find.append(f"One edge router ({snap['sysname']}) carries every upstream and every customer: a single failure or upgrade takes the whole network down.")
    if len(up_phys) <= 1 and ups:
        find.append(f"Every upstream/peer session ({len(ups)} AS) rides on {up_phys[0] if up_phys else 'one port'}.")
    elif ups:
        find.append(f"Upstream sessions use {len(up_phys)} physical ports ({', '.join(up_phys)}), but all on one chassis.")
    trunks = [i for i in snap["interfaces"] if i["name"].lower().startswith("eth-trunk")]
    if not trunks:
        find.append("No Eth-Trunk (LACP) bundles: each uplink and aggregation link is one port and one optic.")
    if snap["isis"] is False and not snap["ospf"]:
        find.append("No IGP (IS-IS/OSPF) is running, so there is nothing to carry loopbacks, iBGP or MPLS between core nodes.")
    if snap["ldp"] == 0:
        find.append("No MPLS LDP sessions: no L2VPN/L3VPN services or fast reroute are possible yet.")
    if snap["alarms"]:
        find.append(f"{snap['alarms']} active alarm(s) on the router; check Logs & alarms (optics on uplinks especially).")
    noqos = [c for c in m["customers"] if not c["profile"]]
    if noqos and snap.get("qos_known", True):
        find.append(f"{len(noqos)} customer link(s) have no QoS profile: " + ", ".join(c["label"] for c in noqos[:8]) + ("…" if len(noqos) > 8 else "") + ".")
    if m["unnamed"]:
        find.append(f"{m['unnamed']} sub-interface(s) have no description and no Splynx match; name or remove them.")
    hot = [i for i in snap["interfaces"] if i.get("type") in ("ethernet", "eth-trunk") and max(i.get("in_util") or 0, i.get("out_util") or 0) >= 70]
    if hot:
        find.append("Ports above 70% utilisation: " + ", ".join(f"{i['name']} ({max(i['in_util'] or 0, i['out_util'] or 0):.0f}%)" for i in hot) + ".")

    rec += ["Add a second edge router (BNG-2/PE-2) and split upstreams across both, so each AS has a session on each router.",
            "Join the two routers with an Eth-Trunk of at least 2 x 100G running IS-IS level-2 (wide metrics, BFD) and MPLS LDP; iBGP between loopbacks.",
            "Dual-home aggregation: two aggregation switches in M-LAG (or a stack), each with an Eth-Trunk to both routers; customers stay on per-reseller VLANs.",
            "Out-of-band management: a separate management switch and VRF for MEth/console, with SSH and SNMPv3 allowed only from the NMS.",
            "BGP safety on every eBGP peer: maximum-prefix limits, inbound/outbound route-policies (bogons, own prefixes only), and communities for traffic engineering.",
            "One QoS profile per product (e.g. 100/300/500/1000 Mbit/s) applied to every customer sub-interface, named consistently with Splynx plans.",
            "Operations: NTP and syslog on every device, nightly config backups, and graphs on every saved device (Manager > Routers)."]
    buy += ["1 x second edge router (NE8000 M8 or M4, matching the current software release)",
            "2 x aggregation switches with M-LAG and 100G/25G uplinks (e.g. CloudEngine 6881/8850 or S6730-H)",
            "100G optics or DAC for the router-to-router Eth-Trunk (4 per 2 x 100G bundle) and for dual-homed aggregation",
            "1 x out-of-band management switch (1G, with a separate uplink)"]

    # target drawing: the live picture plus proposed (NEW) devices
    r = next(n for n in nodes if n["id"] == "r")
    for n in nodes:
        if n["y"] > r["y"] + 60:
            n["y"] += 190      # room for the aggregation pair
    side_x = [n["x"] for n in nodes if n["y"] == r["y"] and n["id"] != "r"]
    r2x = max([r["x"]] + side_x) + COL + 40         # the second router goes right of anything beside the first
    r2 = {"id": "r2", "type": "ne8000", "label": "NEW: BNG-2 / PE-2", "x": r2x, "y": r["y"], "props": {"status": "proposed", "location": "second chassis, ideally second site"}}
    agg1 = {"id": "a1", "type": "switch", "label": "NEW: AGG-1", "x": r["x"] - 20, "y": r["y"] + 190, "props": {"status": "proposed", "location": "M-LAG pair"}}
    agg2 = {"id": "a2", "type": "switch", "label": "NEW: AGG-2", "x": r2x + 20, "y": r["y"] + 190, "props": {"status": "proposed", "location": "M-LAG pair"}}
    lx = min([r["x"]] + side_x) - COL - 40
    oob = {"id": "oob", "type": "switch", "label": "NEW: OOB management", "x": lx, "y": r["y"] + 95, "props": {"status": "proposed", "location": "management VRF"}}
    nms = {"id": "nms", "type": "server", "label": "NMS: NE8000 Manager", "x": lx, "y": r["y"] + 190, "props": {"location": "SNMPv3, syslog, NTP, backups"}}
    nodes += [r2, agg1, agg2, oob, nms]
    dx = 20 - min(n["x"] for n in nodes)
    if dx > 0:
        for n in nodes:
            n["x"] += dx
    links += [{"id": "p1", "from": "r", "to": "r2", "props": {"label": "Eth-Trunk 2x100G · IS-IS L2 + LDP", "status": "proposed"}},
              {"id": "p2", "from": "r", "to": "a1", "props": {"label": "Eth-Trunk", "status": "proposed"}},
              {"id": "p3", "from": "r", "to": "a2", "props": {"label": "Eth-Trunk", "status": "proposed"}},
              {"id": "p4", "from": "r2", "to": "a1", "props": {"label": "Eth-Trunk", "status": "proposed"}},
              {"id": "p5", "from": "r2", "to": "a2", "props": {"label": "Eth-Trunk", "status": "proposed"}},
              {"id": "p6", "from": "a1", "to": "a2", "props": {"label": "M-LAG peer-link", "status": "proposed"}},
              {"id": "p7", "from": "oob", "to": "r", "props": {"label": "MEth", "status": "proposed"}},
              {"id": "p8", "from": "oob", "to": "r2", "props": {"label": "MEth", "status": "proposed"}},
              {"id": "p9", "from": "nms", "to": "oob", "props": {"label": "management", "status": "proposed"}}]
    for k, n in enumerate([n for n in nodes if n["type"] == "upstream"]):
        links.append({"id": f"pu{k}", "from": n["id"], "to": "r2", "props": {"label": "second session", "status": "proposed"}})
    # customer groups move behind the aggregation pair
    for l in links:
        if l["from"] == "r" and re.match(r"^g\d+$", l["to"]):
            l["from"] = "a1" if int(l["to"][1:]) % 2 == 0 else "a2"
            l["props"]["label"] = "via AGG pair (was " + l["props"].get("label", "") + ")"
            l["props"]["status"] = "proposed"

    # draft tasks for this router: preparation only, safe to check, nothing executes on its own
    lo = next((i for i in snap["interfaces"] if i["name"].lower() == "loopback0" and i.get("ip")), None)
    net = _net_from_loopback(lo["ip"]) if lo else ""
    if snap["isis"] is False and net:
        tasks.append({"id": "d1", "action": "isis.process_add", "title": "Start IS-IS process 1 (level-2, wide metrics), ready for the second router",
                      "params": {"process": "1", "level": "level-2", "cost_style": "wide", "net": net}, "source": "design"})
        tasks.append({"id": "d2", "action": "isis.interface_enable", "title": "Advertise LoopBack0 in IS-IS", "params": {"interface": "LoopBack0", "process": "1"}, "source": "design"})
    if m["local_as"] and ups:
        lines = []
        for u in sorted(ups.values(), key=lambda u: u["as"]):
            for p in u["peers"]:
                pref = p["prefixes"] or 0
                limit = 1200000 if pref > 500000 else max(100, pref * 2)
                lines.append(f"  peer {p['peer']} route-limit {limit} 80 alert-only")
        tasks.append({"id": "b1", "action": "custom.lines", "title": f"Prefix-limit alarms on {len(lines)} eBGP peer{'s' if len(lines) != 1 else ''} (alert only, never drops a session)",
                      "params": {"lines": "\n".join([f"bgp {m['local_as']}", " ipv4-family unicast"] + lines)}, "source": "design"})
    desc = ("Carrier-grade review of " + snap["sysname"] + " on " + time.strftime("%Y-%m-%d") + ".\n\nFINDINGS\n- " + "\n- ".join(find)
            + "\n\nRECOMMENDED DESIGN\n- " + "\n- ".join(rec) + "\n\nTO BUY\n- " + "\n- ".join(buy)
            + "\n\nNEW devices and links in the drawing are proposals. The draft tasks only prepare this router; nothing runs until someone executes the plan.")
    return {"nodes": nodes, "links": links}, desc, tasks, {"findings": find, "recommendations": rec, "buy": buy}
