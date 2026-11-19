"""IP plan: the address blocks, the segments (pools) they are split into, and the addresses handed out from them.

What is in use comes from the router itself (the interfaces' addresses); the plan (blocks, segments, allocations) is the
app's own data, one per router, in <data>/ipam.json. Nothing here changes the router: an allocation only books the
addresses, and putting one on an interface goes through the usual Review and commit.
"""
import ipaddress
import json
import os
import threading
import time
import uuid

PURPOSES = {"infra": "Infrastructure and loopbacks", "p2p": "Point-to-point links", "upstream": "Upstreams and IXP", "customers": "Customers",
            "mgmt": "Management", "reserved": "Reserved for growth"}
LOCK = threading.Lock()


def net(s):
    return ipaddress.ip_network(str(s).strip(), strict=False)


def used_from(interfaces):
    """[{net, ip, iface, desc, vpn, kind}] for every IPv4 address on the router's interfaces."""
    out = []
    for r in interfaces or []:
        for i, a in enumerate(([r.get("ip")] if r.get("ip") else []) + list(r.get("secondary") or [])):
            try:
                itf = ipaddress.ip_interface(a)
            except ValueError:
                continue
            if itf.version != 4:
                continue
            out.append({"net": str(itf.network), "ip": str(itf.ip), "iface": r.get("name", ""), "desc": r.get("description", "") + (" (secondary)" if i else ""),
                        "vpn": r.get("vpn", ""), "kind": "loopback" if r.get("type") == "loopback" else r.get("type", ""), "up": r.get("protocol", "")})
    return out


def announced(bgp_config):
    """The IPv4 networks the router announces itself (BGP `network` lines): the address space that is the operator's own."""
    out = []
    for lines in (bgp_config or {}).values():
        for l in lines or []:
            p = str(l).split()
            if len(p) >= 3 and p[0] == "network":
                try:
                    n = net(f"{p[1]}/{p[2]}")
                except ValueError:
                    continue
                if n.version == 4 and n.is_global and n.prefixlen <= 24:
                    out.append(n)
    return out


def suggest_blocks(used, own=None):
    """The operator's own public blocks: the networks the router announces that hold its addresses. Without those, the
    smallest prefix (at least a /24) around each group of 3 or more public subnets; a single link subnet on its own is
    an upstream's or an exchange's space, not ours."""
    if own:
        ins = [str(n) for n in own if any(net(u["net"]).subnet_of(n) for u in used if net(u["net"]).version == 4)]
        if ins:     # a block inside another announced one (a /24 half of the /23 announced to a peer) is the same space
            ins = [x for x in set(ins) if not any(net(x) != net(y) and net(x).subnet_of(net(y)) for y in ins)]
            return sorted(ins, key=lambda s: net(s))
    groups = {}
    for u in used:
        n = net(u["net"])
        if not n.is_global or u["kind"] == "loopback":
            continue
        groups.setdefault(net(f"{n.network_address}/22").network_address, []).append(n)
    out = []
    for nets in groups.values():
        if len(nets) < 3:
            continue
        lo, hi = min(int(n.network_address) for n in nets), max(int(n.broadcast_address) for n in nets)
        p = 24
        while p > 8:
            c = net(f"{ipaddress.IPv4Address(lo)}/{p}")
            if int(c.broadcast_address) >= hi:
                break
            p -= 1
        out.append(str(net(f"{ipaddress.IPv4Address(lo)}/{min(p, 24)}")))
    return sorted(set(out), key=lambda s: net(s))


class Plans:
    def __init__(self, data_dir):
        self.path = os.path.join(data_dir, "ipam.json")

    def _all(self):
        try:
            with open(self.path) as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def get(self, key):
        p = self._all().get(key) or {}
        return {"blocks": p.get("blocks", []), "pools": p.get("pools", []), "allocs": p.get("allocs", [])}

    def put(self, key, plan):
        with LOCK:
            d = self._all()
            d[key] = plan
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(d, f, indent=1)
            os.replace(tmp, self.path)
        return plan


