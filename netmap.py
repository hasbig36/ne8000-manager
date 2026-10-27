"""The live 'as built' map: one per router, rebuilt from the router whenever it is opened.

Keeps the latest inventory of each router's map in <data>/netmap/<key>.json and a list of
what changed between refreshes (customer links added or removed, descriptions and QoS
profiles changed, ports going down, BGP sessions dropping, LLDP neighbours appearing).
"""
import json
import os
import re
import threading
import time

MAX_CHANGES = 300


def inventory(snap, m):
    inv = {"links": {}, "peers": {}, "lldp": [], "ports": {}}
    for c in m["customers"]:
        inv["links"][c["interface"]] = {"label": c["label"], "description": c["description"], "ip": c["ip"], "profile": c["profile"], "state": c["state"]}
    for u in m["upstreams"].values():
        for p in u["peers"]:
            inv["peers"][p["peer"]] = {"as": u["as"], "state": p["state"]}
    for p in m["ibgp"]:
        inv["peers"][p["peer"]] = {"as": m["local_as"], "state": p["state"]}
    inv["lldp"] = sorted(f"{n['local']} → {n['neighbor']}" for ns in m["lldp"].values() for n in ns)
    for parent in m["groups"]:
        inv["ports"][parent] = (m["by_name"].get(parent) or {}).get("phy", "")
    return inv


def diff(old, new):
    out = []
    ol, nl = old.get("links", {}), new["links"]
    for k in sorted(set(nl) - set(ol)):
        out.append(f"Added {k} {nl[k]['label']}" + (f" ({nl[k]['ip']})" if nl[k]["ip"] else ""))
    for k in sorted(set(ol) - set(nl)):
        out.append(f"Removed {k} {ol[k]['label']}")
    for k in sorted(set(ol) & set(nl)):
        a, b = ol[k], nl[k]
        if a["label"] != b["label"]:
            out.append(f"{k} renamed: {a['label']} → {b['label']}")
        elif a.get("description", b["description"]) != b["description"]:
            out.append(f"{k} description: {a['description'] or 'none'} → {b['description'] or 'none'}")
        if a["profile"] != b["profile"]:
            out.append(f"{k} QoS profile: {a['profile'] or 'none'} → {b['profile'] or 'none'}")
        if a["ip"] != b["ip"]:
            out.append(f"{k} address: {a['ip'] or 'none'} → {b['ip'] or 'none'}")
        if a["state"] != b["state"] and b["state"]:
            out.append(f"{k} ({b['label']}) is now {b['state']}")
    op, np_ = old.get("peers", {}), new["peers"]
    for k in sorted(set(np_) - set(op)):
        out.append(f"New BGP peer {k} AS{np_[k]['as']} ({np_[k]['state']})")
    for k in sorted(set(op) - set(np_)):
        out.append(f"BGP peer {k} AS{op[k]['as']} removed")
    for k in sorted(set(op) & set(np_)):
        if op[k]["state"] != np_[k]["state"]:
            out.append(f"BGP peer {k} AS{np_[k]['as']}: {op[k]['state']} → {np_[k]['state']}")
    for x in sorted(set(new["lldp"]) - set(old.get("lldp", []))):
        out.append(f"LLDP neighbour appeared: {x}")
    for x in sorted(set(old.get("lldp", [])) - set(new["lldp"])):
        out.append(f"LLDP neighbour gone: {x}")
    for k, st in new["ports"].items():
        if old.get("ports", {}).get(k) not in (None, st) and st:
            out.append(f"Port {k} is now {st}")
    return out


class NetMapStore:
    def __init__(self, data_dir):
        self.folder = os.path.join(data_dir, "netmap")
        os.makedirs(self.folder, exist_ok=True)
        self.lock = threading.Lock()

    def _path(self, key):
        return os.path.join(self.folder, re.sub(r"[^A-Za-z0-9_.-]", "_", key)[:80] + ".json")

    def load(self, key):
        try:
            with open(self._path(key)) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {"at": None, "inventory": None, "changes": []}

    def update(self, key, inv):
        """Store the new inventory; returns the stored record with any new changes appended."""
        with self.lock:
            rec = self.load(key)
            now = time.time()
            if rec["inventory"] is None:
                rec["changes"].append({"at": now, "text": f"First map: {len(inv['links'])} customer links, {len(inv['peers'])} BGP peers, {len(inv['lldp'])} LLDP neighbours"})
            else:
                rec["changes"] += [{"at": now, "text": t} for t in diff(rec["inventory"], inv)]
            rec["changes"] = rec["changes"][-MAX_CHANGES:]
            rec["inventory"], rec["at"] = inv, now
            tmp = self._path(key) + ".tmp"
            with open(tmp, "w") as f:
                json.dump(rec, f)
            os.replace(tmp, self._path(key))
            return rec
