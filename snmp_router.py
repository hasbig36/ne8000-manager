"""SNMP-first router session.

Monitoring (dashboard, interfaces, traffic, CPU/memory, temperatures, optics, BGP peers,
ARP, LLDP) is read over SNMPv3, which is light on the router and needs no SSH line.
Everything that needs the CLI (console, configuration, routes, alarms, logs, changes)
uses an SSH session that is opened only when someone signs in with their router login."""
import re
import time

import parsers as P
from device import POLL_SECONDS, DeviceError, RealRouter, Router, _cidr, ipv6_addrs
from snmp import SnmpClient, SnmpError, mac, text

SYS = "1.3.6.1.2.1.1"
IF = "1.3.6.1.2.1.2.2.1"            # ifTable
IFX = "1.3.6.1.2.1.31.1.1.1"        # ifXTable
ENT_NAME = "1.3.6.1.2.1.47.1.1.1.1.7"
ENT_CLASS = "1.3.6.1.2.1.47.1.1.1.1.5"
HW_ENT = "1.3.6.1.4.1.2011.5.25.31.1.1.1.1"    # hwEntityStateTable: .5 CPU %, .7 memory %, .11 temperature C
HW_OPT = "1.3.6.1.4.1.2011.5.25.31.1.1.3.1"    # hwOpticalModuleInfoTable (powers in 0.01 dBm)
HW_FAN = "1.3.6.1.4.1.2011.5.25.31.1.1.10.1"   # hwFanStatusTable: .5 speed %
HW_BGP_PEER = "1.3.6.1.4.1.2011.5.25.177.1.1.2.1"   # .2 remote AS, .4 address, .5 state, .7 up time (s)
HW_BGP_ROUTES = "1.3.6.1.4.1.2011.5.25.177.1.1.3.1"  # .1 prefixes received
BGP_PEER = "1.3.6.1.2.1.15.3.1"     # BGP4-MIB, IPv4 peers only (fallback)
ARP = "1.3.6.1.2.1.4.22.1"          # ipNetToMediaTable: .2 MAC, .4 type
IPADDR = "1.3.6.1.2.1.4.20.1"       # ipAddrTable: .2 ifIndex, .3 mask
LLDP_REM = "1.0.8802.1.1.2.1.4.1.1"  # .7 port id, .8 port description, .9 system name
LLDP_LOC_PORT = "1.0.8802.1.1.2.1.3.7.1.3"
BGP_STATES = {1: "Idle", 2: "Connect", 3: "Active", 4: "OpenSent", 5: "OpenConfirm", 6: "Established"}


class SshRequired(DeviceError):
    """This page needs the router CLI: the browser asks for an SSH login and tries again."""
    ssh_required = True


def _plen(mask):
    return sum(bin(int(x)).count("1") for x in mask.split(".")) if mask else 32


def _uptime(ticks):
    s = int(ticks or 0) // 100
    d, h, m = s // 86400, s // 3600 % 24, s // 60 % 60
    return f"{d} day{'s' if d != 1 else ''}, {h} hour{'s' if h != 1 else ''}, {m} minute{'s' if m != 1 else ''}"


def _updown(sec):
    """Seconds -> the CLI's Up/Down style: 10d04h or 04:12:33."""
    if not sec:
        return "0000h00m"
    sec = int(sec)
    if sec >= 86400:
        return f"{sec // 86400}d{sec // 3600 % 24:02d}h"
    return f"{sec // 3600:02d}:{sec // 60 % 60:02d}:{sec % 60:02d}"


def _speed(mbps):
    if not mbps:
        return ""
    return f"{mbps // 1000}G" if mbps >= 1000 and mbps % 1000 == 0 else f"{mbps}M"


def _ip_from_index(idx):
    """Huawei BGP table index <inst>.<afi>.<safi>.<type>.<len>.<address bytes> -> peer address."""
    parts = idx.split(".")
    if len(parts) < 6:
        return idx
    n = int(parts[4])
    b = [int(x) for x in parts[5:5 + n]]
    if n == 4:
        return ".".join(map(str, b))
    if n == 16:
        import ipaddress
        return str(ipaddress.IPv6Address(bytes(b)))
    return idx


