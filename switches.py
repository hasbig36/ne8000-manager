"""Switches: Huawei (S-series campus on VRP5, CloudEngine on VRP8) and MikroTik (CRS on RouterOS, CSS on SwOS).

Read-only. Monitoring is SNMP, the same way as routers (interfaces, traffic, CPU, memory, temperatures, optics, LLDP).
On top of that every switch gets the "Switching" view: each port with its VLANs (untagged / tagged), the MAC addresses
learnt behind it, PoE state and power, spanning-tree state and the LLDP neighbour, plus the whole MAC table and VLAN list.

Sources, standard first so any make answers:
- BRIDGE-MIB / Q-BRIDGE-MIB for bridge ports, the MAC table (dot1qTpFdb, dot1dTpFdb), VLAN membership and STP state;
- POWER-ETHERNET-MIB for PoE, MIKROTIK-MIB mtxrPOETable on MikroTik;
- with a login: Huawei "display mac-address" over SSH, or the RouterOS API (/interface/bridge/host, /bridge/vlan, PoE).
"""
import re

import parsers as P
from device import DeviceError
from snmp import text as _txt
from snmp_router import IFX, SnmpRouter, SshRequired

BASE_PORT_IF = "1.3.6.1.2.1.17.1.4.1.2"        # dot1dBasePortIfIndex: bridge port -> ifIndex
STP_STATE = "1.3.6.1.2.1.17.2.15.1.3"          # dot1dStpPortState
FDB_Q = "1.3.6.1.2.1.17.7.1.2.2.1.2"           # dot1qTpFdbPort: fdbId.mac -> bridge port
FDB_D = "1.3.6.1.2.1.17.4.3.1.2"               # dot1dTpFdbPort: mac -> bridge port
VLAN_FDB = "1.3.6.1.2.1.17.7.1.4.2.1.3"        # dot1qVlanFdbId: time.vlan -> fdbId
VLAN_NAME = "1.3.6.1.2.1.17.7.1.4.3.1.1"       # dot1qVlanStaticName
VLAN_EGRESS = "1.3.6.1.2.1.17.7.1.4.3.1.2"     # dot1qVlanStaticEgressPorts (PortList)
VLAN_UNTAG = "1.3.6.1.2.1.17.7.1.4.3.1.4"      # dot1qVlanStaticUntaggedPorts (PortList)
PVID = "1.3.6.1.2.1.17.7.1.4.5.1.1"            # dot1qPvid: bridge port -> VLAN
PETH_PORT = "1.3.6.1.2.1.105.1.1.1"            # pethPsePortTable: .3 admin, .6 detection status
PETH_MAIN = "1.3.6.1.2.1.105.1.3.1.1"          # pethMainPseTable: .2 power (W), .4 consumption (W)
MT_POE = "1.3.6.1.4.1.14988.1.1.15.1.1"        # mtxrPOETable: .2 name, .3 status, .4 voltage (dV), .5 current (mA), .6 power (dW)
STP = {1: "disabled", 2: "blocking", 3: "listening", 4: "learning", 5: "forwarding", 6: "broken"}
PETH_STATUS = {1: "off", 2: "searching", 3: "on", 4: "fault", 5: "test", 6: "fault"}
MT_POE_STATUS = {1: "off", 2: "searching", 3: "on", 4: "overload", 5: "short circuit", 6: "voltage too low", 7: "current too low"}


def _mac_from_index(parts):
    return ":".join(f"{int(x):02x}" for x in parts)


def _portlist(raw):
    """PortList octets -> bridge port numbers (bit 7 of the first byte is port 1)."""
    if not isinstance(raw, (bytes, bytearray)):
        return []
    out = []
    for i, b in enumerate(raw):
        for bit in range(8):
            if b & (0x80 >> bit):
                out.append(i * 8 + bit + 1)
    return out


