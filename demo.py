"""A simulated NE8000 M8 for demo mode. It keeps a small configuration model,
applies the same command lines the GUI sends to a real router, and prints
VRP8-style output from that model, so every page (and every change) works
without hardware."""
import ipaddress
import math
import random
import re
import time

from device import Router
import parsers as P

BOOT = time.time() - (42 * 86400 + 7 * 3600 + 13 * 60)


def mask_len(mask):
    if mask.isdigit():
        return int(mask)
    return sum(bin(int(o)).count("1") for o in mask.split("."))


def fmt_uptime(seconds):
    d, r = divmod(int(seconds), 86400)
    h, r = divmod(r, 3600)
    return f"{d} days, {h} hours, {r // 60} minutes"


def hhmm(seconds):
    h, r = divmod(int(seconds), 3600)
    return f"{h:04d}h{r // 60:02d}m"


class DemoRouter(Router):
    demo = True

    def __init__(self, host="demo"):
        super().__init__(host)
        self.sysname_ = "NE8000-M8"
        self.cpu = 14.0
        self.t0 = time.time()
        I = self._iface
        self.ifs = {}
        I("GigabitEthernet0/0/0", desc="MGMT", ips=[("192.168.0.10", 24)], vpn="__LOCAL_OAM_VPN__", base=2e5)
        I("GigabitEthernet0/1/0", desc="To-Customer-A", ips=[("10.10.1.1", 30)], base=230e6, qos=["qos car cir 300000 pir 350000 green pass yellow pass red discard inbound"])
        I("GigabitEthernet0/1/1", desc="To-Customer-B", ips=[("10.10.2.1", 30)], base=410e6, errors=2)
        I("GigabitEthernet0/1/2", desc="Spare", cable=False)
        I("GigabitEthernet0/1/3", enabled=False)
        I("GigabitEthernet0/1/4", desc="Access-Switch", base=95e6)
        I("GigabitEthernet0/1/4.100", desc="Customer-C VLAN100", ips=[("10.20.100.1", 24)], vlan=100, base=40e6)
        I("GigabitEthernet0/1/4.200", desc="Customer-D VLAN200", ips=[("10.20.200.1", 24)], vlan=200, vpn="CUST-D", base=25e6)
        I("GigabitEthernet0/1/6", desc="Reseller aggregation (Nairobi)", base=470e6)
        I("GigabitEthernet0/1/6.301", desc="Reseller AlphaNet", ips=[("102.216.117.1", 30)], vlan=301, base=180e6,
          qos=["qos-profile 600Mbps inbound identifier none", "qos-profile 600Mbps outbound identifier none"])
        I("GigabitEthernet0/1/6.302", desc="Reseller BetaLink", ips=[("102.216.117.5", 30)], vlan=302, base=90e6)
        I("GigabitEthernet0/1/6.303", desc="Reseller Gamma Cable 552903", ips=[("10.99.3.1", 30)], vlan=303, base=120e6,
          qos=["qos-profile 500Mbps inbound identifier none", "qos-profile 500Mbps outbound identifier none"])
        I("GigabitEthernet0/1/6.304", desc="Shared reseller VLAN", ips=[("102.216.117.33", 27)], vlan=304, base=70e6)
        I("GigabitEthernet0/1/6.399", desc="TEST ne8000test", ips=[("10.99.99.1", 30)], vlan=399, base=5e6)
        I("GigabitEthernet0/1/5", desc="L2-Trunk", base=60e6, l2=True, trunk="10 20 30")
        I("GigabitEthernet0/1/7", cable=False)
        I("100GE0/2/0", desc="Uplink-Core-1", ips=[("172.16.0.1", 31)], base=12e9, speed="100G", qos=["port shaping 80000"])
        I("100GE0/2/1", desc="Uplink-Core-2", ips=[("172.16.0.3", 31)], base=9e9, speed="100G")
        I("LoopBack0", desc="Router-ID", ips=[("1.1.1.1", 32)])
        I("Vlanif10", desc="Mgmt-VLAN", ips=[("10.0.10.1", 24)], base=1e6)
        I("NULL0")
        self.vlan_db = {1: "", 10: "Mgmt", 20: "Voice", 30: "CCTV"}
        self.static = ["ip route-static 0.0.0.0 0.0.0.0 172.16.0.0 description Default-Core1",
                       "ip route-static 0.0.0.0 0.0.0.0 172.16.0.2 preference 70 description Default-Core2",
                       "ip route-static 203.0.113.0 255.255.255.0 NULL0 description Blackhole-aggregate"]
        self.arp_static = ["arp static 10.10.1.2 00e0-fc11-2201"]
        self.bgp_as = "65001"
        self.bgp_peers = {
            "2.2.2.2": {"as": "65001", "desc": "RR-Core-1", "ignore": False, "state": "Established", "pref": 1204, "up": BOOT, "ci": "LoopBack0"},
            "3.3.3.3": {"as": "65001", "desc": "RR-Core-2", "ignore": False, "state": "Established", "pref": 1198, "up": BOOT, "ci": "LoopBack0"},
            "10.10.1.2": {"as": "64512", "desc": "Customer-A", "ignore": False, "state": "Active", "pref": 0, "up": time.time() - 72, "ci": ""},
        }
        self.bgp_networks = ["10.10.0.0 16", "203.0.113.0 24"]
        self.ospf_networks = [("1", "0.0.0.0", "172.16.0.0 0.0.0.1"), ("1", "0.0.0.0", "172.16.0.2 0.0.0.1"), ("1", "0.0.0.0", "1.1.1.1 0.0.0.0")]
        self.isis_procs = {"1": {"level": "level-2", "cost_style": "wide", "nets": ["49.0001.0010.0000.0001.00"], "is_name": ""}}
        for n in ("100GE0/2/0", "100GE0/2/1", "LoopBack0"):
            self.ifs[n].update(isis="1", isis_p2p=n != "LoopBack0", isis_cost=10 if n != "LoopBack0" else None)
        self.users = {"admin": 3, "noc": 2, "readonly": 1}
        self.ntp = ["192.168.0.1"]
        self.snmp = [("read", "public-ro")]
        self.vpns = {"CUST-D": "65001:200", "__LOCAL_OAM_VPN__": "<not set>"}
        self.policies = {"LIMIT-GUEST": {"classifier": "LIMIT-GUEST_c", "acl": 3001, "cir": 50000}}
        self.queues = {}
        self.profiles = {"100Mbps": [100000, 100000], "300Mbps": [300000, 300000], "500Mbps": [500000, 500000],
                         "600Mbps": [600000, 600000], "1Gbps": [1000000, 1000000], "500MBPS": [1024, 512000]}
        self.extra_log = []
        self._backfill()
        self.start_poller()

    def _backfill(self):
        """Give the graphs 30 minutes of history so demo mode does not start empty."""
        now = time.time()
        cpu = 14.0
        for k in range(360, 0, -1):
            t = now - k * 5
            cpu = min(90, max(5, cpu + random.uniform(-2.5, 2.5) + (14 - cpu) * 0.1 + (25 if random.random() < 0.01 else 0)))
            self.history["cpu"].append((t, round(cpu)))
            self.history["memory"].append((t, round(36 + math.sin(t / 300))))
            if k % 6 == 0:
                self.history["temperature"].append((t, round(46 + 2 * math.sin(t / 200 + 1) + cpu * 0.05)))
        self.cpu = cpu

    def _iface(self, name, desc="", ips=(), enabled=True, cable=True, base=0.0, vlan=None, vpn="", speed="1G",
               errors=0, l2=False, trunk="", qos=()):
        self.ifs[name] = {"desc": desc, "ips": [(a, b, False) for a, b in ips], "enabled": enabled, "cable": cable,
                          "base": base, "vlan": vlan, "encap": "vlan-type", "vpn": vpn, "mtu": None, "speed": speed,
                          "errors": errors, "l2": l2, "trunk": trunk, "pvid": "", "qos": list(qos),
                          "phase": random.random() * 6.28, "in_b": random.randint(10**12, 10**14),
                          "out_b": random.randint(10**12, 10**14), "t": time.time(), "ospf_cost": None,
                          "isis": None, "isis_cost": None, "isis_p2p": False}

    # ---- traffic model ----------------------------------------------------
    def up(self, i):
        return i["enabled"] and i["cable"]

    def rate(self, name, direction, t=None):
        i = self.ifs[name]
        if not self.up(i) or not i["base"]:
            return 0.0
        t = t or time.time()
        k = 1.0 if direction == "in" else 0.78
        wave = 1 + 0.35 * math.sin(t / 45 + i["phase"]) + 0.15 * math.sin(t / 7 + i["phase"] * 2)
        return max(0.0, i["base"] * k * wave * random.uniform(0.92, 1.08))

    def tick_counters(self, name):
        i = self.ifs[name]
        now = time.time()
        dt = now - i["t"]
        i["in_b"] += int(self.rate(name, "in") * dt / 8)
        i["out_b"] += int(self.rate(name, "out") * dt / 8)
        i["t"] = now

    def _sample_interface(self, name, t):
        key = "bps:" + name
        if name in self.ifs and not self.history.get(key):
            for k in range(60, 0, -1):
                tt = t - k * 5
                self.history[key].append((tt, round(self.rate(name, "in", tt)), round(self.rate(name, "out", tt))))
        return super()._sample_interface(name, t)

    def capacity(self, i):
        return {"100G": 100e9, "10G": 10e9}.get(i["speed"], 1e9)

    # ---- command handlers -------------------------------------------------
    def _run(self, command, read_timeout):
        c = " ".join(command.split())
        cl = c.lower()
        if cl.startswith(("ping ", "tracert ")):
            return self.ping(c) if cl.startswith("ping") else self.tracert(c)
        if cl.startswith("display interface ") and cl not in ("display interface brief", "display interface description"):
            return self.show_interface(c.split(" ", 2)[2])
        if cl.startswith("display port-queue statistics interface"):
            return self.show_port_queue(c.split()[4])
        if cl.startswith("display bgp routing-table") and cl.endswith(" statistics"):
            text = self._run(c[: -len(" statistics")], read_timeout)
            return f" Total Number of Routes: {len(P.parse_bgp_routes(text))}"
        if re.match(r"display bgp routing-table \d", cl):
            return self.show_bgp_lookup(c.split()[3])
        if cl.startswith("display bgp routing-table peer"):
            return self.show_bgp_routes(c.split()[4], "advertised" in cl)
        if cl.startswith("display ip routing-table"):
            w = c.split()[3:]
            vpn = w[w.index("vpn-instance") + 1] if "vpn-instance" in w else None
            text = self.show_routes(vpn)
            if w and w[-1] == "statistics":
                rows = P.parse_routing_table(text)
                protos = ["DIRECT", "STATIC", "OSPF", "BGP", "UNR"]
                count = {k: sum(1 for r in rows if r["protocol"].upper().replace("IBGP", "BGP").replace("EBGP", "BGP") == k) for k in protos}
                return f"Summary Prefixes : {len(rows)}\nProto      total      active     added      deleted    freed\n" + \
                       "\n".join(f"{k:<11}{n:<11}{n:<11}{n:<11}0          0" for k, n in count.items())
            if "protocol" in w:
                want = w[w.index("protocol") + 1].lower()
                return "\n".join(l for l in text.splitlines() if not re.match(r"^\s*\S*\d+\.\d+\.\d+\.\d+/\d+\s", l) or
                                 re.search(r"\s(\w+)\s+\d+\s+\d+", l).group(1).lower().replace("ibgp", "bgp").replace("ebgp", "bgp") == want)
            ip = next((x for x in w if re.match(r"\d+\.\d+\.\d+\.\d+$", x)), None)
            if ip:
                return self.show_route_lookup(text, ip)
            return text
        if "| include" in cl:
            pat = c.split("| include", 1)[1].strip()
            return "\n".join(l for l in self.show_config().splitlines() if pat in l)
        fn = getattr(self, "show_" + re.sub(r"[^a-z0-9]+", "_", cl.replace("display ", "", 1)).strip("_"), None)
        if fn:
            return fn()
        return f"                ^\nError: Unrecognized command found at '^' position. (demo mode does not simulate '{c}')"

    def _exec(self, command):
        c = command.strip()
        if c.startswith("reset bgp "):
            peer = self.bgp_peers.get(c.split()[2])
            if peer and peer["state"] == "Established":
                peer["up"] = time.time()
            self.log("BGP/3/STATE_CHG_UPDOWN", f"The status of the peer {c.split()[2]} changed from ESTABLISHED to IDLE (manual reset).")
            return "Info: The BGP peer is reset."
        if c.startswith("reset isis all"):
            self.log("ISIS/6/ADJ_CHANGE", "The IS-IS adjacencies were reset by the GUI user.")
            return "Warning: The ISIS process will be reset. Continue? [Y/N]:Y\nInfo: Reset the ISIS process successfully."
        if c.startswith("reset counters interface"):
            return "Info: Reset the statistics on the interface successfully."
        if c == "save":
            return "Warning: The current configuration will be written to the device. Continue? [Y/N]:Y\nInfo: Save the configuration successfully."
        return "Info: Command executed (demo)."

    def _configure(self, lines, commit):
        snapshot = self._snapshot()
        out, view, ctx = [], "system", None
        for raw in lines:
            line = " ".join(raw.split())
            prompt = f"[*{self.sysname_}{'-' + ctx if ctx else ''}]"
            out.append(prompt + line)
            err = self._apply(line, view, ctx)
            if isinstance(err, tuple):
                view, ctx = err
            elif err:
                out.append(f"Error: {err}")
        if commit and any(l.startswith("Error:") for l in out):
            self._restore(snapshot)
            out.append(f"[*{self.sysname_}]abort\nInfo: Nothing was committed because a line was rejected.")
        elif commit:
            out.append(f"[*{self.sysname_}]commit\n[~{self.sysname_}]")
            self.log("CFG/4/CFG_COMMIT", "The configuration was committed by the GUI user.")
        else:
            self._restore(snapshot)
            out.append(f"[*{self.sysname_}]abort\nInfo: The configuration was not committed (syntax check only).")
        return "\n".join(out)

    def _snapshot(self):
        import copy
        return copy.deepcopy({k: v for k, v in self.__dict__.items()
                              if k in ("ifs", "vlan_db", "static", "arp_static", "bgp_peers", "bgp_networks", "ospf_networks", "isis_procs",
                                       "users", "ntp", "snmp", "vpns", "policies", "queues", "profiles", "sysname_")})

    def _restore(self, snap):
        self.__dict__.update(snap)

    def _apply(self, line, view, ctx):
        """Apply one config line. Returns (view, ctx) to change view, an error string, or None."""
        w = line.split()
        if line == "quit":
            return ("system", None)
        if view == "qosprofile":
            if w[:2] == ["car", "cir"]:
                cir = int(w[2]); pir = int(w[w.index("pir") + 1]) if "pir" in w else cir
                self.profiles[ctx] = [cir, pir]
                return None
            view, ctx = "system", None
        if w[0] == "qos-profile" and len(w) == 2 and view == "system":
            self.profiles.setdefault(w[1], [None, None])
            return ("qosprofile", w[1])
        if w[0] == "interface" and len(w) == 2:
            name = w[1]
            if name not in self.ifs:
                parent = name.split(".")[0]
                if "." in name and parent not in self.ifs:
                    return f"The interface {parent} does not exist."
                if "." not in name and not re.match(r"(?i)^(LoopBack|Vlanif|Tunnel|Eth-Trunk)\d+$", name):
                    return "Wrong parameter found at '^' position."
                base = 0.0
                if "." in name:
                    base = self.ifs[parent]["base"] * 0.2
                if name.lower().startswith("vlanif"):
                    name = "Vlanif" + name[6:]
                if name.lower().startswith("loopback"):
                    name = "LoopBack" + name[8:]
                self._iface(name, base=base)
            return ("interface", name)
        if view == "interface" or (ctx and ctx in self.ifs):
            if w[0] in ("interface",):
                pass
            else:
                return self._apply_if(ctx, line, w)
        if w[0] == "undo" and len(w) >= 3 and w[1] == "interface":
            if self.ifs.pop(w[2], None) is None:
                return "The interface does not exist."
            return None
        if w[0] == "vlan" and len(w) == 2 and w[1].isdigit():
            self.vlan_db.setdefault(int(w[1]), "")
            return ("vlan", w[1])
        if w[0] == "vlan" and w[1] == "batch":
            for v in w[2:]:
                if v.isdigit():
                    self.vlan_db.setdefault(int(v), "")
            return None
        if view == "vlan" and w[0] == "description":
            self.vlan_db[int(ctx)] = line.split(" ", 1)[1]
            return None
        if line.startswith("undo vlan "):
            if self.vlan_db.pop(int(w[2]), None) is None:
                return "The VLAN does not exist."
            return None
        if line.startswith("ip route-static"):
            self.static.append(line)
            return None
        if line.startswith("undo ip route-static"):
            target = line[5:]
            for s in list(self.static):
                if re.sub(r"\s+description\s+.*$", "", s) == target:
                    self.static.remove(s)
                    return None
            return "The static route does not exist."
        if line.startswith("arp static"):
            self.arp_static.append(line)
            return None
        if line.startswith("undo arp static"):
            before = len(self.arp_static)
            self.arp_static = [a for a in self.arp_static if a.split()[2] != w[3]]
            return None if len(self.arp_static) < before else "The static ARP entry does not exist."
        if w[0] == "bgp":
            if w[1] != self.bgp_as:
                return f"BGP is already running with AS {self.bgp_as}."
            return ("bgp", "bgp")
        if view in ("bgp", "bgp-af"):
            return self._apply_bgp(line, w, view)
        if w[0] == "isis" and len(w) == 2 and w[1].isdigit() and view == "system":
            self.isis_procs.setdefault(w[1], {"level": "level-1-2", "cost_style": "narrow", "nets": [], "is_name": ""})
            return ("isis", "isis-" + w[1])
        if view == "isis":
            d = self.isis_procs[ctx.split("-")[1]]
            if w[0] == "is-level":
                d["level"] = w[1]
            elif w[0] == "cost-style":
                d["cost_style"] = w[1]
            elif w[0] == "network-entity":
                if w[1] in d["nets"]:
                    return "The network entity already exists."
                if len(d["nets"]) >= 3:
                    return "A maximum of 3 network entities can be configured."
                d["nets"].append(w[1])
            elif w[0] == "is-name":
                d["is_name"] = w[1]
            return None
        if w[0] == "ospf" and len(w) == 2:
            return ("ospf", "ospf-" + w[1])
        if view in ("ospf", "ospf-area"):
            if w[0] == "area":
                return ("ospf-area", ctx.split("-area")[0] + "-area-" + w[1])
            proc, area = ctx.split("-")[1], ctx.split("-area-")[-1]
            if w[0] == "network":
                self.ospf_networks.append((proc, area, f"{w[1]} {w[2]}"))
                return None
            if w[:2] == ["undo", "network"]:
                key = (proc, area, f"{w[2]} {w[3]}")
                if key in self.ospf_networks:
                    self.ospf_networks.remove(key)
                    return None
                return "The network does not exist."
            return None
        if line == "aaa":
            return ("aaa", "aaa")
        if view == "aaa":
            m = re.match(r"^(undo )?local-user (\S+)(?: (password|level) (\S+)(?: (\S+))?)?", line)
            if m:
                if m.group(1):
                    self.users.pop(m.group(2), None)
                elif m.group(3) == "level":
                    self.users[m.group(2)] = int(m.group(4))
                else:
                    self.users.setdefault(m.group(2), 3)
            return None
        if w[0] == "sysname":
            self.sysname_ = w[1]
            return None
        if line.startswith("ntp-service unicast-server"):
            self.ntp.append(w[2])
            return None
        if line.startswith("undo ntp-service unicast-server"):
            if w[3] in self.ntp:
                self.ntp.remove(w[3])
            return None
        if line.startswith("snmp-agent community"):
            self.snmp.append((w[2], w[-1]))
            return None
        if w[0] == "ip" and w[1] == "vpn-instance":
            self.vpns.setdefault(w[2], "<not set>")
            return ("vpn", w[2])
        if view == "vpn" and w[0] == "route-distinguisher":
            self.vpns[ctx] = w[1]
            return None
        if line.startswith("undo ip vpn-instance"):
            self.vpns.pop(w[3], None)
            return None
        if w[0] == "traffic" and w[1] == "policy":
            self.policies.setdefault(w[2], {"classifier": "", "acl": "", "cir": ""})
            return ("policy", w[2])
        if view == "policy" and w[0] == "classifier":
            self.policies[ctx]["classifier"] = w[1]
            return None
        if w[0] == "traffic" and w[1] == "behavior":
            return ("behavior", w[2])
        if view == "behavior" and w[0] == "car":
            for pol in self.policies.values():
                if pol["classifier"] == ctx[:-2] + "_c" or not pol["cir"]:
                    pol["cir"] = int(w[2])
            self._pending_cir = int(w[2])
            return None
        if w[0] == "acl":
            return ("acl", w[2])
        if w[0] == "traffic" and w[1] == "classifier":
            return ("classifier", w[2])
        return None

    def _apply_if(self, name, line, w):
        i = self.ifs[name]
        if line == "shutdown":
            i["enabled"] = False
            self.log("IFNET/2/linkDown_active", f"The interface status changes. (ifName={name}, AdminStatus=DOWN)")
        elif line == "undo shutdown":
            i["enabled"] = True
            self.log("IFNET/2/linkDown_clear", f"The interface status changes. (ifName={name}, AdminStatus=UP)")
        elif w[0] == "description":
            i["desc"] = line.split(" ", 1)[1]
        elif line == "undo description":
            i["desc"] = ""
        elif w[0] == "mtu":
            i["mtu"] = int(w[1])
        elif w[:2] == ["ip", "address"]:
            entry = (w[2], mask_len(w[3]), "sub" in w)
            if not entry[2]:
                i["ips"] = [x for x in i["ips"] if x[2]]
            i["ips"].append(entry)
        elif w[:3] == ["undo", "ip", "address"]:
            before = len(i["ips"])
            i["ips"] = [x for x in i["ips"] if not (x[0] == w[3] and x[1] == mask_len(w[4]))]
            if len(i["ips"]) == before:
                return "The address does not exist."
        elif w[:2] == ["ip", "binding"]:
            if w[3] not in self.vpns:
                return f"The VPN instance {w[3]} does not exist."
            i["vpn"], i["ips"] = w[3], []
        elif w[0] == "vlan-type":
            i["vlan"], i["encap"] = int(w[2]), "vlan-type"
        elif w[:3] == ["dot1q", "termination", "vid"]:
            i["vlan"], i["encap"] = int(w[3]), "termination"
        elif w[:2] == ["qinq", "termination"]:
            i["vlan"], i["encap"] = int(w[3]), f"qinq {w[3]}.{w[5]}"
        elif line == "portswitch":
            i["l2"] = True
            i["ips"] = []
        elif line == "undo portswitch":
            i["l2"], i["trunk"] = False, ""
        elif w[:3] == ["port", "trunk", "allow-pass"]:
            i["trunk"] = (i["trunk"] + " " + " ".join(w[4:])).strip()
        elif w[:4] == ["undo", "port", "trunk", "allow-pass"]:
            rm = " ".join(w[5:])
            i["trunk"] = " ".join(x for x in i["trunk"].split() if x not in rm.split())
        elif w[:3] == ["port", "default", "vlan"]:
            i["trunk"], i["pvid"] = "", w[3]
        elif w[:4] == ["port", "trunk", "pvid", "vlan"]:
            i["pvid"] = w[4]
        elif w[:2] == ["qos", "car"]:
            i["qos"] = [q for q in i["qos"] if not (q.startswith("qos car") and q.endswith(w[-1]))] + [line]
        elif w[:3] == ["undo", "qos", "car"]:
            i["qos"] = [q for q in i["qos"] if not (q.startswith("qos car") and q.endswith(w[3]))]
        elif w[:2] == ["port", "shaping"]:
            i["qos"] = [q for q in i["qos"] if not q.startswith("port shaping")] + [line]
        elif line == "undo port shaping":
            i["qos"] = [q for q in i["qos"] if not q.startswith("port shaping")]
        elif w[0] == "port-queue":
            i["qos"] = [q for q in i["qos"] if not q.startswith(f"port-queue {w[1]} ")] + [line]
        elif w[0] == "qos-profile" and len(w) >= 3:
            if w[1] not in self.profiles:
                return f"The QoS profile {w[1]} does not exist."
            if any(q.startswith("qos-profile ") and q.split()[2] == w[2] for q in i["qos"]):
                return f"A QoS profile is already applied in the {w[2]} direction. Undo it first."
            i["qos"].append(line)
        elif w[:2] == ["undo", "qos-profile"]:
            # like the real router: "undo qos-profile inbound", no profile name
            if len(w) != 3 or w[2] not in ("inbound", "outbound"):
                return "Unrecognized command found at '^' position."
            before = len(i["qos"])
            i["qos"] = [q for q in i["qos"] if not (q.startswith("qos-profile ") and q.split()[2] == w[2])]
            if len(i["qos"]) == before:
                return "The QoS profile is not applied in that direction."
        elif w[0] == "traffic-policy":
            if w[1] not in self.policies:
                return f"The traffic policy {w[1]} does not exist."
            i["qos"] = [q for q in i["qos"] if not (q.startswith("traffic-policy") and q.endswith(w[2]))] + [line]
        elif w[:2] == ["undo", "traffic-policy"]:
            i["qos"] = [q for q in i["qos"] if not (q.startswith("traffic-policy") and q.endswith(w[2]))]
        elif w[:2] == ["ospf", "cost"]:
            i["ospf_cost"] = int(w[2])
        elif w[:2] == ["isis", "enable"]:
            proc = w[2] if len(w) > 2 else "1"
            if proc not in self.isis_procs:
                return f"The IS-IS process {proc} does not exist."
            i["isis"] = proc
        elif line == "undo isis enable":
            if not i["isis"]:
                return "IS-IS is not enabled on the interface."
            i["isis"] = None
        elif line == "isis circuit-type p2p":
            i["isis_p2p"] = True
        elif w[:2] == ["isis", "cost"]:
            i["isis_cost"] = int(w[2])
        return None

    def _apply_bgp(self, line, w, view):
        if line == "ipv4-family unicast":
            return ("bgp-af", "bgp-af-ipv4")
        m = re.match(r"^(undo )?peer (\S+) (.+)$", line)
        if line.startswith("undo peer ") and len(w) == 3:
            if self.bgp_peers.pop(w[2], None) is None:
                return "The peer does not exist."
            return None
        if m:
            undo, peer, rest = m.groups()
            if rest.startswith("as-number"):
                self.bgp_peers[peer] = {"as": rest.split()[1], "desc": "", "ignore": False, "pref": 0,
                                        "state": "Established" if rest.split()[1] == self.bgp_as else "Connect",
                                        "up": time.time(), "ci": ""}
                return None
            p = self.bgp_peers.get(peer)
            if p is None:
                return f"The peer {peer} does not exist."
            if rest.startswith("description"):
                p["desc"] = rest.split(" ", 1)[1]
            elif rest.startswith("connect-interface"):
                p["ci"] = rest.split()[1]
            elif rest == "ignore":
                p["ignore"] = not undo
                p["state"] = "Active" if undo else "Idle(Admin)"
                p["up"] = time.time()
                if not undo:
                    p["pref"] = 0
            return None
        if w[0] == "network":
            self.bgp_networks.append(f"{w[1]} {w[2]}")
        elif w[:2] == ["undo", "network"]:
            key = f"{w[2]} {w[3]}"
            if key not in self.bgp_networks:
                return "The network does not exist."
            self.bgp_networks.remove(key)
        return None

    def log(self, tag, msg):
        stamp = time.strftime("%b %d %Y %H:%M:%S+00:00", time.gmtime())
        self.extra_log.append(f"{stamp} {self.sysname_} %%01{tag}(l):CID=0x80ca2711;{msg}")

    # ---- display outputs ----------------------------------------------------
    def show_version(self):
        return f"""Huawei Versatile Routing Platform Software
VRP (R) software, Version 8.220 (NE8000 V800R022C00SPC600)
Copyright (C) 2012-2022 Huawei Technologies Co., Ltd.
HUAWEI NE8000 M8 uptime is {fmt_uptime(time.time() - BOOT)}
Patch Version: V800R022SPH120

NE8000 M8 version information:
- BKP  version information:
  PCB         Version : CR57BKPA REV B
  MPU  Slot  Quantity : 2
  LPU  Slot  Quantity : 8"""

    def show_cpu_usage(self):
        self.cpu = min(95, max(4, self.cpu + random.uniform(-3, 3) + (14 - self.cpu) * 0.1))
        if random.random() < 0.02:
            self.cpu = min(95, self.cpu + 30)
        c = round(self.cpu)
        return f"""CPU utilization statistics at {time.strftime('%Y-%m-%d %H:%M:%S')}
System CPU Using Percentage :         {c}%
CPU utilization for five seconds: {c}%, one minute: {max(1, c - 2)}%, five minutes: {max(1, c - 3)}%
Max CPU Usage :                       38%
CPU Usage : {c}% Max: 38%"""

    def show_memory_usage(self):
        used = int(16777216 * (0.36 + 0.01 * math.sin(time.time() / 300)))
        return f"""Memory utilization statistics at {time.strftime('%Y-%m-%d %H:%M:%S')}
System Total Memory Is: 16777216 Kbytes
Total Memory Used Is: {used} Kbytes
Memory Using Percentage Is: {round(used * 100 / 16777216)}%"""

    def show_temperature(self):
        rows = []
        t = time.time()
        for slot, board, base in (("1", "LPU", 46), ("2", "LPU", 44), ("9", "MPU", 39), ("10", "MPU", 38), ("13", "PWR", 33), ("14", "PWR", 34)):
            temp = round(base + 2 * math.sin(t / 200 + int(slot)) + self.cpu * 0.05)
            rows.append(f"{slot:<6}{board:<9}{'TSENSOR0':<11}{'NORMAL':<9}{-5:<8}{75:<8}{85:<8}{temp}")
        return "Base-Board, Unit:C, Slot 1\nSlot  BoardType SensorNum  Status   Minor   Major   Fatal   Current(C)\n" + \
               "-" * 74 + "\n" + "\n".join(rows)

    def phy(self, name, i):
        if not i["enabled"]:
            return "*down"
        if "." in name:
            parent = self.ifs.get(name.split(".")[0])
            return "up" if parent and self.up(parent) else "down"
        return "up" if i["cable"] else "down"

    def show_interface_brief(self):
        out = ["PHY: Physical", "*down: administratively down", "^down: standby", "(l): loopback", "(s): spoofing",
               "InUti/OutUti: input utility/output utility",
               "Interface                   PHY     Protocol  InUti OutUti   inErrors  outErrors"]
        for n, i in self.ifs.items():
            ph = self.phy(n, i)
            spoof = n.startswith(("LoopBack", "NULL"))
            proto = ("up(s)" if spoof else ("up" if ph == "up" and (i["ips"] or i["l2"] or "." in n or not n.startswith("Vlanif")) else "down")) if ph == "up" else "down"
            cap = self.capacity(self.ifs.get(n.split(".")[0], i))
            iu = f"{self.rate(n, 'in') * 100 / cap:.2f}%" if ph == "up" else "0%"
            ou = f"{self.rate(n, 'out') * 100 / cap:.2f}%" if ph == "up" else "0%"
            out.append(f"{n:<28}{ph:<8}{proto:<9}{iu:>7}{ou:>7}{i['errors']:>11}{0:>11}")
        return "\n".join(out)

    def show_ip_interface_brief(self):
        out = ["*down: administratively down", "(l): loopback", "(s): spoofing",
               "Interface                         IP Address/Mask      Physical   Protocol VPN"]
        for n, i in self.ifs.items():
            if i["l2"]:
                continue
            ip = f"{i['ips'][0][0]}/{i['ips'][0][1]}" if i["ips"] else "unassigned"
            ph = self.phy(n, i)
            out.append(f"{n:<34}{ip:<21}{ph:<11}{('up' if ph == 'up' else 'down'):<9}{i['vpn'] or '--'}")
        return "\n".join(out)

    def show_interface_description(self):
        out = ["PHY: Physical", "*down: administratively down", "Interface                     PHY     Protocol Description"]
        for n, i in self.ifs.items():
            ph = self.phy(n, i)
            out.append(f"{n:<30}{ph:<8}{('up' if ph == 'up' else 'down'):<9}{i['desc']}")
        return "\n".join(out)

    def show_interface(self, name):
        i = self.ifs.get(name)
        if not i:
            return "Error: The interface does not exist."
        self.tick_counters(name)
        ph = self.phy(name, i)
        rin, rout = self.rate(name, "in"), self.rate(name, "out")
        ip = f"Internet Address is {i['ips'][0][0]}/{i['ips'][0][1]}" if i["ips"] else "Internet protocol processing : disabled"
        vlan = f"\nEncapsulation is 802.1Q Virtual LAN, VLAN ID : {i['vlan']}" if i["vlan"] else ""
        return f"""{name} current state : {'UP' if ph == 'up' else ('Administratively DOWN' if ph == '*down' else 'DOWN')}
Line protocol current state : {'UP' if ph == 'up' else 'DOWN'}
Last line protocol up time : 2026-09-10 11:02:45
Description: {i['desc']}
Route Port,The Maximum Transmit Unit is {i['mtu'] or 1500}
{ip}{vlan}
IP Sending Frames' Format is PKTFMT_ETHNT_2, Hardware address is 00e0-fc{abs(hash(name)) % 100:02d}-{abs(hash(name)) % 10000:04d}
Last physical up time   : 2026-09-10 11:02:41
Last physical down time : 2026-09-10 11:01:12
Current system time: {time.strftime('%Y-%m-%d %H:%M:%S')}
Port Mode: COMMON COPPER
Speed : {i['speed']},  Loopback: NONE
Duplex: FULL,  Negotiation: ENABLE
Current BW : {i['speed']}bps
Statistics last cleared:never
    Last 300 seconds input rate: {int(rin)} bits/sec, {int(rin / 6000)} packets/sec
    Last 300 seconds output rate: {int(rout)} bits/sec, {int(rout / 6000)} packets/sec
    Input: {i['in_b'] // 750} packets, {i['in_b']} bytes
      Unicast:  {i['in_b'] // 760}, Multicast: 1022, Broadcast: 310
      CRC: {i['errors']}, Giants: 0, Jabbers: 0, Fragments: 0
      Discard: 0, Total Error: {i['errors']}
    Output: {i['out_b'] // 750} packets, {i['out_b']} bytes
      Unicast:  {i['out_b'] // 760}, Multicast: 822, Broadcast: 120
      Discard: 0, Total Error: 0"""

    def if_config(self, n, i):
        lines = [f"interface {n}"]
        if i["l2"]:
            lines.append(" portswitch")
            if i["trunk"]:
                lines += [" port link-type trunk", f" port trunk allow-pass vlan {i['trunk']}"]
                if i["pvid"]:
                    lines.append(f" port trunk pvid vlan {i['pvid']}")
            elif i["pvid"]:
                lines += [" port link-type access", f" port default vlan {i['pvid']}"]
        if i["vlan"]:
            if i["encap"] == "vlan-type":
                lines.append(f" vlan-type dot1q {i['vlan']}")
            elif i["encap"] == "termination":
                lines.append(f" dot1q termination vid {i['vlan']}")
            else:
                a, b = i["encap"].split()[1].split(".")
                lines.append(f" qinq termination pe-vid {a} ce-vid {b}")
        if i["mtu"]:
            lines.append(f" mtu {i['mtu']}")
        lines.append(" shutdown" if not i["enabled"] else " undo shutdown")
        if i["desc"]:
            lines.append(f" description {i['desc']}")
        if i["vpn"] and not i["vpn"].startswith("__"):
            lines.append(f" ip binding vpn-instance {i['vpn']}")
        for a, plen, sub in i["ips"]:
            lines.append(f" ip address {a} {plen}" + (" sub" if sub else ""))
        if i["ospf_cost"]:
            lines.append(f" ospf cost {i['ospf_cost']}")
        if i["isis"]:
            lines.append(f" isis enable {i['isis']}")
            if i["isis_p2p"]:
                lines.append(" isis circuit-type p2p")
            if i["isis_cost"]:
                lines.append(f" isis cost {i['isis_cost']}")
        lines += [" " + q for q in i["qos"]]
        return lines

    def show_current_configuration_configuration_qos_profile(self):
        out = []
        for name, (cir, pir) in self.profiles.items():
            out += ["#", f"qos-profile {name}"] + ([f" car cir {cir} pir {pir} cbs 18700000 pbs 18700000 green pass yellow pass red discard"] if cir else [])
        return "\n".join(out + ["#"])

    def show_current_configuration_interface(self):
        return "\n".join("\n".join(self.if_config(n, i)) + "\n#" for n, i in self.ifs.items())

    def show_current_configuration_configuration_isis(self):
        out = []
        for proc, d in self.isis_procs.items():
            out += [f"isis {proc}", f" is-level {d['level']}", f" cost-style {d['cost_style']}"] + \
                   [f" network-entity {n}" for n in d["nets"]] + ([f" is-name {d['is_name']}"] if d["is_name"] else []) + ["#"]
        return "\n".join(out)

    def _isis_ifs(self):
        return [(n, i) for n, i in self.ifs.items() if i["isis"]]

    def _isis_peers(self):
        far = {"100GE0/2/0": ("0010.0000.0002", "2.2.2.2", "172.16.0.0"), "100GE0/2/1": ("0010.0000.0003", "3.3.3.3", "172.16.0.2")}
        return [(n, i, far[n]) for n, i in self._isis_ifs() if n in far and self.up(i)]

    def show_isis_peer(self):
        if not self.isis_procs:
            return ""
        out = []
        for proc in self.isis_procs:
            out += [f"                          Peer information for ISIS({proc})", "                          ----------------------------", "",
                    "  System Id     Interface          Circuit Id        State HoldTime Type     PRI",
                    "--------------------------------------------------------------------------------"]
            peers = [x for x in self._isis_peers() if x[1]["isis"] == proc]
            for n, i, (sid, _, _) in peers:
                out.append(f"{sid}  {n:<18} {'0000000' + str(len(out) % 9 + 1):<17} Up   {random.randint(22, 30)}s      L2       --")
            out += ["", f"Total Peer(s): {len(peers)}"]
        return "\n".join(out)

    def show_isis_interface(self):
        if not self.isis_procs:
            return ""
        out = []
        for proc in self.isis_procs:
            out += [f"                       Interface information for ISIS({proc})", "                       ---------------------------------",
                    "  Interface       Id      IPV4.State          IPV6.State      MTU  Type  DIS"]
            for k, (n, i) in enumerate([x for x in self._isis_ifs() if x[1]["isis"] == proc]):
                st = "Up" if self.up(i) else "Down"
                lvl = {"level-1": "L1", "level-2": "L2"}.get(self.isis_procs[proc]["level"], "L1/L2")
                dis = "--" if i["isis_p2p"] or n.startswith("LoopBack") else "No"
                out.append(f"  {n:<15} {k + 1:03d}         {st:<18} Down         {i['mtu'] or 1497} {lvl:<5} {dis}")
        return "\n".join(out)

    def show_isis_route(self):
        if not self.isis_procs:
            return ""
        out = []
        for proc in self.isis_procs:
            out += [f"                         Route information for ISIS({proc})", "                         -----------------------------", "",
                    f"                        ISIS({proc}) Level-2 Forwarding Table", "                        --------------------------------", "",
                    "IPV4 Destination     IntCost    ExtCost ExitInterface     NextHop         Flags",
                    "-------------------------------------------------------------------------------"]
            for n, i in self._isis_ifs():
                for a, plen, _ in i["ips"]:
                    net = ipaddress.ip_network(f"{a}/{plen}", strict=False)
                    out.append(f"{str(net):<20} {i['isis_cost'] or 10:<10} NULL    {n:<17} Direct          D/-/L/-")
            for n, i, (_, rid, nh) in self._isis_peers():
                out.append(f"{rid + '/32':<20} {(i['isis_cost'] or 10) + 10:<10} NULL    {n:<17} {nh:<15} A/-/-/-")
            out += ["     Flags: D-Direct, A-Added to URT, L-Advertised in LSPs, S-IGP Shortcut,", "            U-Up/Down Bit Set, LP-Local Prefix-Sid"]
        return "\n".join(out)

    def show_isis_lsdb(self):
        if not self.isis_procs:
            return ""
        out = []
        for proc, d in self.isis_procs.items():
            out += [f"                        Database information for ISIS({proc})", "                        --------------------------------", "",
                    "                          Level-2 Link State Database", "",
                    "LSPID                 Seq Num      Checksum      Holdtime      Length  ATT/P/OL",
                    "-------------------------------------------------------------------------------"]
            sid = ".".join(d["nets"][0].split(".")[-4:-1]) if d["nets"] else "0000.0000.0000"
            out.append(f"{sid}.00-00*   0x0000a1f3   0x6c21        1102          {84 + 12 * len(self._isis_ifs())}      0/0/0")
            for _, _, (psid, _, _) in self._isis_peers():
                out.append(f"{psid}.00-00    0x00009e07   0x3b8a        987           96      0/0/0")
            out += ["", "Total LSP(s): " + str(1 + len(self._isis_peers())), "    *(In TLV)-Leaking Route, *(By LSPID)-Self LSP, +-Self LSP(Extended),",
                    "           ATT-Attached, P-Partition, OL-Overload"]
        return "\n".join(out)

    def show_current_configuration_configuration_bgp(self):
        lines = [f"bgp {self.bgp_as}", " router-id 1.1.1.1"]
        for p, d in self.bgp_peers.items():
            lines.append(f" peer {p} as-number {d['as']}")
            if d["desc"]:
                lines.append(f" peer {p} description {d['desc']}")
            if d["ci"]:
                lines.append(f" peer {p} connect-interface {d['ci']}")
            if d["ignore"]:
                lines.append(f" peer {p} ignore")
        lines += [" #", " ipv4-family unicast"] + [f"  network {n}" for n in self.bgp_networks] + \
                 [f"  peer {p} enable" for p in self.bgp_peers]
        return "\n".join(lines) + "\n#"

    def show_config(self):
        out = [f"!Software Version V800R022C00SPC600", "#", f"sysname {self.sysname_}", "#"]
        for v, rd in self.vpns.items():
            if not v.startswith("__"):
                out += [f"ip vpn-instance {v}", " ipv4-family", f"  route-distinguisher {rd}", "#"]
        out += ["vlan batch " + " ".join(str(v) for v in sorted(self.vlan_db)), "#"]
        for v, d in sorted(self.vlan_db.items()):
            if d:
                out += [f"vlan {v}", f" description {d}", "#"]
        out += ["aaa"] + [f" local-user {u} password irreversible-cipher $1c$demo$ level {l}" for u, l in self.users.items()] + ["#"]
        for name, p in self.policies.items():
            out += [f"traffic policy {name}", f" classifier {p['classifier']} behavior {name}_b", "#"]
        out += [self.show_current_configuration_configuration_qos_profile()]
        out += [self.show_current_configuration_interface()]
        out += [self.show_current_configuration_configuration_bgp()]
        out += [self.show_current_configuration_configuration_isis()]
        out += [f"ospf {proc} router-id 1.1.1.1" if i == 0 else "" for i, (proc, a, n) in enumerate(self.ospf_networks[:1])]
        out += [" area 0.0.0.0"] + [f"  network {n}" for _, _, n in self.ospf_networks] + ["#"]
        out += self.static + ["#"] + self.arp_static + ["#"]
        out += [f"ntp-service unicast-server {s}" for s in self.ntp] + ["#"]
        out += ["snmp-agent"] + [f"snmp-agent community {a} cipher %^%#demo%^%#" for a, _ in self.snmp] + ["#", "return"]
        return "\n".join(l for l in out if l is not None)

    show_current_configuration = show_config

    def show_vlan(self):
        out = ["The total number of vlans is : %d" % len(self.vlan_db),
               "--------------------------------------------------------------------------------",
               "U: Up;         D: Down;         TG: Tagged;         UT: Untagged;",
               "--------------------------------------------------------------------------------",
               "VID   Type     Ports"]
        for v in sorted(self.vlan_db):
            ports = [f"{n}(U)" for n, i in self.ifs.items() if i["l2"] and str(v) in (i["trunk"] + " " + i["pvid"]).split()]
            out.append(f"{v:<6}{'common':<9}" + ("TG:" + " ".join(ports) if ports else ""))
        out += ["", "VID   Status   Property     MAC-LRN STAT    BC  MLB  Description",
                "--------------------------------------------------------------------------------"]
        for v, d in sorted(self.vlan_db.items()):
            out.append(f"{v:<6}{'enable':<9}{'default':<13}{'enable':<8}{'disable':<8}{d}")
        return "\n".join(out)

    def connected(self):
        import ipaddress
        rows = []
        for n, i in self.ifs.items():
            if self.phy(n, i) != "up" or i["vpn"]:
                continue
            for a, plen, _ in i["ips"]:
                net = ipaddress.ip_interface(f"{a}/{plen}").network
                rows.append((f"{net.network_address}/{plen}", "Direct", 0, 0, "D", a if plen < 32 else "127.0.0.1", n))
        return rows

    def show_routes(self, vpn=None):
        rows = []
        if vpn:
            for n, i in self.ifs.items():
                if i["vpn"] == vpn and self.phy(n, i) == "up":
                    for a, plen, _ in i["ips"]:
                        import ipaddress
                        net = ipaddress.ip_interface(f"{a}/{plen}").network
                        rows.append((str(net), "Direct", 0, 0, "D", a, n))
        else:
            for s in self.static:
                m = re.match(r"ip route-static (?:vpn-instance \S+ )?(\S+) (\S+) (.*)", s)
                if "vpn-instance" in s:
                    continue
                rest = m.group(3).split()
                nh = next((t for t in rest if re.match(r"\d+\.\d+\.\d+\.\d+$", t)), "0.0.0.0")
                oif = "NULL0" if "NULL0" in rest else next((n for n, i in self.ifs.items() for a, pl, _ in i["ips"]
                                                             if nh != "0.0.0.0" and nh.rsplit(".", 1)[0] == a.rsplit(".", 1)[0]), "NULL0")
                pref = re.search(r"preference (\d+)", s)
                rows.append((f"{m.group(1)}/{mask_len(m.group(2))}", "Static", int(pref.group(1)) if pref else 60, 0, "RD", nh, oif))
            rows += self.connected()
            rows += [("2.2.2.2/32", "OSPF", 10, 2, "D", "172.16.0.0", "100GE0/2/0"),
                     ("3.3.3.3/32", "OSPF", 10, 2, "D", "172.16.0.2", "100GE0/2/1"),
                     ("100.64.0.0/16", "IBGP", 255, 0, "RD", "2.2.2.2", "100GE0/2/0"),
                     ("100.65.0.0/16", "IBGP", 255, 0, "RD", "3.3.3.3", "100GE0/2/1")]
            for k in range(24):
                rows.append((f"198.51.{k}.0/24", "IBGP", 255, 0, "RD", "2.2.2.2" if k % 2 else "3.3.3.3", "100GE0/2/0" if k % 2 else "100GE0/2/1"))
        rows.sort(key=lambda r: tuple(int(x) for x in r[0].split("/")[0].split(".")) + (int(r[0].split("/")[1]),))
        out = ["Route Flags: R - relay, D - download to fib, T - to vpn-instance, B - black hole route",
               "-" * 78, f"Routing Table : {vpn or '_public_'}",
               f"         Destinations : {len(set(r[0] for r in rows))}        Routes : {len(rows)}", "",
               "Destination/Mask    Proto   Pre  Cost        Flags NextHop         Interface", ""]
        last = None
        for r in rows:
            dest = r[0] if r[0] != last else ""
            last = r[0]
            out.append(f"{dest:>19}  {r[1]:<7} {r[2]:<4} {r[3]:<11} {r[4]:<4}{r[5]:<16}{r[6]}")
        return "\n".join(out)

    def show_route_lookup(self, text, ip):
        import ipaddress
        addr, best = ipaddress.ip_address(ip), None
        for r in P.parse_routing_table(text):
            net = ipaddress.ip_network(r["destination"], strict=False)
            if addr in net and (best is None or net.prefixlen > best[0].prefixlen):
                best = (net, r)
        head = "Route Flags: R - relay, D - download to fib, T - to vpn-instance, B - black hole route\n" + "-" * 78 + \
               f"\nRouting Table : _public_\nSummary Count : {1 if best else 0}\n\nDestination/Mask    Proto   Pre  Cost        Flags NextHop         Interface\n\n"
        if not best:
            return head
        r = best[1]
        return head + f"{r['destination']:>19}  {r['protocol']:<7} {r['preference']:<4} {r['cost']:<11} {r['flags']:<4}{r['next_hop']:<16}{r['interface']}"

    def show_bgp_lookup(self, ip):
        return (" BGP local router ID : 1.1.1.1\n Local AS number : 65001\n"
                f" (demo mode: no BGP path information is simulated for {ip})")

    def show_arp_all(self):
        out = ["ARP timeout:1200s", "IP ADDRESS      MAC ADDRESS    EXPIRE(M) TYPE        INTERFACE        VPN-INSTANCE",
               "-" * 86]
        dyn = [("10.10.1.2", "00e0-fc11-2201", "GigabitEthernet0/1/0"), ("10.10.2.2", "00e0-fc11-2202", "GigabitEthernet0/1/1"),
               ("172.16.0.0", "00e0-fc22-0001", "100GE0/2/0"), ("172.16.0.2", "00e0-fc22-0002", "100GE0/2/1"),
               ("10.20.100.11", "a4c6-4f00-1011", "GigabitEthernet0/1/4.100"), ("10.20.100.12", "a4c6-4f00-1012", "GigabitEthernet0/1/4.100"),
               ("192.168.0.1", "0050-56aa-0001", "GigabitEthernet0/0/0"),
               ("102.216.117.2", "4c5e-0c11-0301", "GigabitEthernet0/1/6.301"), ("102.216.117.34", "4c5e-0c11-0304", "GigabitEthernet0/1/6.304")]
        for n, i in self.ifs.items():
            for a, _, _ in i["ips"]:
                out.append(f"{a:<16}00e0-fc12-3456            I           {n}")
        for ip, mac, n in dyn:
            out.append(f"{ip:<16}{mac:<15}{random.randint(1, 20):<10}D-0         {n}")
        for s in self.arp_static:
            w = s.split()
            out.append(f"{w[2]:<16}{w[3]:<15}{'':<10}S           --")
        return "\n".join(out)

    def show_lldp_neighbor_brief(self):
        return """Local Interface         Exptime(s) Neighbor Interface      Neighbor Device
-------------------------------------------------------------------------------
100GE0/2/0              104        100GE1/0/0              CORE-RTR-1
100GE0/2/1              98         100GE1/0/0              CORE-RTR-2
GE0/1/4                 117        GE0/0/48                ACCESS-SW-01
GE0/1/5                 111        XGE0/0/1                L2-SW-02"""

    def show_ip_vpn_instance(self):
        out = [f" Total VPN-Instances configured : {len(self.vpns)}", "", "  VPN-Instance Name               RD                    Address-family"]
        for v, rd in self.vpns.items():
            out.append(f"  {v:<32}{rd:<22}IPv4")
        return "\n".join(out)

    def show_bgp_peer(self):
        est = sum(1 for p in self.bgp_peers.values() if p["state"] == "Established")
        out = [f" BGP local router ID : 1.1.1.1", f" Local AS number : {self.bgp_as}",
               f" Total number of peers : {len(self.bgp_peers)}                 Peers in established state : {est}", "",
               "  Peer            V          AS  MsgRcvd  MsgSent  OutQ  Up/Down       State  PrefRcv"]
        for ip, p in self.bgp_peers.items():
            age = time.time() - p["up"]
            if p["state"] in ("Connect",) and age > 15 and not p["ignore"]:
                p["state"], p["up"], p["pref"] = "Established", time.time(), random.randint(5, 50)
            msgs = int(age / 60) + 3
            out.append(f"  {ip:<15} 4 {p['as']:>11} {msgs:>8} {msgs + 2:>8} {0:>5} {hhmm(age):>8} {p['state']:>11} {p['pref']:>10}")
        return "\n".join(out)

    def show_bgp_routing_table(self):
        return self.show_bgp_routes(None, False)

    def show_bgp_routes(self, peer, advertised):
        out = [" BGP Local router ID is 1.1.1.1", " Status codes: * - valid, > - best, d - damped, h - history,  i - internal, s - suppressed",
               " Origin : i - IGP, e - EGP, ? - incomplete", "",
               "     Network            NextHop        MED        LocPrf    PrefVal Path/Ogn", ""]
        if advertised:
            for n in self.bgp_networks:
                net, plen = n.split()
                out.append(f" *>   {net + '/' + plen:<19}0.0.0.0        0                    0       i")
        else:
            peers = [peer] if peer else [p for p, d in self.bgp_peers.items() if d["state"] == "Established"]
            for p in peers:
                d = self.bgp_peers.get(p)
                if not d or d["state"] != "Established":
                    continue
                path = "" if d["as"] == self.bgp_as else d["as"] + " "
                for k in range(min(d["pref"], 12)):
                    out.append(f" *>i  {'198.51.' + str(k) + '.0/24':<19}{p:<15}0          100        0       {path}i")
        return "\n".join(out)

    def show_ospf_peer_brief(self):
        return """
         OSPF Process 1 with Router ID 1.1.1.1
                  Peer Statistic Information
 ----------------------------------------------------------------------------
 Area Id          Interface                        Neighbor id      State
 0.0.0.0          100GE0/2/0                       2.2.2.2          Full
 0.0.0.0          100GE0/2/1                       3.3.3.3          Full
 ----------------------------------------------------------------------------
 Total Peer(s):        2"""

    def show_ospf_interface(self):
        c0 = self.ifs["100GE0/2/0"]["ospf_cost"] or 1
        c1 = self.ifs["100GE0/2/1"]["ospf_cost"] or 1
        return f"""
          OSPF Process 1 with Router ID 1.1.1.1
                  Interfaces

 Area: 0.0.0.0           (MPLS TE not enabled)
 Interface                 IP Address      Type      State    Cost    Pri   DR              BDR
 100GE0/2/0                172.16.0.1      P2P       P-2-P    {c0:<7} 1     0.0.0.0         0.0.0.0
 100GE0/2/1                172.16.0.3      P2P       P-2-P    {c1:<7} 1     0.0.0.0         0.0.0.0
 LoopBack0                 1.1.1.1         P2P       P-2-P    0       1     0.0.0.0         0.0.0.0"""

    def show_ospf_lsdb(self):
        return """
         OSPF Process 1 with Router ID 1.1.1.1
                 Link State Database

                         Area: 0.0.0.0
 Type      LinkState ID    AdvRouter          Age  Len   Sequence   Metric
 Router    1.1.1.1         1.1.1.1            312  60    80000412       1
 Router    2.2.2.2         2.2.2.2            890  72    800003f1       1
 Router    3.3.3.3         3.3.3.3            455  72    80000398       1"""

    def show_mpls_ldp_session(self):
        return """ LDP Session(s) in Public Network
 Codes: LAM(Label Advertisement Mode), SsnAge Unit(DDDD:HH:MM)
 An asterisk (*) before a session means the session is being deleted.
 --------------------------------------------------------------------------
 PeerID             Status      LAM  SsnRole  SsnAge       KASent/Rcv
 --------------------------------------------------------------------------
 2.2.2.2:0          Operational DU   Passive  0042:07:13   36412/36410
 3.3.3.3:0          Operational DU   Passive  0042:07:12   36409/36411
 --------------------------------------------------------------------------
 TOTAL: 2 Session(s) Found."""

    def show_mpls_lsp(self):
        return """-------------------------------------------------------------------------------
                 LSP Information: LDP LSP
-------------------------------------------------------------------------------
FEC                In/Out Label    In/Out IF                      Vrf Name
1.1.1.1/32         3/NULL          -/-
2.2.2.2/32         NULL/3          -/100GE0/2/0
2.2.2.2/32         48061/3         -/100GE0/2/0
3.3.3.3/32         NULL/3          -/100GE0/2/1
3.3.3.3/32         48062/3         -/100GE0/2/1"""

    def show_local_user(self):
        out = ["  The total number of local users is : %d" % len(self.users),
               "  ----------------------------------------------------------------------------",
               "  User-name                 State  AuthMask  AdminLevel",
               "  ----------------------------------------------------------------------------"]
        for u, l in self.users.items():
            out.append(f"  {u:<26}Active {'S':<9} {l}")
        return "\n".join(out)

    def show_users(self):
        return f"""  User-Intf    Delay    Type   Network Address     AuthenStatus    AuthorcmdFlag
+ 34  VTY 0   00:00:00  SSH    {self.host if self.host != 'demo' else '192.168.0.50':<20}pass            no
  Username : admin"""

    def show_clock(self):
        return time.strftime("%Y-%m-%d %H:%M:%S+00:00\n%A\nTime Zone(UTC) : UTC")

    def show_ntp_service_status(self):
        return f""" clock status: synchronized
 clock stratum: 3
 reference clock ID: {self.ntp[0] if self.ntp else 'none'}
 nominal frequency: 64.0000 Hz
 clock offset: 0.2213 ms
 root delay: 1.92 ms
 synchronization state: clock synchronized"""

    def show_snmp_agent_community(self):
        return "\n".join(f"   Community name: %^%#demo{idx}%^%#\n       Group name: {a}\n       Storage-type: nonVolatile"
                         for idx, (a, _) in enumerate(self.snmp))

    def show_device(self):
        return """NE8000 M8's Device status:
-------------------------------------------------------------------------------
Slot #   Type   Online    Register     Status   Role  LsId   Primary
-------------------------------------------------------------------------------
1        LPU    Present   Registered   Normal   LC    0      NA
2        LPU    Present   Registered   Normal   LC    0      NA
3        LPU    Absent    NA           NA       NA    NA     NA
9        MPU    Present   NA           Normal   MMB   0      Master
10       MPU    Present   Registered   Normal   MMB   0      Slave
13       PWR    Present   Registered   Normal   OTHER 0      NA
14       PWR    Present   Registered   Normal   OTHER 0      NA
15       FAN    Present   Registered   Normal   OTHER 0      NA
-------------------------------------------------------------------------------"""

    def show_power(self):
        p1 = 610 + random.randint(-15, 15)
        p2 = 598 + random.randint(-15, 15)
        return f"""-------------------------------------------------------------------------------
Slot     PowerNo  Present  Mode   State    Current(A)  Voltage(V)  RealPwr(W)
-------------------------------------------------------------------------------
13       PWR1     YES      AC     Supply   {p1 / 53.5:<11.1f} 53.5        {p1}
14       PWR2     YES      AC     Supply   {p2 / 53.5:<11.1f} 53.5        {p2}
-------------------------------------------------------------------------------
Total real power: {p1 + p2} W"""

    def show_fan(self):
        return "\n".join(["Slot  FanID  Present  Registered  Speed  Mode   Airflow", "-" * 56] +
                         [f"15    {k:<7}YES      YES         {round(45 + self.cpu / 4)}%    AUTO   Side-to-Back" for k in range(1, 5)])

    def show_esn(self):
        return "ESN of slot 9: 2102352168P0K8000123\nESN of slot 10: 2102352168P0K8000124"

    def show_alarm_active(self):
        rows = []
        k = 1
        for n, i in self.ifs.items():
            if i["enabled"] and not i["cable"] and "." not in n:
                rows.append(f"  {k}/Independent/2026-10-22 17:58:11+00:00/-/0x8520003/linkDown/Major/Start/\n    Interface {n} has turned into DOWN state.")
                k += 1
        for p, d in self.bgp_peers.items():
            if d["state"] not in ("Established",):
                rows.append(f"  {k}/Independent/2026-10-22 18:00:02+00:00/-/0x10330000/hwBgpPeerSessionDown/Major/Start/\n    BGP peer {p} state changed to {d['state']}.")
                k += 1
        if self.cpu > 80:
            rows.append(f"  {k}/Independent/{time.strftime('%Y-%m-%d %H:%M:%S')}+00:00/-/0x8180101/hwCPUUtilizationRisingAlarm/Minor/Start/\n    CPU utilization exceeded the threshold.")
        return "A/B/C/D/E/F/G/H/I/J\nA=Sequence, B=RootKindFlag, C=Generating time, D=Clearing time\nE=ID, F=Name, G=Level, H=State\nI=Description\n" + "\n".join(rows)

    def show_logbuffer(self):
        base = [
            "Oct 22 2026 17:58:11+00:00 NE8000-M8 %%01IFNET/2/linkDown_active(l):CID=0x807a0405;The interface status changes. (ifName=GigabitEthernet0/1/2, AdminStatus=UP, OperStatus=DOWN, Reason=The link protocol is down)",
            "Oct 22 2026 18:00:02+00:00 NE8000-M8 %%01BGP/3/STATE_CHG_UPDOWN(l):CID=0x80882711;The status of the peer 10.10.1.2 changed from ESTABLISHED to IDLE. (InstanceName=Public, StateChangeReason=Hold Timer Expired)",
            "Oct 22 2026 18:01:44+00:00 NE8000-M8 %%01SSH/5/SSH_USER_LOGIN(s):CID=0x80cb0001;The SSH user succeeded in logging in. (UserName=admin, IPAddress=192.168.0.50)",
        ]
        lines = list(reversed(base + self.extra_log))
        return "Logging buffer configuration and contents : enabled\nAllowed max buffer size : 1024\nChannel number : 4 , Channel name : logbuffer\n" + \
               f"Dropped messages : 0\nOverwritten messages : 0\nCurrent messages : {len(lines)}\n\n" + "\n".join(lines)

    def show_traffic_policy_user_defined(self):
        out = [f"  User Defined Traffic Policy Information:"]
        for name, p in self.policies.items():
            out += [f"  Policy: {name}", f"   Classifier: {p['classifier']}", f"    Behavior: {name}_b",
                    f"     Committed Access Rate:", f"       CIR {p['cir']} (Kbps), PIR 0 (Kbps), CBS 0 (byte), PBS 0 (byte)",
                    "       Conform Action: pass", "       Exceed Action: discard", ""]
        return "\n".join(out)

    def show_port_queue(self, name):
        if name not in self.ifs:
            return "Error: The interface does not exist."
        rate = self.rate(name, "out")
        shares = {"be": 0.55, "af1": 0.1, "af2": 0.12, "af3": 0.05, "af4": 0.08, "ef": 0.06, "cs6": 0.03, "cs7": 0.01}
        out = [f"[{name}]"]
        uptime = time.time() - BOOT
        for q, sh in shares.items():
            bps = int(rate * sh)
            drop = int(bps * 0.002) if q == "be" else 0
            total = int(bps / 8 * uptime / 50)
            out += [f"  [{q}]", f"    Total pass:", f"      {total // 700} packets, {total} bytes",
                    f"    Total discard:", f"      {drop * 1000} packets, {drop * 700000} bytes",
                    f"    Last 30s pass rate:", f"      {bps // 6000} pps, {bps} bps",
                    f"    Last 30s discard rate:", f"      {drop // 6000} pps, {drop} bps"]
        return "\n".join(out)

    def ping(self, c):
        target = c.split()[-1]
        lines = [f"  PING {target}: 56  data bytes, press CTRL_C to break"]
        times = [round(random.uniform(0.6, 2.4), 2) for _ in range(5)]
        for k, t in enumerate(times, 1):
            lines.append(f"    Reply from {target}: bytes=56 Sequence={k} ttl=254 time={t} ms")
        lines += ["", f"  --- {target} ping statistics ---", "    5 packet(s) transmitted", "    5 packet(s) received",
                  "    0.00% packet loss", f"    round-trip min/avg/max = {min(times)}/{sum(times) / 5:.2f}/{max(times)} ms"]
        return "\n".join(lines)

    def tracert(self, c):
        target = c.split()[-1]
        hops = ["172.16.0.0", "10.255.0.1", "10.255.1.9", target]
        out = [f" traceroute to  {target}(%s), max hops: 30 ,packet length: 40,press CTRL_C to break" % target]
        for k, h in enumerate(hops, 1):
            out.append(f" {k} {h} {random.uniform(0.5, 3):.0f} ms  {random.uniform(0.5, 3):.0f} ms  {random.uniform(0.5, 3):.0f} ms")
        return "\n".join(out)
