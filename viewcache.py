"""Router views served from server memory.

Every page used to wait for the router (SSH commands, SNMP walks) on each load. Now the server keeps the last answer of
each view per connected router and gives it straight away:

- fresh enough (younger than its TTL): served as is;
- older but not too old (MAX_STALE): served at once, and refreshed in the background for the next request;
- missing (first use): fetched now; several requests for the same view share one fetch.

A warmer thread per connected router refreshes the busy views ahead of time while someone is using it, so pages open
from memory. Any change made through the app (a commit, a CLI command, a new SSH login) drops the stored views so the
next answer comes from the router again.
"""
import threading
import time

TTL = {"overview": 8, "interfaces": 8, "arp": 25, "bgp": 20, "ospf": 30, "isis": 30, "mpls": 30, "neighbors": 60,
       "vlans": 30, "qos": 60, "vpn_instances": 60, "system": 120, "hardware": 300, "logs": 15}
MAX_STALE = 600          # never show data older than this without fetching it first
IDLE = 300               # stop warming a router nobody has looked at for this long
WARM = (("overview", 1), ("interfaces", 1), ("arp", 3), ("bgp", 3), ("hardware", 30), ("qos", 12))   # (view, every N rounds of 5 s)


class ViewCache:
    def __init__(self):
        self.lock = threading.Lock()
        self.data = {}        # (router id, view) -> (time, value)
        self.busy = {}        # (router id, view) -> Event while a fetch runs
        self.used = {}        # router id -> last time a page asked
        self.warmers = {}     # router id -> Thread

    def _fetch(self, r, name):
        key = (id(r), name)
        with self.lock:
            ev = self.busy.get(key)
            mine = ev is None
            if mine:
                ev = self.busy[key] = threading.Event()
        if not mine:              # someone else is already asking the router: wait for that answer
            ev.wait(120)
            hit = self.data.get(key)
            if hit:
                return hit[1]
            raise RuntimeError("The router did not answer in time.")
        try:
            value = getattr(r, name)()
            with self.lock:
                self.data[key] = (time.time(), value)
            return value
        finally:
            with self.lock:
                self.busy.pop(key, None)
            ev.set()

    def _background(self, r, name):
        if (id(r), name) in self.busy:
            return
        threading.Thread(target=self._quiet, args=(r, name), daemon=True).start()

    def _quiet(self, r, name):
        try:
            self._fetch(r, name)
        except Exception:
            pass                  # the next request fetches again and shows the error

    def get(self, r, name):
        """-> (value, age in seconds)"""
        self.used[id(r)] = time.time()
        self.start(r)
        hit = self.data.get((id(r), name))
        now = time.time()
        if hit:
            age = now - hit[0]
            if age < TTL.get(name, 30):
                return hit[1], age
            if age < MAX_STALE:
                self._background(r, name)
                return hit[1], age
        return self._fetch(r, name), 0.0

    def invalidate(self, r):
        rid = id(r)
        with self.lock:
            for k in [k for k in self.data if k[0] == rid]:
                del self.data[k]
        self.used[rid] = time.time()
        for name, _ in WARM[:2]:  # get the main views again straight away
            self._background(r, name)

    def start(self, r):
        rid = id(r)
        if rid in self.warmers and self.warmers[rid].is_alive():
            return
        self.used.setdefault(rid, time.time())
        t = threading.Thread(target=self._warm, args=(r,), daemon=True)
        self.warmers[rid] = t
        t.start()

    def _warm(self, r):
        rid, n = id(r), 0
        try:
            while not r.stop.is_set() and time.time() - self.used.get(rid, 0) < IDLE:
                for name, every in WARM:
                    if r.stop.is_set():
                        break
                    hit = self.data.get((rid, name))
                    if n % every == 0 and (not hit or time.time() - hit[0] >= TTL.get(name, 30) * 0.6):
                        self._quiet(r, name)
                n += 1
                r.stop.wait(5)
        finally:
            self.warmers.pop(rid, None)
            if r.stop.is_set():
                self.forget(r)

    def forget(self, r):
        rid = id(r)
        with self.lock:
            for k in [k for k in self.data if k[0] == rid]:
                del self.data[k]
        self.used.pop(rid, None)