class SwitchViews:
    """switching() over SNMP; subclasses add CLI / API sources. Needs walk(), if_table(), interfaces(), neighbors()."""

    def _w(self, oid, ttl=30):
        try:
            return self.walk(oid, ttl)
        except DeviceError:
            return {}

    def _port_names(self):
        names = {i: _txt(v) for i, v in self.walk(IFX + ".1", 300).items()}
        bport = self._w(BASE_PORT_IF, 300)
        return names, {str(p): names.get(str(ifx), str(ifx)) for p, ifx in bport.items()}

    def snmp_macs(self, ports):
        fdb_vlan = {str(v): k.split(".")[-1] for k, v in self._w(VLAN_FDB, 300).items()}
        rows = []
        for idx, port in self._w(FDB_Q, 20).items():
            parts = idx.split(".")
            if len(parts) < 7:
                continue
            rows.append({"mac": _mac_from_index(parts[-6:]), "vlan": fdb_vlan.get(parts[0], parts[0]),
                         "port": ports.get(str(port), str(port)), "type": "dynamic"})
        if not rows:
            for idx, port in self._w(FDB_D, 20).items():
                parts = idx.split(".")
                if len(parts) == 6:
                    rows.append({"mac": _mac_from_index(parts), "vlan": "", "port": ports.get(str(port), str(port)), "type": "dynamic"})
        return rows

    def snmp_vlans(self, ports):
        names, egress, untag = self._w(VLAN_NAME, 120), self._w(VLAN_EGRESS, 60), self._w(VLAN_UNTAG, 60)
        out = []
        for vid in sorted(set(names) | set(egress), key=lambda x: int(x) if x.isdigit() else 0):
            u = [ports.get(str(p), str(p)) for p in _portlist(untag.get(vid))]
            t = [ports.get(str(p), str(p)) for p in _portlist(egress.get(vid)) if ports.get(str(p), str(p)) not in u]
            out.append({"id": vid, "name": _txt(names.get(vid)), "untagged": u, "tagged": t})
        return out

    def snmp_poe(self, if_names):
        """{port name: {status, watts}} and the PoE budget."""
        poe, budget = {}, None
        mt = self._w(MT_POE + ".2", 120)
        if mt:
            st, pw = self._w(MT_POE + ".3", 15), self._w(MT_POE + ".6", 15)
            for i, n in mt.items():
                poe[_txt(n)] = {"status": MT_POE_STATUS.get(st.get(i), str(st.get(i) or "")),
                                "watts": round(pw[i] / 10, 1) if isinstance(pw.get(i), int) else None}
            return poe, budget
        det = self._w(PETH_PORT + ".6", 15)
        if det:
            # group.port: on most switches the port number is the ifIndex; otherwise the n-th Ethernet port
            eth = [n for i, n in sorted(if_names.items(), key=lambda x: int(x[0]) if x[0].isdigit() else 0)
                   if P.interface_type(n) == "ethernet" or n.lower().startswith(("ether", "ge", "gigabit", "xge", "10ge", "multige"))]
            for idx, v in det.items():
                port = idx.split(".")[-1]
                name = if_names.get(port) or (eth[int(port) - 1] if port.isdigit() and 0 < int(port) <= len(eth) else f"PoE port {port}")
                poe[name] = {"status": PETH_STATUS.get(v, str(v)), "watts": None}
            main_p, main_c = self._w(PETH_MAIN + ".2", 60), self._w(PETH_MAIN + ".4", 15)
            if main_p:
                budget = {"watts": sum(v for v in main_p.values() if isinstance(v, int)),
                          "used": sum(v for v in main_c.values() if isinstance(v, int))}
        return poe, budget

    def cli_macs(self):
        return []

    def switching(self):
        if_names, ports = self._port_names()
        macs = self.cli_macs() or self.snmp_macs(ports)
        vlans = self.snmp_vlans(ports)
        poe, budget = self.snmp_poe(if_names)
        stp = {ports.get(p, p): STP.get(v, str(v)) for p, v in self._w(STP_STATE, 30).items()}
        pvid = {ports.get(p, p): str(v) for p, v in self._w(PVID, 60).items()}
        try:
            nb = {r["local"]: r["neighbor"] for r in self.neighbors()["lldp"]}
        except DeviceError:
            nb = {}
        return self._assemble(macs, vlans, poe, budget, stp, pvid, nb)

    def _assemble(self, macs, vlans, poe, budget, stp, pvid, nb):
        for m in macs:      # one MAC format everywhere: aa:bb:cc:dd:ee:ff
            h = re.sub(r"[^0-9a-f]", "", m["mac"].lower())
            if len(h) == 12:
                m["mac"] = ":".join(h[i:i + 2] for i in range(0, 12, 2))
        count = {}
        for m in macs:
            count[m["port"]] = count.get(m["port"], 0) + 1
        rows = []
        for i in self.interfaces():
            if i["type"] not in ("ethernet", "eth-trunk"):
                continue
            n = i["name"]
            rows.append({"name": n, "phy": i["phy"], "admin_down": i["admin_down"], "speed_mbps": i.get("speed_mbps") or 0,
                         "description": i.get("description", ""), "in_errors": i.get("in_errors", 0), "out_errors": i.get("out_errors", 0),
                         "in_util": i.get("in_util", 0), "out_util": i.get("out_util", 0),
                         "untagged": [v["id"] for v in vlans if n in v["untagged"]] or ([pvid[n]] if pvid.get(n) else []),
                         "tagged": [v["id"] for v in vlans if n in v["tagged"]],
                         "macs": count.get(n, 0), "poe": poe.get(n), "stp": stp.get(n, ""), "neighbor": nb.get(n, "")})
        return {"ports": rows, "macs": macs[:20000], "vlans": vlans, "poe_budget": budget,
                "poe_ports": sum(1 for r in rows if r["poe"] and r["poe"]["status"] == "on")}


