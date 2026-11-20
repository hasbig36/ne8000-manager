"""OLTs (GPON and EPON): Huawei, ZTE, Nokia, VSOL and HiOSO. Read-only monitoring over SNMP.

One device class with a reader per make. Every make gets the standard parts the same way as routers (PON and uplink
ports with their traffic and errors, uptime, CPU and temperatures where the make reports them). The ONU list (serial or
MAC, name, status, optical levels, distance, why it last went down) comes from the make's own MIB:

- Huawei MA5600T / MA5800 / EA5800, GPON: HUAWEI-XPON-MIB (hwGponDeviceOnt*, hwGponOntOpticalDdm*);
- ZTE C300 / C320 / C600, GPON: ZTE-AN-GPON-* (zxAnGponOnu*, zxAnPonRxOpticalPower);
- other makes and EPON tables: no reader yet. Their ports and traffic still show, and an admin can take an SNMP
  snapshot of the OLT's own MIB (read-only walk) so a reader can be written against the real device.

Nothing is ever written to an OLT from here (authorising ONUs comes later, with preview and check first)."""
import re
import time

from device import DeviceError
from snmp import text as _txt
from snmp_router import IF, IFX, SYS, SnmpRouter

MAKES = {"huawei": "Huawei", "zte": "ZTE", "nokia": "Nokia", "vsol": "VSOL", "hioso": "HiOSO"}
WEAK_DBM, BAD_DBM = -27.0, -28.5           # ONU receive level: weak below -27 dBm, too low below -28.5 dBm

# Huawei HUAWEI-XPON-MIB, index <PON ifIndex>.<ONT id>
HW = "1.3.6.1.4.1.2011.6.128.1.1.2"
HW_SN, HW_DESC = HW + ".43.1.3", HW + ".43.1.9"
HW_RUN, HW_DIST, HW_DOWN = HW + ".46.1.15", HW + ".46.1.20", HW + ".46.1.24"
HW_RX, HW_TX, HW_OLT_RX = HW + ".51.1.4", HW + ".51.1.3", HW + ".51.1.6"
HW_DOWN_CAUSE = {1: "LOS (fibre)", 2: "LOSi/LOBi", 3: "LOFi", 4: "SFi", 5: "LOAi", 6: "LOAMi", 7: "deactivate failed",
                 8: "deactivated", 9: "reset", 10: "re-registered", 11: "pop-up failed", 13: "dying gasp (power off)",
                 15: "LOKi", 18: "deactivated (ring)", 30: "port shut down", 31: "ONU reset", 32: "ONU reset (CLI)"}

# ZTE C300/C320/C600, index <PON ifIndex>.<ONU id> (optics: .<ONU id>.1)
ZTE = "1.3.6.1.4.1.3902.1012.3"
ZTE_NAME, ZTE_DESC, ZTE_SN = ZTE + ".28.1.1.2", ZTE + ".28.1.1.3", ZTE + ".28.1.1.5"
ZTE_PHASE, ZTE_RX, ZTE_DIST = ZTE + ".28.2.1.4", ZTE + ".50.12.1.1.10", ZTE + ".11.4.1.2"
ZTE_PHASES = {1: "registering", 2: "los", 3: "registering", 4: "online", 5: "power off", 6: "auth failed", 7: "offline"}

NO_VALUE = (2147483647, -2147483648, 65535, 0x7FFFFFFF)


def _sn(v):
    """GPON serial: 4 vendor letters + 8 hex digits (HWTC1A2B3C4D) from the 8 raw bytes, or the text as given."""
    if isinstance(v, bytes) and len(v) == 8 and v[:4].isalpha():
        return v[:4].decode("ascii", "replace") + v[4:].hex().upper()
    if isinstance(v, bytes) and len(v) == 6:
        return ":".join(f"{b:02x}" for b in v)        # EPON: the ONU's MAC
    return _txt(v)


def _num(v):
    return v if isinstance(v, int) and v not in NO_VALUE else None


def onu_health(o):
    """'' (fine), 'weak' or 'bad' from the ONU's receive level."""
    rx = o.get("rx_dbm")
    if o.get("status") != "online" or rx is None:
        return ""
    return "bad" if rx < BAD_DBM else "weak" if rx < WEAK_DBM else ""


