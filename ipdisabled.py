"""Addresses taken off the router with Disable, kept here so Enable can put them back as they were.

VRP has no per-address shutdown: Disable removes the address ('undo ip address ...') and records it;
Enable adds it again. One JSON file per data folder, keyed by router (as the network map is).
"""
import json
import os
import threading
import time


class DisabledIPs:
    def __init__(self, data_dir):
        self.path = os.path.join(data_dir, "ip_disabled.json")
        self.lock = threading.Lock()

    def _load(self):
        try:
            with open(self.path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save(self, data):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, self.path)

    def get(self, key):
        return self._load().get(key, {})

    def disable(self, key, interface, ip, sub, by, reason=""):
        with self.lock:
            data = self._load()
            rows = [x for x in data.setdefault(key, {}).setdefault(interface, []) if x["ip"] != ip]
            rows.append({"ip": ip, "sub": bool(sub), "by": by, "at": time.time(), "reason": reason[:200]})
            data[key][interface] = rows
            self._save(data)

    def enable(self, key, interface, ip):
        with self.lock:
            data = self._load()
            rows = [x for x in data.get(key, {}).get(interface, []) if x["ip"] != ip]
            if rows:
                data[key][interface] = rows
            else:
                data.get(key, {}).pop(interface, None)
            self._save(data)