def check_plan(plan):
    """Clean and check a plan sent by the page: blocks do not overlap, segments sit inside a block and do not overlap."""
    blocks, pools = [], []
    for b in (plan.get("blocks") or [])[:50]:
        try:
            n = net(b.get("cidr"))
        except ValueError:
            raise ValueError(f"Not a block: {b.get('cidr')}")
        if n.version != 4 or n.prefixlen < 8:
            raise ValueError(f"{n}: an IPv4 block of /8 or smaller")
        if any(n.overlaps(net(x["cidr"])) for x in blocks):
            raise ValueError(f"{n} overlaps another block")
        blocks.append({"cidr": str(n), "name": str(b.get("name") or "")[:60]})
    for p in (plan.get("pools") or [])[:300]:
        try:
            n = net(p.get("cidr"))
        except ValueError:
            raise ValueError(f"Not a segment: {p.get('cidr')}")
        if not any(n.subnet_of(net(b["cidr"])) for b in blocks):
            raise ValueError(f"Segment {n} is not inside one of the blocks")
        o = next((x for x in pools if n.overlaps(net(x["cidr"]))), None)
        if o:
            raise ValueError(f"Segment {n} overlaps {o['cidr']} ({o['name']})")
        pools.append({"id": str(p.get("id") or uuid.uuid4().hex[:8])[:12], "cidr": str(n), "name": str(p.get("name") or "")[:60] or str(n),
                      "purpose": p.get("purpose") if p.get("purpose") in PURPOSES else "customers", "region": str(p.get("region") or "")[:40]})
    allocs = [a for a in (plan.get("allocs") or [])[:2000] if isinstance(a, dict) and a.get("cidr")]
    return {"blocks": blocks, "pools": pools, "allocs": allocs}


def taken(plan, used, skip_pools=False):
    t = [net(u["net"]) for u in used] + [net(a["cidr"]) for a in plan["allocs"]]
    return t if skip_pools else t + [net(p["cidr"]) for p in plan["pools"]]


def first_free(within, plen, busy):
    """The first subnet of length plen inside `within`, aligned, overlapping nothing in busy."""
    within = net(within)
    if plen < within.prefixlen:
        return None
    busy = [b for b in busy if b.overlaps(within)]
    for c in within.subnets(new_prefix=plen):
        if not any(c.overlaps(b) for b in busy):
            return c
    return None


def allocate(plan, used, where, plen, name, note="", by=""):
    """Book the next free /plen in a segment (or a block, outside its segments). Returns the allocation."""
    plen = int(plen)
    if not 16 <= plen <= 32:
        raise ValueError("Ask for a /16 to a /32")
    pool = next((p for p in plan["pools"] if p["id"] == where or p["cidr"] == where), None)
    if pool:
        c = first_free(pool["cidr"], plen, taken(plan, used, skip_pools=True))
    else:
        blk = next((b for b in plan["blocks"] if b["cidr"] == where), None)
        if not blk:
            raise ValueError("Pick a segment or a block")
        c = first_free(blk["cidr"], plen, taken(plan, used))
    if not c:
        raise ValueError(f"No free /{plen} left in {pool['name'] if pool else where}")
    a = {"id": uuid.uuid4().hex[:8], "cidr": str(c), "name": str(name or "")[:80], "note": str(note or "")[:200], "pool": pool["id"] if pool else "",
         "by": by, "at": int(time.time())}
    plan["allocs"].append(a)
    return a


def hosts(n):
    """The addresses to configure: the router's (first usable) and the far end's, for a link."""
    n = net(n)
    if n.prefixlen == 32:
        return [str(n.network_address)]
    if n.prefixlen == 31:
        return [str(n.network_address), str(n.broadcast_address)]
    h = list(n.hosts()) if n.prefixlen >= 24 else [n.network_address + 1, n.network_address + 2]
    return [str(h[0]), str(h[1])] if len(h) > 1 else [str(h[0])]


