"""Router session: runs commands, keeps a background poller for graphs, and
exposes the views the GUI shows. RealRouter talks SSH; demo.DemoRouter fakes it."""
import collections
import re
import threading
import time

import parsers as P

READ_ONLY_PREFIXES = ("display ", "dis ", "disp ", "ping ", "tracert ")
HISTORY = 720            # samples kept per series (1 hour at 5 s)
POLL_SECONDS = 5
ROUTE_LIST_LIMIT = 5000      # never list more routes than this in one go
LISTABLE_PROTOCOLS = ("DIRECT", "STATIC", "OSPF", "ISIS", "RIP", "UNR")
WATCH_TTL = 90           # stop polling an interface's counters this long after the GUI last asked


class DeviceError(Exception):
    pass


def has_control_chars(text):
    """Newlines or control characters would let one 'command' smuggle in more lines."""
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in text)


def is_read_only(command):
    if has_control_chars(command) or len(command) > 300:
        return False
    c = command.strip().lower()
    return c in ("display", "dis") or c.startswith(READ_ONLY_PREFIXES)


def _cidr(line):
    """'ip address 10.0.0.1 255.255.255.0 sub' or 'ip address 10.0.0.1 24 sub' -> '10.0.0.1/24'"""
    parts = line.split()
    mask = parts[3]
    plen = sum(bin(int(x)).count("1") for x in mask.split(".")) if "." in mask else int(mask)
    return f"{parts[2]}/{plen}"


def ipv6_addrs(lines):
    """'ipv6 address 2001:DB8::1/64' (and 'ipv6 address 2001:DB8::1 64') -> ['2001:DB8::1/64']; link-local skipped."""
    out = []
    for l in lines:
        w = l.split()
        if len(w) >= 3 and w[0] == "ipv6" and w[1] == "address" and ":" in w[2] and "link-local" not in w:
            out.append(w[2] if "/" in w[2] else f"{w[2]}/{w[3]}" if len(w) > 3 and w[3].isdigit() else w[2])
    return out