class SnmpRouter(Router):
    via = "SNMP"

    def __init__(self, host, snmp, ssh_port=22):
        super().__init__(host)
        self.ssh_port = ssh_port
        self.ssh = None
        self.ssh_user = ""
        try:
            self.snmp = SnmpClient(host, user=snmp["user"], auth_proto=snmp["auth_proto"], auth_key=snmp["auth_key"],
                                   priv_proto=snmp["priv_proto"], priv_key=snmp["priv_key"], port=snmp.get("port", 161))
            got = self.snmp.get(SYS + ".5.0")
        except SnmpError as exc:
            self._close_snmp()
            raise DeviceError(f"SNMP to {host} failed: {exc}") from exc
        if got.get(SYS + ".5.0") is None:
            self._close_snmp()
            raise DeviceError(f"SNMP to {host} gave no answer.")
        self.counters = {}       # ifIndex -> (t, in_octets, out_octets)
        self.util = {}           # ifIndex -> (in %, out %)
        self.start_poller()

    # ---- SSH, only when someone signs in with it ---------------------------
    @property
    def can_configure(self):
        return self.ssh is not None

    def attach_ssh(self, username, password):
        ssh = RealRouter(self.host, username, password, self.ssh_port, poll=False)
        old, self.ssh, self.ssh_user = self.ssh, ssh, username
        self.via = "SNMP + SSH"
        if old:
            old.close()
        self.cache.clear()

    def _need_ssh(self):
        if self.ssh is None:
            raise SshRequired("This page needs the router's command line. Sign in with your SSH login to open it.")
        return self.ssh

    def _run(self, command, read_timeout):
        return self._need_ssh()._run(command, read_timeout)

    def _configure(self, lines, commit):
        return self._need_ssh()._configure(lines, commit)

    def _exec(self, command):
        return self._need_ssh()._exec(command)

    def execute(self, command):
        self._need_ssh()
        return super().execute(command)

    def _close_snmp(self):
        try:
            self.snmp.close()
        except Exception:
            pass

    def close(self):
        super().close()
        if self.ssh:
            self.ssh.close()
        self._close_snmp()

    # ---- SNMP reads ---------------------------------------------------------
    def walk(self, oid, ttl=4, reps=40):
        key = "snmp:" + oid
        hit = self.cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        try:
            out = self.snmp.walk(oid, reps=reps)
        except SnmpError as exc:
            raise DeviceError(f"SNMP: {exc}") from exc
        self.cache[key] = (time.time(), out)
        return out

    def get(self, *oids):
        try:
            return self.snmp.get(*oids)
        except SnmpError as exc:
            raise DeviceError(f"SNMP: {exc}") from exc

    def entities(self):
        names = self.walk(ENT_NAME, 600)
        return {idx: text(v) for idx, v in names.items()}

    def if_table(self, ttl=8):
        names = self.walk(IFX + ".1", 300)
        cols = {"admin": self.walk(IF + ".7", ttl), "oper": self.walk(IF + ".8", ttl),
                "in_err": self.walk(IF + ".14", ttl), "out_err": self.walk(IF + ".20", ttl),
                "alias": self.walk(IFX + ".18", 60), "speed": self.walk(IFX + ".15", 60),
                "mtu": self.walk(IF + ".4", 300), "mac": self.walk(IF + ".6", 300)}
        rows = {}
        for idx, name in names.items():
            rows[idx] = {"name": text(name), **{k: v.get(idx) for k, v in cols.items()}}
        return rows

    def if_index(self, name):
        return next((i for i, v in self.walk(IFX + ".1", 300).items() if text(v) == name), None)

    def _cpu_mem(self):
        cpu, mem = self.walk(HW_ENT + ".5", 3), self.walk(HW_ENT + ".7", 3)
        pick = lambda col: col.get("16777216") or max((v for v in col.values() if isinstance(v, int)), default=None)
        return pick(cpu), pick(mem)

    def temps(self, ttl=30):
        names = self.entities()
        rows = []
        for idx, t in self.walk(HW_ENT + ".11", ttl).items():
            if not t:
                continue
            name = names.get(idx, idx)
            board, _, slot = name.rpartition(" ")
            rows.append({"slot": slot if board else "", "board": board or name, "sensor": "", "status": "Normal",
                         "temp": t, "minor": None, "major": None})
        return rows

    # ---- poller: CPU, memory, temperature, every interface's traffic --------
    def _poll_once(self, t, tick):
        cpu, mem = self._cpu_mem()
        self.history["cpu"].append((t, cpu))
        self.history["memory"].append((t, mem))
        if tick % 6 == 0:
            temps = self.temps(0)
            if temps:
                self.history["temperature"].append((t, max(r["temp"] for r in temps)))
        if tick % 2 == 0:
            names = self.walk(IFX + ".1", 300)
            ins, outs, speeds = self.walk(IFX + ".6", 0), self.walk(IFX + ".10", 0), self.walk(IFX + ".15", 60)
            for idx, raw in names.items():
                name, i, o = text(raw), ins.get(idx), outs.get(idx)
                if i is None or o is None:
                    continue
                prev = self.counters.get(idx)
                self.counters[idx] = (t, i, o)
                if not prev or t <= prev[0]:
                    continue
                dt = t - prev[0]
                rx, tx = max(0, i - prev[1]) * 8 / dt, max(0, o - prev[2]) * 8 / dt
                self.history["bps:" + name].append((t, round(rx), round(tx)))
                cap = (speeds.get(idx) or 0) * 1e6
                ui, uo = (round(rx / cap * 100, 2), round(tx / cap * 100, 2)) if cap else (0.0, 0.0)
                self.util[idx] = (ui, uo)
                self.history["util:" + name].append((t, ui, uo))

    # ---- views ----------------------------------------------------------------
    def sysname(self):
        return text(self.get(SYS + ".5.0").get(SYS + ".5.0")) or self.host

    def _brief(self):
        rows = []
        for idx, r in self.if_table().items():
            name = r["name"]
            up = r["oper"] == 1
            ui, uo = self.util.get(idx, (0.0, 0.0))
            rows.append({"name": name, "phy": "*down" if r["admin"] == 2 else ("up" if up else "down"),
                         "protocol": "up" if up else "down", "in_util": ui, "out_util": uo,
                         "in_errors": r["in_err"] or 0, "out_errors": r["out_err"] or 0,
                         "admin_down": r["admin"] == 2, "type": P.interface_type(name),
                         "parent": name.split(".")[0] if "." in name else "", "_idx": idx, "_r": r})
        return rows

    def overview(self):
        sysd = self.get(SYS + ".1.0", SYS + ".3.0", SYS + ".5.0")
        descr = text(sysd.get(SYS + ".1.0"))
        model = re.search(r"HUAWEI\s+(.+?)\s*$", descr, re.M)
        ver = re.search(r"Version\s+([\d.]+)\s*\((?:.*?\s)?(V\w+)\)", descr)
        ifs = self._brief()
        phys = [i for i in ifs if i["type"] in ("ethernet", "eth-trunk", "management")]
        cpu, mem = self._cpu_mem()
        temps = self.temps()
        bgp = self.bgp_peers()
        alarms = {"total": None, "critical": None}
        if self.ssh:
            try:
                a = P.parse_alarms(self.cached("display alarm active", 10))
                alarms = {"total": len(a), "critical": sum(1 for x in a if x["level"] in ("Critical", "Major"))}
            except DeviceError:
                pass
        for i in ifs:
            i.pop("_r"), i.pop("_idx")
        return {
            "host": self.host, "demo": False, "via": self.via,
            "model": model.group(1) if model else "NetEngine 8000", "uptime": _uptime(sysd.get(SYS + ".3.0")),
            "vrp_version": ver.group(1) if ver else "", "software": ver.group(2) if ver else "", "patch": "",
            "cpu": cpu, "memory": {"percent": mem, "total_kb": None, "used_kb": None},
            "sysname": text(sysd.get(SYS + ".5.0")) or self.host,
            "interfaces": {"total": len(phys), "up": sum(1 for i in phys if i["phy"] == "up"),
                           "admin_down": sum(1 for i in phys if i["admin_down"]), "all": len(ifs)},
            "bgp": bgp, "alarms": alarms,
            "max_temp": max((r["temp"] for r in temps), default=None),
            "top_interfaces": sorted([i for i in ifs if i["phy"] == "up" and i["type"] != "loopback"],
                                     key=lambda i: -(i["in_util"] + i["out_util"]))[:6],
            "poll_error": self.poll_error,
        }

    def resources(self, since=0):
        _, mem = self._cpu_mem()
        return {"cpu": self.series("cpu", since), "memory": self.series("memory", since),
                "temperature": self.series("temperature", since),
                "memory_detail": {"percent": mem, "total_kb": None, "used_kb": None},
                "temps": self.temps(), "poll_seconds": POLL_SECONDS, "poll_error": self.poll_error, "via": self.via}

    def _ip_map(self):
        ifidx, masks = self.walk(IPADDR + ".2", 60), self.walk(IPADDR + ".3", 60)
        out = {}
        for ip, idx in ifidx.items():
            out.setdefault(str(idx), []).append(f"{ip}/{_plen(masks.get(ip))}")
        return out

    def interfaces(self):
        ips = self._ip_map()
        cfg = self.interface_config() if self.ssh else {}
        rows = []
        for row in self._brief():
            idx, r = row.pop("_idx"), row.pop("_r")
            addrs = ips.get(idx, [])
            lines = cfg.get(row["name"], [])
            conf = sorted((l for l in lines if l.startswith("ip address ") and len(l.split()) >= 4), key=lambda l: l.endswith(" sub"))
            conf = [_cidr(l) for l in conf]
            if conf:    # the configuration says which address is primary and which are "sub"; SNMP does not
                addrs = conf
            row.update(ip=addrs[0] if addrs else "", ipv6=ipv6_addrs(lines), vpn="", description=text(r["alias"]),
                       vlan=P.vlan_from_config(lines) if lines else "", mtu=str(r["mtu"] or ""),
                       l2="portswitch" in lines, secondary=addrs[1:],
                       util_history=[[x[1], x[2]] for x in list(self.history.get("util:" + row["name"], ()))[-30:]])
            rows.append(row)
        return rows

    def interface_detail(self, name):
        idx = self.if_index(name)
        if idx is None:
            raise DeviceError(f"No interface {name}.")
        g = self.get(*[f"{c}.{idx}" for c in (IF + ".4", IF + ".6", IF + ".7", IF + ".8", IF + ".9", IF + ".13", IF + ".14",
                                              IFX + ".6", IFX + ".7", IFX + ".10", IFX + ".11", IFX + ".15", IFX + ".18")])
        v = lambda c: g.get(f"{c}.{idx}")
        bps = list(self.history.get("bps:" + name, ()))
        ips = self._ip_map().get(idx, [])
        up = v(IF + ".8") == 1
        d = {"name": name, "state": "Administratively DOWN" if v(IF + ".7") == 2 else ("UP" if up else "DOWN"),
             "protocol": "UP" if up else "DOWN", "description": text(v(IFX + ".18")), "mtu": v(IF + ".4"),
             "mac": mac(v(IF + ".6")), "ip": ips[0] if ips else "", "speed": _speed(v(IFX + ".15")), "duplex": "",
             "last_up": "", "last_down": "",
             "in_rate_bps": bps[-1][1] if bps else None, "out_rate_bps": bps[-1][2] if bps else None,
             "in_pps": None, "out_pps": None, "vlan": "",
             "input_bytes": v(IFX + ".6"), "input_packets": v(IFX + ".7"),
             "output_bytes": v(IFX + ".10"), "output_packets": v(IFX + ".11"),
             "crc_errors": v(IF + ".14"), "in_discards": v(IF + ".13")}
        d["config"] = self.interface_config().get(name, []) if self.ssh else []
        d["optics"] = self.optics().get(name)
        d["raw"] = "\n".join(f"{k}: {d[k]}" for k in ("state", "protocol", "description", "speed", "mtu", "mac", "ip",
                                                       "input_bytes", "input_packets", "output_bytes", "output_packets",
                                                       "crc_errors", "in_discards") if d[k] not in (None, "")) + \
            "\n(read over SNMP" + ("" if self.ssh else "; sign in with SSH to see the configuration") + ")"
        return d

    def traffic(self, names, since=0):
        return {n: self.series("bps:" + n, since) for n in names}

    def optics(self):
        names = self.entities()
        cols = {k: self.walk(f"{HW_OPT}.{c}", 30) for k, c in
                (("temp", 5), ("rx", 8), ("tx", 9), ("rx_low", 13), ("rx_high", 14), ("tx_low", 15), ("tx_high", 16),
                 ("vendor", 24), ("part", 25), ("wavelength", 2))}
        out = {}
        for idx in cols["rx"]:
            name = names.get(idx, idx)
            dbm = lambda k: round(cols[k][idx] / 100, 2) if isinstance(cols[k].get(idx), int) else None
            out[name] = {"rx_dbm": dbm("rx"), "tx_dbm": dbm("tx"), "rx_low": dbm("rx_low"), "rx_high": dbm("rx_high"),
                         "tx_low": dbm("tx_low"), "tx_high": dbm("tx_high"), "temp": cols["temp"].get(idx),
                         "vendor": text(cols["vendor"].get(idx)), "part": text(cols["part"].get(idx)),
                         "wavelength": cols["wavelength"].get(idx)}
        return out

    def bgp_peers(self):
        local = self.get("1.3.6.1.2.1.15.2.0", "1.3.6.1.2.1.15.4.0")
        peers = []
        try:
            state, asn, up = self.walk(HW_BGP_PEER + ".5", 10), self.walk(HW_BGP_PEER + ".2", 60), self.walk(HW_BGP_PEER + ".7", 10)
            pref = self.walk(HW_BGP_ROUTES + ".1", 10)
            for idx, st in state.items():
                peers.append({"peer": _ip_from_index(idx), "as": str(asn.get(idx, "")), "msg_rcvd": None, "msg_sent": None,
                              "out_q": None, "up_down": _updown(up.get(idx)), "state": BGP_STATES.get(st, str(st)),
                              "prefixes": pref.get(idx, 0) or 0})
        except DeviceError:
            peers = []
        if not peers:   # standard BGP4-MIB (IPv4 peers only)
            state, asn, up = self.walk(BGP_PEER + ".2", 10), self.walk(BGP_PEER + ".9", 60), self.walk(BGP_PEER + ".16", 10)
            rin, rout = self.walk(BGP_PEER + ".12", 10), self.walk(BGP_PEER + ".13", 10)
            for ip, st in state.items():
                peers.append({"peer": ip, "as": str(asn.get(ip, "")), "msg_rcvd": rin.get(ip), "msg_sent": rout.get(ip),
                              "out_q": None, "up_down": _updown(up.get(ip)), "state": BGP_STATES.get(st, str(st)), "prefixes": 0})
        return {"router_id": local.get("1.3.6.1.2.1.15.4.0") or "", "local_as": str(local.get("1.3.6.1.2.1.15.2.0") or ""),
                "total": len(peers), "established": sum(1 for p in peers if p["state"] == "Established"), "peers": peers}

    def bgp(self):
        b = self.bgp_peers()
        b["config"] = P.parse_config_blocks(self.cached("display current-configuration configuration bgp", 10), "bgp") if self.ssh else {}
        return b

    def arp(self):
        names = {i: text(v) for i, v in self.walk(IFX + ".1", 300).items()}
        macs, types = self.walk(ARP + ".2", 10), self.walk(ARP + ".4", 10)
        rows = []
        for idx, m in macs.items():
            ifidx, _, ip = idx.partition(".")
            rows.append({"ip": ip, "mac": mac(m) or "Incomplete", "expire": "",
                         "type": {3: "D", 4: "S", 1: "O", 2: "I"}.get(types.get(idx), ""), "interface": names.get(ifidx, ifidx),
                         "vpn": "", "vlan": ""})
        static = []
        if self.ssh:
            text_ = self.cached("display current-configuration | include arp static", 10)
            static = [l.strip() for l in text_.splitlines() if l.strip().startswith("arp static")]
        return {"entries": rows, "static": static}

    def neighbors(self):
        loc = {i: text(v) for i, v in self.walk(LLDP_LOC_PORT, 300).items()}
        port, desc, sysn = (self.walk(LLDP_REM + c, 10, reps=10) for c in (".7", ".8", ".9"))
        rows = []
        for idx, p in port.items():
            parts = idx.split(".")
            local = loc.get(parts[1], parts[1]) if len(parts) >= 3 else idx
            rows.append({"local": local, "exptime": "", "neighbor_port": text(p) or text(desc.get(idx)),
                         "neighbor": text(sysn.get(idx)) or "-"})
        return {"lldp": rows}

    def hardware(self):
        if self.ssh:
            h = super().hardware()
            h["optics"] = self.optics()
            return h
        names, classes = self.entities(), self.walk(ENT_CLASS, 600)
        boards = []
        for idx, name in names.items():
            if classes.get(idx) in (6, 7, 9):   # power supply, fan, module
                kind, _, slot = name.rpartition(" ")
                boards.append({"slot": slot, "type": kind or name, "online": "Present", "register": "", "status": "", "role": ""})
        fans = self.walk(HW_FAN + ".5", 60)
        return {"boards": boards, "temperature": self.temps(),
                "power": "\n".join(f"{n}: present" for i, n in names.items() if classes.get(i) == 6) or "(no power supplies reported over SNMP)",
                "fan": "\n".join(f"Fan {i.split('.')[0]}: {v}% speed" for i, v in fans.items()) or "(no fans reported over SNMP)",
                "elabel": "(sign in with SSH to read the electronic labels)", "optics": self.optics()}