def layout(block, regions):
    """A suggested split of a block, the usual way: loopbacks first, links as /31, upstreams, management, a pool per
    region, and a quarter kept back for growth. Each piece is aligned (the biggest are placed first)."""
    b = net(block)
    size = b.num_addresses
    if size < 64:
        raise ValueError("A block of at least a /26 is needed for a layout")
    pl = lambda n: max(b.prefixlen, 32 - max(0, (n - 1).bit_length()))
    want = [("Infrastructure and loopbacks", "infra", "", pl(max(16, size // 32))), ("Point-to-point links (/31)", "p2p", "", pl(size // 8)),
            ("Upstreams and IXP", "upstream", "", pl(max(8, size // 64))), ("Management", "mgmt", "", pl(max(8, size // 64))),
            ("Reserved for growth", "reserved", "", pl(size // 4))]
    left = size - sum(2 ** (32 - w[3]) for w in want)
    regions = [r for r in regions if r][:8] or ["Customers"]
    each = 2 ** (max(0, (left // len(regions))).bit_length() - 1) if left >= len(regions) else 0
    while each and each * len(regions) > left:
        each //= 2
    want += [(f"Customers – {r}" if r != "Customers" else "Customers", "customers", r if r != "Customers" else "", pl(each)) for r in regions if each >= 4]
    out = []
    for name, purpose, region, p in sorted(want, key=lambda w: (w[1] == "reserved", w[3])):     # the reserve goes last, at the top of the block
        c = first_free(b, p, [net(x["cidr"]) for x in out])
        if c:
            out.append({"id": uuid.uuid4().hex[:8], "cidr": str(c), "name": name, "purpose": purpose, "region": region})
    return sorted(out, key=lambda x: net(x["cidr"]))


def stats(block, plan, used):
    """Size, in use on the router, booked, free, and the free runs (gaps) of one block."""
    b = net(block)
    ins = [net(u["net"]) for u in used if net(u["net"]).subnet_of(b)]
    booked = [net(a["cidr"]) for a in plan["allocs"] if net(a["cidr"]).subnet_of(b) and not any(net(a["cidr"]).overlaps(x) for x in ins)]
    marks = sorted([(int(n.network_address), int(n.broadcast_address)) for n in ins + booked])
    merged = []
    for lo, hi in marks:
        if merged and lo <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])
    gaps, cur = [], int(b.network_address)
    for lo, hi in merged:
        if lo > cur:
            gaps.append([cur, lo - 1])
        cur = max(cur, hi + 1)
    if cur <= int(b.broadcast_address):
        gaps.append([cur, int(b.broadcast_address)])
    n_used = sum(n.num_addresses for n in ins)
    n_booked = sum(n.num_addresses for n in booked)
    return {"cidr": str(b), "size": b.num_addresses, "used": n_used, "booked": n_booked, "free": b.num_addresses - n_used - n_booked,
            "gaps": [{"from": str(ipaddress.IPv4Address(lo)), "size": hi - lo + 1} for lo, hi in sorted(gaps, key=lambda g: g[0] - g[1])]}


def findings(plan, used):
    """What could be better in the addresses as they are, with advice. Nothing is changed by itself."""
    out = []
    v4 = [(u, net(u["net"])) for u in used]
    p30 = [u for u, n in v4 if n.prefixlen == 30]
    if p30:
        out.append({"severity": "info", "kind": "p2p31", "title": f"{len(p30)} point-to-point links use a /30",
                    "detail": f"A /30 takes 4 addresses for 2 devices. As /31s they would take {len(p30) * 2} addresses instead of {len(p30) * 4}.",
                    "advice": "Give new point-to-point links a /31 (RFC 3021; the NE8000 supports it, check that the far-end device does too). Move existing links only in a planned window, one at a time.",
                    "nets": [u["net"] for u in p30]})
    seen = []
    for u, n in v4:
        for w, m in seen:
            if n.overlaps(m) and (u["vpn"] or "") == (w["vpn"] or ""):
                out.append({"severity": "critical", "kind": "overlap", "title": f"{u['net']} overlaps {w['net']}",
                            "detail": f"{u['iface']} ({u['desc'] or 'no description'}) and {w['iface']} ({w['desc'] or 'no description'}) are in the same routing table.",
                            "advice": "Two interfaces in one routing table must not share addresses: renumber one of them.", "nets": [u["net"], w["net"]]})
        seen.append((u, n))
    for b in plan["blocks"]:
        st = stats(b["cidr"], plan, used)
        small = [g for g in st["gaps"] if g["size"] < 16]
        if len(st["gaps"]) >= 8:
            out.append({"severity": "warning", "kind": "fragmented", "title": f"{b['cidr']} is fragmented: {len(st['gaps'])} free gaps",
                        "detail": f"{st['free']} addresses are free, the largest run is {st['gaps'][0]['size'] if st['gaps'] else 0}; {len(small)} gaps are too small for a /28.",
                        "advice": "Split the block into segments (by purpose and region) and hand out new addresses from them, so it fills in order. Use “Suggest a layout”."})
        if st["free"] < st["size"] * 0.15:
            out.append({"severity": "warning", "kind": "full", "title": f"{b['cidr']} is {round(100 - st['free'] * 100 / st['size'])}% used",
                        "detail": f"Only {st['free']} addresses are free.", "advice": "Ask AFRINIC for more space in time, and reclaim addresses booked but never used."})
    if plan["pools"]:
        outside = [u for u, n in v4 if n.is_global and any(n.subnet_of(net(b["cidr"])) for b in plan["blocks"]) and not any(n.subnet_of(net(p["cidr"])) for p in plan["pools"])]
        if outside:
            out.append({"severity": "info", "kind": "nopool", "title": f"{len(outside)} subnets are in no segment",
                        "detail": "They were given out before the block was split.", "advice": "Leave them until they can move in a planned window; the renumbering plan shows where each would go.",
                        "nets": [u["net"] for u in outside]})
        loops = [u for u, n in v4 if u["kind"] == "loopback" and n.is_global and any(n.subnet_of(net(b["cidr"])) for b in plan["blocks"]) and not any(n.subnet_of(net(p["cidr"])) for p in plan["pools"] if p["purpose"] == "infra")]
        if loops and any(p["purpose"] == "infra" for p in plan["pools"]):
            out.append({"severity": "info", "kind": "loopback", "title": "Loopbacks outside the infrastructure segment",
                        "detail": ", ".join(u["net"] for u in loops), "advice": "Keep loopbacks together in the infrastructure segment, so they are easy to filter and protect."})
    now = time.time()
    stale = [a for a in plan["allocs"] if now - a.get("at", now) > 30 * 86400 and not any(net(a["cidr"]).overlaps(n) for _, n in v4)]
    if stale:
        out.append({"severity": "info", "kind": "stale", "title": f"{len(stale)} bookings not on the router after 30 days",
                    "detail": ", ".join(f"{a['cidr']} {a['name']}" for a in stale[:8]), "advice": "Free them if they are no longer needed."})
    return out


def renumber(plan, used, moves):
    """A renumbering proposal (nothing is booked or changed): for each {net: segment id}, the next free subnet of the
    same size in that segment, and the addresses to use."""
    busy = taken(plan, used, skip_pools=True)
    out = []
    for cidr, pid in list(moves.items())[:200]:
        p = next((x for x in plan["pools"] if x["id"] == pid), None)
        if not p:
            continue
        n = net(cidr)
        plen = 31 if n.prefixlen == 30 and p["purpose"] in ("p2p", "upstream") else n.prefixlen
        c = first_free(p["cidr"], plen, busy)
        if c:
            busy.append(c)
            out.append({"from": cidr, "to": str(c), "pool": p["name"], "hosts": hosts(c)})
        else:
            out.append({"from": cidr, "to": "", "pool": p["name"], "hosts": []})
    return out