class Router:
    demo = False
    via = "SSH"
    can_configure = True

    def __init__(self, host):
        self.host = host
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.history = collections.defaultdict(lambda: collections.deque(maxlen=HISTORY))
        self.watch = {}             # interface -> last time the GUI asked for it
        self.last_counters = {}     # interface -> (t, in_bytes, out_bytes)
        self.cache = {}
        self.poll_error = ""

    # subclasses implement _run / _configure / _exec / close
    def run(self, command, read_timeout=60):
        with self.lock:
            try:
                return self._run(command, read_timeout)
            except DeviceError:
                raise
            except Exception as exc:
                raise DeviceError(str(exc)) from exc

    def configure(self, lines, commit=True):
        lines = [l for l in (x.rstrip() for x in lines) if l.strip() and not l.lstrip().startswith("#")]
        if not lines:
            raise DeviceError("No configuration lines given.")
        with self.lock:
            try:
                out = self._configure(lines, commit)
            except DeviceError:
                raise
            except Exception as exc:
                raise DeviceError(str(exc)) from exc
        self.cache.clear()
        if "Error:" in out:
            raise DeviceError("The router rejected part of the change:\n" + out)
        return out

    def execute(self, command):
        """User-view operational command (reset, refresh, save)."""
        with self.lock:
            try:
                out = self._exec(command)
            except Exception as exc:
                raise DeviceError(str(exc)) from exc
        self.cache.clear()
        return out

    def cached(self, command, ttl=4):
        hit = self.cache.get(command)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        out = self.run(command)
        self.cache[command] = (time.time(), out)
        return out

    # ---- poller -----------------------------------------------------------
    def start_poller(self):
        threading.Thread(target=self._poll_loop, daemon=True).start()

    def _poll_loop(self):
        tick = 0
        while not self.stop.is_set():
            t = time.time()
            try:
                self._poll_once(t, tick)
                self.poll_error = ""
            except Exception as exc:  # keep polling; show the error in the GUI
                self.poll_error = str(exc)
            tick += 1
            self.stop.wait(max(0.5, POLL_SECONDS - (time.time() - t)))

    def _poll_once(self, t, tick):
        cpu = P.parse_cpu(self.run("display cpu-usage"))
        mem = P.parse_memory(self.run("display memory-usage"))["percent"]
        self.history["cpu"].append((t, cpu))
        self.history["memory"].append((t, mem))
        if tick % 6 == 0:
            temps = P.parse_temperature(self.run("display temperature"))
            if temps:
                self.history["temperature"].append((t, max(r["temp"] for r in temps)))
        if tick % 2 == 0:
            for row in P.parse_interface_brief(self.run("display interface brief")):
                self.history["util:" + row["name"]].append((t, row["in_util"], row["out_util"]))
        now = time.time()
        for name, seen in list(self.watch.items()):
            if now - seen > WATCH_TTL:
                self.watch.pop(name, None)
                continue
            self._sample_interface(name, t)

    def _sample_interface(self, name, t):
        d = P.parse_interface_detail(self.run(f"display interface {name}"))
        prev = self.last_counters.get(name)
        if d["input_bytes"] is not None and prev and t > prev[0]:
            dt = t - prev[0]
            rx = max(0, d["input_bytes"] - prev[1]) * 8 / dt
            tx = max(0, d["output_bytes"] - prev[2]) * 8 / dt
        elif d["input_bytes"] is None:
            rx, tx = d["in_rate_bps"] or 0, d["out_rate_bps"] or 0
        else:
            rx = tx = None
        if d["input_bytes"] is not None:
            self.last_counters[name] = (t, d["input_bytes"], d["output_bytes"])
        if rx is not None:
            self.history["bps:" + name].append((t, round(rx), round(tx)))
        return d

    def series(self, key, since=0):
        return [list(x) for x in self.history.get(key, ()) if x[0] > since]

    def close(self):
        self.stop.set()

    # ---- views ------------------------------------------------------------
    def overview(self):
        version = P.parse_version(self.cached("display version", 300))
        ifs = P.parse_interface_brief(self.cached("display interface brief"))
        phys = [i for i in ifs if i["type"] in ("ethernet", "eth-trunk", "management")]
        bgp_text = self.cached("display bgp peer", 10)
        alarms = P.parse_alarms(self.cached("display alarm active", 10))
        temps = P.parse_temperature(self.cached("display temperature", 30))
        mem = P.parse_memory(self.cached("display memory-usage"))
        return {
            "host": self.host, "demo": self.demo, "via": self.via, **version,
            "cpu": P.parse_cpu(self.cached("display cpu-usage")), "memory": mem,
            "sysname": self.sysname(),
            "interfaces": {"total": len(phys), "up": sum(1 for i in phys if i["phy"] == "up"),
                           "admin_down": sum(1 for i in phys if i["admin_down"]), "all": len(ifs)},
            "bgp": {**P.parse_bgp_summary(bgp_text), "peers": P.parse_bgp_peers(bgp_text)},
            "alarms": {"total": len(alarms), "critical": sum(1 for a in alarms if a["level"] in ("Critical", "Major"))},
            "max_temp": max((r["temp"] for r in temps), default=None),
            "top_interfaces": sorted([i for i in ifs if i["phy"] == "up" and i["type"] != "loopback"],
                                     key=lambda i: -(i["in_util"] + i["out_util"]))[:6],
            "poll_error": self.poll_error,
        }

    def sysname(self):
        return P.first_str(r"^sysname (\S+)", self.cached("display current-configuration | include sysname", 60), re.M) or self.host

    def resources(self, since=0):
        return {"cpu": self.series("cpu", since), "memory": self.series("memory", since),
                "temperature": self.series("temperature", since),
                "memory_detail": P.parse_memory(self.cached("display memory-usage")),
                "temps": P.parse_temperature(self.cached("display temperature", 30)),
                "poll_seconds": POLL_SECONDS, "poll_error": self.poll_error}

    def interface_config(self):
        return P.parse_config_blocks(self.cached("display current-configuration interface", 10))

    def interfaces(self):
        rows = P.parse_interface_brief(self.cached("display interface brief"))
        ips = P.parse_ip_brief(self.cached("display ip interface brief"))
        desc = P.parse_descriptions(self.cached("display interface description"))
        cfg = self.interface_config()
        for row in rows:
            row.update(ips.get(row["name"], {"ip": "", "vpn": ""}))
            row["description"] = desc.get(row["name"], "")
            lines = cfg.get(row["name"], [])
            row["vlan"] = P.vlan_from_config(lines)
            row["mtu"] = next((l.split()[1] for l in lines if l.startswith("mtu ")), "")
            row["l2"] = "portswitch" in lines
            row["secondary"] = [_cidr(l) for l in lines if l.startswith("ip address ") and l.endswith(" sub")]
            row["ipv6"] = ipv6_addrs(lines)
            row["util_history"] = [[x[1], x[2]] for x in list(self.history.get("util:" + row["name"], ()))[-30:]]
        return rows

    def interface_detail(self, name):
        self.watch[name] = time.time()
        d = self._sample_interface(name, time.time()) if not self.history.get("bps:" + name) else \
            P.parse_interface_detail(self.cached(f"display interface {name}"))
        d["name"] = name
        d["config"] = self.interface_config().get(name, [])
        d["raw"] = self.cached(f"display interface {name}")
        return d

    def traffic(self, names, since=0):
        out = {}
        for n in names:
            if n not in self.watch and not self.history.get("bps:" + n):
                self._sample_interface(n, time.time())
            self.watch[n] = time.time()
            out[n] = self.series("bps:" + n, since)
        return out

    def vlans(self):
        ifs = self.interfaces()
        return {"vlans": P.parse_vlans(self.cached("display vlan", 10)),
                "subinterfaces": [i for i in ifs if i["type"] == "sub-interface"],
                "vlanifs": [i for i in ifs if i["type"] == "vlanif"],
                "l2ports": [{"name": n, "config": [l for l in c if l.startswith(("port ", "portswitch"))]}
                            for n, c in self.interface_config().items() if "portswitch" in c],
                "parents": [i["name"] for i in ifs if i["type"] in ("ethernet", "eth-trunk")]}

    def qos(self):
        cfg = self.interface_config()
        rules = P.parse_qos_from_config(cfg)
        text = self.cached("display current-configuration configuration qos-profile", 10)
        profiles = [] if text.strip().startswith(("Error", "^")) or "Error:" in text[:200] else P.parse_qos_profiles(text)
        by_name = {x["name"]: x for x in profiles}
        for x in profiles:
            x["interfaces"] = sorted({r["interface"] for r in rules if r.get("profile") == x["name"]})
        for r in rules:   # a profile binding limits at the profile's CAR rate
            if r["kind"] == "profile" and r["profile"] in by_name:
                r["cir_kbps"], r["pir_kbps"] = by_name[r["profile"]]["cir_kbps"], by_name[r["profile"]]["pir_kbps"]
        return {"rules": rules, "profiles": profiles,
                "queues": [{"interface": n, "line": l} for n, c in cfg.items() for l in c if l.startswith("port-queue")],
                "policies": "" if (pol := self.cached("display traffic policy user-defined", 10)).strip().startswith("Error:") else pol,
                "interfaces": [n for n in cfg if not n.lower().startswith(("null", "loopback"))]}

    def queue_stats(self, name):
        text = self.run(f"display port-queue statistics interface {name} outbound")
        return {"queues": P.parse_port_queue(text), "raw": text}

    def routes(self, vpn=None, lookup=None):
        """The routing table. A table with a full Internet feed (about a million prefixes) is never
        listed whole: it takes minutes and ties up an SSH line on the router. Then only the smaller
        protocols are listed, and a lookup finds the route for one address or prefix."""
        cmd = f"display ip routing-table vpn-instance {vpn}" if vpn else "display ip routing-table"
        static = [r for r in P.parse_static_routes(self.cached("display current-configuration | include ip route-static", 10))
                  if not vpn or r.get("vpn") == vpn]   # public view lists all static routes, as before
        if lookup:
            text = self.run(f"{cmd} {lookup.replace('/', ' ')}")
            return {"routes": P.parse_routing_table(text), "summary": f"Lookup {lookup}", "static": static, "lookup": lookup, "raw": text}
        stats = P.parse_route_stats(self.cached(cmd + " statistics", 30))
        if stats is None or stats["total"] <= ROUTE_LIST_LIMIT:
            text = self.cached(cmd)
            return {"routes": P.parse_routing_table(text), "summary": P.first_str(r"(Destinations\s*:.*)", text),
                    "static": static, "stats": stats}
        rows, skipped = [], []
        for proto, n in stats["protocols"].items():
            if not n:
                continue
            if proto.replace("-", "") in LISTABLE_PROTOCOLS and n <= ROUTE_LIST_LIMIT:
                rows += P.parse_routing_table(self.cached(f"{cmd} protocol {proto.lower().replace('-', '')}", 30))
            else:
                skipped.append(f"{proto} {n:,}")
        return {"routes": rows, "static": static, "stats": stats, "large": True, "skipped": skipped,
                "summary": f"{stats['total']:,} prefixes. Not listed: {', '.join(skipped)}. Use Lookup to find the route for an address."}

    def arp(self):
        static = self.cached("display current-configuration | include arp static", 10)
        return {"entries": P.parse_arp(self.cached("display arp all")),
                "static": [l.strip() for l in static.splitlines() if l.strip().startswith("arp static")]}

    def neighbors(self):
        return {"lldp": P.parse_lldp(self.cached("display lldp neighbor brief", 10))}

    def vpn_instances(self):
        text = self.cached("display ip vpn-instance", 10)
        return {"instances": P.parse_vpn_instances(text), "raw": text}

    def bgp(self):
        text = self.cached("display bgp peer")
        return {**P.parse_bgp_summary(text), "peers": P.parse_bgp_peers(text),
                "config": P.parse_config_blocks(self.cached("display current-configuration configuration bgp", 10), "bgp")}

    def check_table_size(self, command):
        """Console guard: refuse to dump a whole routing table that is too big to list."""
        c = " ".join(command.lower().split())
        m = re.fullmatch(r"(?:dis|disp|display) (ip|bgp) routing-table((?: vpn-instance \S+)?)(?: verbose)?", c)
        if not m:
            return
        if m.group(1) == "ip":
            stats = P.parse_route_stats(self.cached(f"display ip routing-table{m.group(2)} statistics", 30))
            count = stats["total"] if stats else None
        else:
            count = P.first_int(r"Total Number of Routes\s*:\s*(\d+)", self.run("display bgp routing-table statistics"))
        if count and count > ROUTE_LIST_LIMIT:
            raise DeviceError(f"That table has {count:,} routes, too many to print here. Add an address (e.g. {command.strip()} 8.8.8.8), "
                              "use '| include', or add 'statistics'.")

    def bgp_routes(self, peer=None, which="received", lookup=None):
        if lookup:
            text = self.run(f"display bgp routing-table {lookup.replace('/', ' ')}")
            return {"routes": P.parse_bgp_routes(text), "raw": text, "lookup": lookup}
        if peer:
            cmd = f"display bgp routing-table peer {peer} {'advertised-routes' if which == 'advertised' else 'received-routes'}"
        else:
            cmd = "display bgp routing-table"
        count = P.first_int(r"(?:Total Number of Routes|routes total)\s*:\s*(\d+)", self.run(cmd + " statistics"), re.I)
        if count is None and peer and which != "advertised":
            count = next((p["prefixes"] for p in P.parse_bgp_peers(self.cached("display bgp peer", 10)) if p["peer"] == peer), None)
        if count is None or count > ROUTE_LIST_LIMIT:
            what = f"{count:,} routes" if count is not None else "an unknown number of routes"
            return {"routes": [], "too_many": count, "raw": f"This would list {what}, more than the {ROUTE_LIST_LIMIT:,} this page shows at once "
                    "(listing a full Internet table takes minutes and ties up an SSH line on the router).\n"
                    "Use Lookup with an address or prefix, e.g. 8.8.8.8 or 8.8.8.0/24."}
        text = self.run(cmd, read_timeout=120)
        return {"routes": P.parse_bgp_routes(text), "raw": text}

    def ospf(self):
        return {"peers": P.parse_ospf_peers(self.cached("display ospf peer brief")),
                "interfaces": P.parse_ospf_interfaces(self.cached("display ospf interface", 10)),
                "lsdb": self.cached("display ospf lsdb", 10)}

    def isis(self):
        def show(cmd):
            out = self.cached(cmd, 10)
            return "" if "Error:" in out else out     # an older image may not know a command
        processes = P.parse_isis_config(show("display current-configuration configuration isis"))
        ifcfg = P.parse_isis_if_config(self.interface_config())
        state = {P.isis_key(r["interface"]): r for r in P.parse_isis_interfaces(show("display isis interface"))}
        interfaces = []
        for name, c in ifcfg.items():
            st = state.pop(P.isis_key(name), {})
            interfaces.append({"interface": name, **c, "ipv4": st.get("ipv4", ""), "type": st.get("type", ""),
                               "mtu": st.get("mtu"), "dis": st.get("dis", "")})
        for st in state.values():                    # running but not found in the interface config
            interfaces.append({"interface": st["interface"], "process": st["process"], "cost": None, "cost_level": "",
                               "p2p": False, "passive": False, "ipv4": st["ipv4"], "type": st["type"], "mtu": st["mtu"], "dis": st["dis"]})
        peers = P.parse_isis_peers(show("display isis peer"))
        configured = bool(processes or interfaces or peers)
        return {"configured": configured, "processes": processes, "interfaces": interfaces, "peers": peers,
                "routes": P.parse_isis_routes(show("display isis route")) if configured else [],
                "lsdb": show("display isis lsdb") if configured else ""}

    def mpls(self):
        return {"ldp": P.parse_ldp_sessions(self.cached("display mpls ldp session", 10)),
                "lsp": self.cached("display mpls lsp", 10)}

    def system(self):
        return {"users": P.parse_local_users(self.cached("display local-user", 10)),
                "online": self.cached("display users", 10),
                "clock": self.cached("display clock", 10),
                "ntp": self.cached("display ntp-service status", 10),
                "ntp_servers": [l.split()[-1] for l in self.cached("display current-configuration | include ntp-service unicast-server", 10).splitlines() if "unicast-server" in l],
                "snmp": self.cached("display snmp-agent community", 10),
                "sysname": self.sysname()}

    def hardware(self):
        return {"boards": P.parse_device(self.cached("display device", 10)),
                "temperature": P.parse_temperature(self.cached("display temperature", 10)),
                "power": self.cached("display power", 10), "fan": self.cached("display fan", 10),
                "elabel": self.cached("display esn", 300)}

    def logs(self):
        alarms_text = self.cached("display alarm active", 5)
        log_text = self.cached("display logbuffer", 5)
        return {"alarms": P.parse_alarms(alarms_text), "alarms_raw": alarms_text,
                "log": P.parse_logbuffer(log_text), "log_raw": log_text}


