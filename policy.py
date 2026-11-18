"""What would a route-policy do to one prefix?

Walks the policy's nodes in order the way VRP does: a node matches when all its if-match clauses match;
a permit node accepts the route (and applies its apply clauses), a deny node rejects it, and a route that
no node matches is rejected. Prefix lists are checked exactly (index order, ge / le, default deny).

Only the prefix is known here, so a clause about the route's AS path, communities, next hop and so on
cannot be decided: the walk then says the answer depends on that clause and goes on to show both ways.
"""
import ipaddress
import re


def prefix_list_permits(plist, net):
    """-> (True/False, entry index or None). An empty or missing list permits nothing."""
    for e in sorted((plist or {}).get("entries", []), key=lambda x: x["index"]):
        p = ipaddress.ip_network(e["prefix"], strict=False)
        if net.version != p.version or net.prefixlen < p.prefixlen or not net.subnet_of(p):
            continue
        lo = e["ge"] if e["ge"] is not None else e["len"]
        hi = e["le"] if e["le"] is not None else (net.max_prefixlen if e["ge"] is not None else e["len"])
        if lo <= net.prefixlen <= hi:
            return e["action"] == "permit", e["index"]
    return False, None


def clause_result(clause, net, prefixes):
    """-> (True / False / None for unknown, words)"""
    m = re.match(r"^if-match ip-prefix (\S+)", clause)
    if m:
        name = m.group(1)
        if name not in prefixes:
            return None, f"prefix list {name} is not defined (or was not read)"
        ok, idx = prefix_list_permits(prefixes[name], net)
        return ok, (f"{name} index {idx} {'permits' if ok else 'denies'} it" if idx is not None else f"no entry in {name} covers it")
    m = re.match(r"^if-match (\S+)", clause)
    what = m.group(1) if m else clause
    return None, f"depends on the route's {what.replace('-', ' ')}"


def evaluate(prefix, policy, prefixes):
    """-> {"prefix", "result": "permit" | "deny" | "depends", "node", "steps": [...], "apply": [...]}
    "depends" only when the unknown clauses could really change the outcome."""
    net = ipaddress.ip_network(prefix, strict=False)
    steps, possible, apply_ = [], set(), []
    final = None
    for node in sorted(policy.get("nodes", []), key=lambda n: n["node"]):
        results = [(c,) + clause_result(c, net, prefixes) for c in node["match"]]
        if any(r[1] is False for r in results):
            steps.append({"node": node["node"], "action": node["action"], "matches": False,
                          "why": "; ".join(r[2] for r in results if r[1] is False)})
            continue
        if any(r[1] is None for r in results):
            steps.append({"node": node["node"], "action": node["action"], "matches": None,
                          "why": "; ".join(r[2] for r in results)})
            if not (node.get("goto_next") and node["action"] == "permit"):
                possible.add(node["action"])
            continue
        steps.append({"node": node["node"], "action": node["action"], "matches": True,
                      "why": "; ".join(r[2] for r in results) or "no match clause: matches every route"})
        if node.get("goto_next") and node["action"] == "permit":
            apply_ += node["apply"]
            continue
        final = node
        break
    possible.add(final["action"] if final else "deny")
    result = possible.pop() if len(possible) == 1 else "depends"
    return {"prefix": str(net), "result": result, "node": final["node"] if final else None, "steps": steps,
            "apply": (apply_ + final["apply"]) if final and final["action"] == "permit" else [],
            "note": "" if final else "No node matched for sure, so the route ends at the implicit deny at the end of every route-policy."}
