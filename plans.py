"""Network plans: a drawing of the network, the list of changes that build it,
and a permanent record of every edit and every execution on the router.

Each plan is one JSON file in plans/. Saving a plan keeps the previous version
in its revision history, and every execution (check-only or commit) is
recorded with the exact commands sent and the router's output.
"""
import copy
import json
import os
import re
import threading
import time
import uuid

import actions

HERE = os.path.dirname(os.path.abspath(__file__))
PLANS_DIR = os.path.join(HERE, "plans")
STATUSES = ("draft", "approved", "checked", "executed", "failed", "cancelled")
MAX_REVISIONS = 200


class PlanError(Exception):
    pass


def _now():
    return time.time()


def _content(plan):
    return {k: copy.deepcopy(plan.get(k)) for k in ("name", "description", "status", "diagram", "tasks")}


class PlanStore:
    def __init__(self, folder=PLANS_DIR):
        self.folder = folder
        self.lock = threading.Lock()
        os.makedirs(folder, exist_ok=True)

    def _path(self, pid):
        if not re.match(r"^[a-f0-9]{8,32}$", pid or ""):
            raise PlanError("Invalid plan id.")
        return os.path.join(self.folder, pid + ".json")

    def _write(self, plan):
        tmp = self._path(plan["id"]) + ".tmp"
        with open(tmp, "w") as f:
            json.dump(plan, f, indent=1)
        os.replace(tmp, self._path(plan["id"]))

    def get(self, pid):
        try:
            with open(self._path(pid)) as f:
                return json.load(f)
        except FileNotFoundError:
            raise PlanError("Plan not found.")

    def list(self):
        out = []
        for fn in os.listdir(self.folder):
            if fn.endswith(".json"):
                try:
                    with open(os.path.join(self.folder, fn)) as f:
                        p = json.load(f)
                except (OSError, ValueError):
                    continue
                last_run = p["runs"][-1] if p.get("runs") else None
                out.append({"id": p["id"], "name": p["name"], "description": p.get("description", ""), "status": p["status"],
                            "created": p["created"], "updated": p["updated"], "author": p.get("author", ""),
                            "nodes": len(p["diagram"]["nodes"]), "links": len(p["diagram"]["links"]), "tasks": len(p["tasks"]),
                            "revisions": len(p.get("revisions", [])), "runs": len(p.get("runs", [])),
                            "last_run": {k: last_run[k] for k in ("at", "host", "commit", "ok")} if last_run else None})
        return sorted(out, key=lambda p: -p["updated"])

    def create(self, name, description="", author="", diagram=None, tasks=None):
        name = (name or "").strip()
        if not name:
            raise PlanError("Give the plan a name.")
        t = _now()
        plan = {"id": uuid.uuid4().hex[:12], "name": name[:120], "description": (description or "")[:4000], "status": "draft",
                "author": (author or "")[:60], "created": t, "updated": t,
                "diagram": validate_diagram(diagram or {"nodes": [], "links": []}), "tasks": validate_tasks(tasks or []),
                "revisions": [{"at": t, "note": "Plan created", "by": author or "", "content": None}], "runs": []}
        with self.lock:
            self._write(plan)
        return plan

    def save(self, pid, data, note="", author=""):
        with self.lock:
            plan = self.get(pid)
            before = _content(plan)
            if "name" in data:
                if not str(data["name"]).strip():
                    raise PlanError("Give the plan a name.")
                plan["name"] = str(data["name"]).strip()[:120]
            if "description" in data:
                plan["description"] = str(data["description"])[:4000]
            if "status" in data:
                if data["status"] not in STATUSES:
                    raise PlanError("Unknown status.")
                plan["status"] = data["status"]
            if "diagram" in data:
                plan["diagram"] = validate_diagram(data["diagram"])
            if "tasks" in data:
                plan["tasks"] = validate_tasks(data["tasks"])
            after = _content(plan)
            if after == before:
                return plan
            plan["updated"] = _now()
            plan["revisions"].append({"at": plan["updated"], "note": (note or describe_change(before, after))[:300],
                                      "by": (author or "")[:60], "content": before})
            plan["revisions"] = plan["revisions"][-MAX_REVISIONS:]
            self._write(plan)
            return plan

    def restore(self, pid, index, author=""):
        plan = self.get(pid)
        try:
            rev = plan["revisions"][int(index)]
        except (IndexError, ValueError):
            raise PlanError("Revision not found.")
        if not rev.get("content"):
            raise PlanError("This revision has no saved content.")
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(rev["at"]))
        return self.save(pid, rev["content"], note=f"Restored the version saved before {when}", author=author)

    def duplicate(self, pid, author=""):
        p = self.get(pid)
        return self.create(p["name"] + " (copy)", p.get("description", ""), author, p["diagram"], p["tasks"])

    def delete(self, pid):
        with self.lock:
            os.remove(self._path(pid))

    def add_run(self, pid, run):
        with self.lock:
            plan = self.get(pid)
            plan["runs"].append(run)
            if run["commit"]:
                plan["status"] = "executed" if run["ok"] else "failed"
            elif run["ok"] and plan["status"] in ("draft", "approved", "failed"):
                plan["status"] = "checked"
            plan["updated"] = _now()
            self._write(plan)
            return plan