class RealRouter(Router):
    def __init__(self, host, username, password, port=22, poll=True):
        super().__init__(host)
        try:
            from netmiko import ConnectHandler
            self.conn = ConnectHandler(device_type="huawei_vrpv8", host=host, port=int(port), username=username,
                                       password=password, timeout=30, conn_timeout=15, fast_cli=False)
        except Exception as exc:
            raise DeviceError(f"Could not connect to {host}:{port}: {exc}") from exc
        if poll:
            self.start_poller()

    def _run(self, command, read_timeout):
        return self.conn.send_command(command, read_timeout=read_timeout)

    def _configure(self, lines, commit):
        out = self.conn.send_config_set(lines, exit_config_mode=False, read_timeout=90, cmd_verify=False)
        if commit and "Error:" in out:
            # a rejected line: abort, so a half-done change is never committed
            out += self._discard() + "\nInfo: Nothing was committed because a line was rejected."
        elif commit:
            out += self.conn.commit()
            self.conn.exit_config_mode()
        else:
            out += self._discard()
        return out

    def _discard(self):
        """Throw away the candidate and get back to user view. VRP may ask
        "Uncommitted configurations found, commit them before exiting? [Y(yes)/N(no)/C(cancel)]",
        which netmiko's exit_config_mode cannot answer: the answer is always N."""
        ask = lambda o: "[Y(yes)/N(no)" in o or "commit them before exiting" in o
        out = self.conn.send_command_timing("abort")
        if ask(out):
            out += self.conn.send_command_timing("N")
        for _ in range(3):
            if self.conn.find_prompt().rstrip().endswith(">"):
                break
            o = self.conn.send_command_timing("return")
            out += o
            if ask(o):
                out += self.conn.send_command_timing("N")
        return out

    def _exec(self, command):
        out = self.conn.send_command_timing(command, read_timeout=60)
        if "[Y/N]" in out or "y/n" in out.lower():
            out += self.conn.send_command_timing("Y", read_timeout=60)
        return out

    def close(self):
        super().close()
        try:
            self.conn.disconnect()
        except Exception:
            pass