class Olt(SnmpRouter):
    kind = "olt"

    def __init__(self, host, snmp, make="huawei", ssh_port=22):
        self.vendor = make if make in MAKES else "huawei"
        super().__init__(host, snmp, ssh_port)
        self.via = "SNMP"

    # read-only: nothing is changed on OLTs yet
    @property
    def can_configure(self):
        return False

    def attach_ssh(self, username, password):
        raise DeviceError("OLT logins come with ONU authorisation. Monitoring works over SNMP.")

    def _configure(self, lines, commit):
        raise DeviceError("Changes to OLTs are not supported yet.")

    def execute(self, command):
        raise DeviceError("Changes to OLTs are not supported yet.")

    def _load_replace(self, text, commit, description):
        raise DeviceError("Changes to OLTs are not supported yet.")

    def _pon_names(self):
        names = {i: _txt(v) for i, v in self.walk(IFX + ".1", 300).items()}
        for i, v in self.walk(IF + ".2", 300).items():           # ifDescr where ifName is missing
            names.setdefault(i, _txt(v))
        return names

    def _w(self, oid, ttl=30):
        try:
            return self.walk(oid, ttl)
        except DeviceError:
            return {}

    # ---- the ONU list -------------------------------------------------------
    def onus(self):
        reader = {"huawei": self._huawei_onus, "zte": self._zte_onus}.get(self.vendor)
        rows = reader() if reader else []
        note = ""
        if not reader:
            note = (f"Reading ONUs from {MAKES.get(self.vendor, self.vendor)} OLTs is not built yet. The PON ports and their traffic "
                    "show under Interfaces. An admin can take an SNMP snapshot below so the reader can be made for this OLT.")
        elif not rows:
            note = ("The OLT gave no ONU list over SNMP. If it has ONUs, it may be an EPON OLT or firmware the app doesn't read yet: "
                    "an admin can take an SNMP snapshot below.")
        for o in rows:
            o["health"] = onu_health(o)
        ports = {}
        for o in rows:
            p = ports.setdefault(o["port"], {"port": o["port"], "total": 0, "online": 0, "offline": 0, "los": 0, "power_off": 0,
                                             "weak": 0, "rx": []})
            p["total"] += 1
            st = o["status"]
            p["online" if st == "online" else "offline"] += 1
            p["los"] += st == "los"
            p["power_off"] += st == "power off"
            p["weak"] += bool(o["health"])
            if o.get("rx_dbm") is not None and st == "online":
                p["rx"].append(o["rx_dbm"])
        for p in ports.values():
            rx = p.pop("rx")
            p["avg_rx"] = round(sum(rx) / len(rx), 2) if rx else None
            p["worst_rx"] = min(rx) if rx else None
            # most ONUs on one port down by LOS at once: usually the feeder fibre or splitter, not the customers
            p["suspect_cut"] = p["total"] >= 4 and p["los"] >= max(3, p["total"] * 0.6)
        return {"make": MAKES.get(self.vendor, self.vendor), "onus": rows,
                "ports": sorted(ports.values(), key=lambda p: [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", p["port"])]),
                "total": len(rows), "online": sum(o["status"] == "online" for o in rows),
                "weak": sum(bool(o["health"]) for o in rows), "note": note, "weak_dbm": WEAK_DBM, "bad_dbm": BAD_DBM,
                "time": time.time()}

    def _huawei_onus(self):
        names = self._pon_names()
        sn = self._w(HW_SN, 60)
        if not sn:
            return []
        desc, run, dist, down = self._w(HW_DESC, 120), self._w(HW_RUN), self._w(HW_DIST), self._w(HW_DOWN)
        rx, tx, orx = self._w(HW_RX), self._w(HW_TX), self._w(HW_OLT_RX)
        out = []
        for idx, s in sn.items():
            pon, _, oid = idx.partition(".")
            cause = _num(down.get(idx))
            online = run.get(idx) == 1
            status = "online" if online else "los" if cause == 1 else "power off" if cause == 13 else "offline"
            r, t, o = _num(rx.get(idx)), _num(tx.get(idx)), _num(orx.get(idx))
            out.append({"port": names.get(pon, f"PON {pon}"), "id": int(oid) if oid.isdigit() else oid, "sn": _sn(s),
                        "name": _txt(desc.get(idx)), "status": status,
                        "rx_dbm": round(r / 100, 2) if r is not None and online else None,
                        "tx_dbm": round(t / 100, 2) if t is not None and online else None,
                        "olt_rx_dbm": round((o - 10000) / 100, 2) if o is not None and online else None,
                        "distance_m": _num(dist.get(idx)) if online else None,
                        "down_cause": "" if online else HW_DOWN_CAUSE.get(cause, f"code {cause}" if cause is not None else "")})
        return out

    def _zte_onus(self):
        names = self._pon_names()
        sn = self._w(ZTE_SN, 60)
        if not sn:
            return []
        name, desc, phase, rx, dist = self._w(ZTE_NAME, 120), self._w(ZTE_DESC, 120), self._w(ZTE_PHASE), self._w(ZTE_RX), self._w(ZTE_DIST)
        out = []
        for idx, s in sn.items():
            pon, _, oid = idx.partition(".")
            ph = phase.get(idx)
            status = ZTE_PHASES.get(ph, "offline") if isinstance(ph, int) else "offline"
            raw = rx.get(idx + ".1")
            dbm = None
            if isinstance(raw, int) and raw not in NO_VALUE:
                dbm = round((raw if raw <= 30000 else raw - 65536) * 0.002 - 30, 2)
            online = status == "online"
            out.append({"port": names.get(pon, f"PON {pon}"), "id": int(oid) if oid.isdigit() else oid, "sn": _sn(s),
                        "name": _txt(name.get(idx)) or _txt(desc.get(idx)), "status": status,
                        "rx_dbm": dbm if online else None, "tx_dbm": None, "olt_rx_dbm": None,
                        "distance_m": _num(dist.get(idx)) if online else None,
                        "down_cause": "" if online else {"los": "LOS (fibre)", "power off": "dying gasp (power off)"}.get(status, "")})
        return out

    # ---- the rest of the views ------------------------------------------------
    def overview(self):
        o = super().overview()
        descr = _txt(self.get(SYS + ".1.0").get(SYS + ".1.0"))
        m = re.search(r"(MA5\d{3}\S*|EA5\d{3}\S*|C[36]\d{2}\S*|ZXA10\s*\S+|7360\S*|7362\S*|ISAM\S*|V\d{4}\S*)", descr, re.I)
        o["model"] = f"{MAKES.get(self.vendor, '')} {m.group(1) if m else 'OLT'}".strip()
        o["kind"] = "olt"
        try:
            s = self.onus()
            o["onus"] = {k: s[k] for k in ("total", "online", "weak")}
        except DeviceError:
            o["onus"] = None
        return o

    def bgp_peers(self):
        return {"router_id": "", "local_as": "", "total": 0, "established": 0, "peers": []}

    def bgp(self):
        return {**self.bgp_peers(), "config": {}}

    def track(self, detail=True):
        return {"bgp": []}

    def snmp_snapshot(self, max_rows=20000):
        """Read-only walk of the OLT's own MIB (its enterprise number), to write a reader for a new make or firmware.
        Community and keys are not in it. Values are shown as text or hex."""
        oid = _txt(self.get(SYS + ".2.0").get(SYS + ".2.0"))
        m = re.match(r"^1\.3\.6\.1\.4\.1\.(\d+)", oid or "")
        if not m:
            raise DeviceError("The OLT did not say which make it is (sysObjectID).")
        base = f"1.3.6.1.4.1.{m.group(1)}"
        try:
            rows = self.snmp.walk(base, max_rows=max_rows, reps=20)
        except Exception as exc:
            raise DeviceError(f"SNMP: {exc}") from exc
        lines = [f"# sysObjectID {oid}", f"# sysDescr {_txt(self.get(SYS + '.1.0').get(SYS + '.1.0'))}", f"# {len(rows)} values under {base}"]
        for k, v in rows.items():
            if isinstance(v, bytes):
                t = v.decode("ascii", "replace")
                v = t if t.isprintable() else "0x" + v.hex()
            lines.append(f"{base}.{k} = {v}")
        return "\n".join(lines) + "\n"