def describe_change(before, after):
    parts = []
    if before["name"] != after["name"]:
        parts.append(f'renamed to "{after["name"]}"')
    if before["status"] != after["status"]:
        parts.append(f'status {before["status"]} → {after["status"]}')
    if before["description"] != after["description"]:
        parts.append("description edited")
    bn, an = len(before["diagram"]["nodes"]), len(after["diagram"]["nodes"])
    bl, al = len(before["diagram"]["links"]), len(after["diagram"]["links"])
    if before["diagram"] != after["diagram"]:
        d = []
        if an != bn:
            d.append(f"{an - bn:+d} device{'s' if abs(an - bn) != 1 else ''}")
        if al != bl:
            d.append(f"{al - bl:+d} link{'s' if abs(al - bl) != 1 else ''}")
        parts.append("design " + (", ".join(d) if d else "edited"))
    if before["tasks"] != after["tasks"]:
        bt, at = len(before["tasks"]), len(after["tasks"])
        parts.append("tasks " + (f"{at - bt:+d}" if at != bt else "edited"))
    return "; ".join(parts) or "edited"


NODE_TYPES = ("ne8000", "router", "switch", "olt", "reseller", "customer", "upstream", "server", "ap", "note")


def _clean(v, n=200):
    return str(v if v is not None else "")[:n]


def validate_diagram(d):
    if not isinstance(d, dict):
        raise PlanError("Bad diagram.")
    nodes, ids = [], set()
    for n in (d.get("nodes") or [])[:500]:
        nid = _clean(n.get("id"), 40)
        if not nid or nid in ids:
            continue
        ids.add(nid)
        nodes.append({"id": nid, "type": n.get("type") if n.get("type") in NODE_TYPES else "router",
                      "label": _clean(n.get("label"), 80), "x": float(n.get("x") or 0), "y": float(n.get("y") or 0),
                      "props": {_clean(k, 40): _clean(v) for k, v in (n.get("props") or {}).items()}})
    links = []
    for l in (d.get("links") or [])[:1000]:
        if l.get("from") in ids and l.get("to") in ids and l.get("from") != l.get("to"):
            links.append({"id": _clean(l.get("id"), 40) or uuid.uuid4().hex[:8], "from": l["from"], "to": l["to"],
                          "props": {_clean(k, 40): _clean(v) for k, v in (l.get("props") or {}).items()}})
    return {"nodes": nodes, "links": links}


def validate_tasks(tasks):
    out = []
    for t in (tasks or [])[:300]:
        if t.get("action") not in actions.ACTIONS:
            raise PlanError(f"Unknown task type: {t.get('action')}")
        params = {_clean(k, 40): (v if isinstance(v, bool) else _clean(v, 8000)) for k, v in (t.get("params") or {}).items()
                  if k not in actions.SECRET_KEYS}
        out.append({"id": _clean(t.get("id"), 40) or uuid.uuid4().hex[:8], "action": t["action"],
                    "title": _clean(t.get("title"), 160), "params": params, "source": _clean(t.get("source"), 40)})
    return out