class HuaweiSwitch(SwitchViews, SnmpRouter):
    """A Huawei switch over SNMP; an SSH login adds the CLI (VRP5 campus switches use netmiko's "huawei" driver,
    CloudEngine the same VRP8 driver as the NE8000). Read-only in the app."""
    kind = "switch"

    def __init__(self, host, snmp, ssh_port=22, vrp="vrp5"):
        self.vrp = vrp
        super().__init__(host, snmp, ssh_port)

    @property
    def can_configure(self):
        return False

    def attach_ssh(self, username, password):
        from device import RealRouter
        ssh = RealRouter(self.host, username, password, self.ssh_port, poll=False,
                         device_type="huawei" if self.vrp == "vrp5" else "huawei_vrpv8")
        ssh.lock = self.lock
        old, self.ssh, self.ssh_user = self.ssh, ssh, username
        self.via = "SNMP + SSH"
        if old:
            old.close()
        self.cache.clear()

    def _configure(self, lines, commit):
        raise DeviceError("Changes to switches are not supported yet.")

    def execute(self, command):
        raise DeviceError("Changes to switches are not supported yet.")

    def _load_replace(self, text, commit, description):
        raise DeviceError("Changes to switches are not supported yet.")

    def cli_macs(self):
        if not self.ssh:
            return []
        try:
            return P.parse_mac_table(self.cached("display mac-address", 20))
        except DeviceError:
            return []

    def overview(self):
        o = super().overview()
        o["kind"] = "switch"
        if o.get("model") == "NetEngine 8000":
            descr = _txt(self.get("1.3.6.1.2.1.1.1.0").get("1.3.6.1.2.1.1.1.0"))
            m = re.search(r"(S\d{4}\S*|CE\d{4}\S*|S\d{3,4}-\S+)", descr)
            o["model"] = m.group(1) if m else "Huawei switch"
        return o

    def bgp_peers(self):
        return {"router_id": "", "local_as": "", "total": 0, "established": 0, "peers": []}

    def bgp(self):
        return {**self.bgp_peers(), "config": {}}

    def track(self, detail=True):
        out = {"bgp": []}
        if self.ssh:
            out["mac"] = self.cli_macs()
        return out


def mikrotik_api_switching(api_router, base):
    """MikroTik with a login: the bridge host table, bridge VLANs and PoE from the RouterOS API.
    base: the SNMP view's ports (or None) to fill in names, speeds and states."""
    hosts = api_router.q_or_empty("/interface/bridge/host", ttl=20)
    macs = [{"mac": (h.get("mac-address") or "").lower(), "vlan": h.get("vid", ""), "port": h.get("on-interface") or h.get("interface", ""),
             "type": "dynamic" if h.get("dynamic", "true") == "true" else "static"} for h in hosts if h.get("local") != "true"]
    vlans = []
    for v in api_router.q_or_empty("/interface/bridge/vlan", ttl=60):
        split = lambda k: [x for x in re.split(r"[,\s]+", v.get(k, "") or "") if x]
        ids = split("vlan-ids") or [""]
        for vid in ids:
            vlans.append({"id": vid, "name": v.get("comment", ""), "untagged": split("current-untagged") or split("untagged"),
                          "tagged": split("current-tagged") or split("tagged")})
    poe = {}
    for p in api_router.q_or_empty("/interface/ethernet/poe", ttl=30):
        poe[p.get("name", "")] = {"status": "off" if p.get("poe-out") == "off" else (p.get("poe-out-status") or "auto"), "watts": None}
    pvid = {p.get("interface", ""): p.get("pvid", "") for p in api_router.q_or_empty("/interface/bridge/port", ttl=60)}
    return macs, vlans, poe, pvid