def preview(plan):
    out = []
    for t in plan["tasks"]:
        try:
            b = actions.build(t["action"], t["params"])
            out.append({"id": t["id"], "ok": True, "lines": b["preview"], "mode": b["mode"]})
        except actions.ActionError as exc:
            out.append({"id": t["id"], "ok": False, "error": str(exc), "lines": []})
    return out


def execute(plan, router, commit):
    """Run every task in order. On commit, stop at the first failure."""
    results, ok = [], True
    for t in plan["tasks"]:
        r = {"id": t["id"], "title": t["title"], "action": t["action"], "lines": [], "output": "", "ok": False}
        try:
            b = actions.build(t["action"], t["params"])
            r["lines"] = b["preview"]
            if b["mode"] == "exec":
                r["output"] = "(skipped in check-only run)" if not commit else "\n".join(router.execute(c) for c in b["lines"])
            else:
                r["output"] = router.configure(b["lines"], commit=commit)
            r["ok"] = True
        except Exception as exc:
            r["output"] = str(exc)
            ok = False
        results.append(r)
        if not r["ok"] and commit:
            break
    skipped = len(plan["tasks"]) - len(results)
    return {"at": _now(), "host": router.host, "demo": router.demo, "commit": commit, "ok": ok and not skipped,
            "results": results, "skipped": skipped}


EXAMPLE = {
    "name": "Example: connect a new reseller in Nairobi",
    "description": "New reseller on GE0/1/6 VLAN 310 with a /30, limited to 300 Mbit/s, plus a static route for their public block.",
    "diagram": {"nodes": [
        {"id": "n1", "type": "upstream", "label": "Upstream / Internet", "x": 380, "y": 40, "props": {}},
        {"id": "n2", "type": "ne8000", "label": "BNG-NDOVU1 (NE8000 M8)", "x": 380, "y": 190, "props": {"mgmt_ip": "102.216.116.1"}},
        {"id": "n3", "type": "switch", "label": "Nairobi aggregation switch", "x": 380, "y": 340, "props": {}},
        {"id": "n4", "type": "reseller", "label": "New reseller: Kilele Net", "x": 200, "y": 480, "props": {"splynx_id": "553400"}},
        {"id": "n5", "type": "reseller", "label": "Existing: AlphaNet", "x": 560, "y": 480, "props": {}}],
        "links": [
        {"id": "l1", "from": "n1", "to": "n2", "props": {"label": "2 x 100G"}},
        {"id": "l2", "from": "n2", "to": "n3", "props": {"label": "GE0/1/6 trunk", "router_port": "GigabitEthernet0/1/6"}},
        {"id": "l3", "from": "n3", "to": "n4", "props": {"label": "VLAN 310", "router_port": "GigabitEthernet0/1/6", "vlan": "310",
                                                          "address": "102.216.117.9/30", "rate_kbps": "300000"}},
        {"id": "l4", "from": "n3", "to": "n5", "props": {"label": "VLAN 301"}}]},
    "tasks": [
        {"id": "t1", "action": "subif.create", "title": "Sub-interface for Kilele Net, VLAN 310",
         "params": {"parent": "GigabitEthernet0/1/6", "vlan": "310", "encap": "vlan-type", "address": "102.216.117.9/30",
                    "description": "Reseller Kilele Net 553400"}},
        {"id": "t2", "action": "qos.car", "title": "Limit Kilele Net to 300 Mbit/s (download)",
         "params": {"interface": "GigabitEthernet0/1/6.310", "direction": "outbound", "cir": "300000"}},
        {"id": "t3", "action": "qos.car", "title": "Limit Kilele Net to 300 Mbit/s (upload)",
         "params": {"interface": "GigabitEthernet0/1/6.310", "direction": "inbound", "cir": "300000"}},
        {"id": "t4", "action": "route.static_add", "title": "Route Kilele Net public block",
         "params": {"prefix": "102.216.118.0/29", "next_hop": "102.216.117.10", "description": "Kilele-Net-public"}}],
}


def ensure_example(store):
    if not store.list():
        p = EXAMPLE
        store.create(p["name"], p["description"], "example", p["diagram"], p["tasks"])
