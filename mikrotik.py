"""MikroTik (RouterOS) routers, read-only.

Everything is read over the RouterOS API (port 8728, or 8729 with TLS) with a login that only needs the "read" and "api"
policies: interfaces and their traffic, CPU, memory, temperatures, addresses, ARP, neighbors, routes, BGP and OSPF,
logs, users and the hardware. The configuration backup (/export) is read over SSH with the same login, because the API
cannot print it. Nothing is ever changed on the router from here: the console takes print / monitor / export / ping /
traceroute only, and every change page says that MikroTik changes are not supported yet.

The views return the same shapes as the Huawei ones (device.Router / snmp_router.SnmpRouter), so the pages work as they
are. The login is held in memory only, like the SSH login of a Huawei router.
"""
import hashlib
import re
import socket
import ssl
import threading
import time

from device import POLL_SECONDS, ROUTE_LIST_LIMIT, DeviceError, Router

API_PORT, API_TLS_PORT = 8728, 8729
READ_VERBS = ("print", "getall", "monitor", "export", "ping", "traceroute")
NOT_HERE = "Not available on MikroTik routers."


class ApiError(DeviceError):
    pass


# ---- RouterOS API client ----------------------------------------------------------------------------------------
def _enc_len(n):
    if n < 0x80:
        return bytes([n])
    if n < 0x4000:
        return (n | 0x8000).to_bytes(2, "big")
    if n < 0x200000:
        return (n | 0xC00000).to_bytes(3, "big")
    if n < 0x10000000:
        return (n | 0xE0000000).to_bytes(4, "big")
    return b"\xf0" + n.to_bytes(4, "big")


class Api:
    """The classic RouterOS API: sentences of length-prefixed words. One command at a time (the caller holds a lock)."""

    def __init__(self, host, username, password, port=API_PORT, tls=False, timeout=15):
        self.host, self.port = host, int(port)
        try:
            raw = socket.create_connection((host, self.port), timeout=timeout)
        except OSError as exc:
            raise ApiError(f"Could not reach the RouterOS API on {host}:{port}: {exc}. "
                           f"Check that /ip service api{'-ssl' if tls else ''} is enabled and allowed from this server.") from exc
        if tls:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE        # RouterOS uses a self-signed certificate by default
            try:
                raw = ctx.wrap_socket(raw, server_hostname=host)
            except (OSError, ssl.SSLError) as exc:
                raw.close()
                raise ApiError(f"TLS to {host}:{port} failed: {exc}") from exc
        self.sock = raw
        self.sock.settimeout(timeout)
        self.buf = b""
        try:
            self._login(username, password)
        except Exception:
            self.close()
            raise

    def _login(self, username, password):
        replies, done = self.talk(["/login", f"=name={username}", f"=password={password}"], raise_trap=False)
        trap = next((r for t, r in replies if t == "!trap"), None)
        if trap is not None:
            raise ApiError(f"MikroTik login failed: {trap.get('message', 'invalid user name or password')}")
        if "ret" in done:     # RouterOS before 6.43: challenge and MD5 response
            chal = bytes.fromhex(done["ret"])
            resp = "00" + hashlib.md5(b"\x00" + password.encode() + chal).hexdigest()
            replies, _ = self.talk(["/login", f"=name={username}", f"=response={resp}"], raise_trap=False)
            trap = next((r for t, r in replies if t == "!trap"), None)
            if trap is not None:
                raise ApiError(f"MikroTik login failed: {trap.get('message', 'invalid user name or password')}")

    # wire format
    def _send(self, words):
        data = b"".join(_enc_len(len(w.encode())) + w.encode() for w in words) + b"\x00"
        self.sock.sendall(data)

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ApiError("The router closed the API connection.")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _read_len(self):
        b = self._read(1)[0]
        if b < 0x80:
            return b
        if b < 0xC0:
            return ((b & 0x3F) << 8) | self._read(1)[0]
        if b < 0xE0:
            return ((b & 0x1F) << 16) | int.from_bytes(self._read(2), "big")
        if b < 0xF0:
            return ((b & 0x0F) << 24) | int.from_bytes(self._read(3), "big")
        return int.from_bytes(self._read(4), "big")

    def _sentence(self):
        words = []
        while True:
            n = self._read_len()
            if n == 0:
                return words
            words.append(self._read(n).decode("utf-8", "replace"))

    @staticmethod
    def _attrs(words):
        d = {}
        for w in words:
            if w.startswith("="):
                k, _, v = w[1:].partition("=")
                d[k] = v
            elif w.startswith(".tag="):
                pass
        return d

    def talk(self, words, raise_trap=True):
        """Send one command; returns ([(reply type, attrs)], attrs of !done)."""
        self._send(words)
        replies = []
        while True:
            s = self._sentence()
            if not s:
                continue
            kind, attrs = s[0], self._attrs(s[1:])
            if kind == "!done":
                if raise_trap:
                    trap = next((r for t, r in replies if t == "!trap"), None)
                    if trap is not None:
                        raise ApiError(trap.get("message", "The router refused the command."))
                return replies, attrs
            if kind == "!fatal":
                raise ApiError("RouterOS: " + " ".join(s[1:]))
            if kind == "!empty":
                continue
            replies.append((kind, attrs))

    def print(self, path, *args):
        replies, _ = self.talk([path.rstrip("/") + ("" if path.endswith(("/print", "/getall")) else "/print"), *args])
        return [r for t, r in replies if t == "!re"]

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


class RestApi:
    """RouterOS v7 REST (/rest on the www-ssl or www service), with the same talk()/print() as Api."""

    def __init__(self, host, username, password, port=443, tls=True, timeout=20):
        import base64
        import http.client
        self.host, self.port, self.tls, self.timeout = host, int(port), tls, timeout
        self.auth = "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()
        self._http = http.client
        self.conn = None
        try:
            self.talk(["/system/identity/print"])
        except ApiError as exc:
            self.close()
            if "401" in str(exc):
                raise ApiError("MikroTik login failed: invalid user name or password, or the user's group lacks the rest-api policy.") from exc
            raise

    def _connect(self):
        if self.tls:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE         # RouterOS uses a self-signed certificate by default
            return self._http.HTTPSConnection(self.host, self.port, timeout=self.timeout, context=ctx)
        return self._http.HTTPConnection(self.host, self.port, timeout=self.timeout)

    def talk(self, words, raise_trap=True):
        import json
        path, body, query = words[0].strip("/"), {}, []
        for w in words[1:]:
            if w.startswith("="):
                k, _, v = w[1:].partition("=")
                body[k] = v
            elif w.startswith("?"):
                query.append(w[1:])
        if query:
            body[".query"] = query
        data = json.dumps(body).encode()
        for attempt in (1, 2):
            try:
                if self.conn is None:
                    self.conn = self._connect()
                self.conn.request("POST", "/rest/" + path, body=data,
                                  headers={"Authorization": self.auth, "Content-Type": "application/json"})
                resp = self.conn.getresponse()
                raw = resp.read()
                break
            except (OSError, self._http.HTTPException) as exc:
                self.close()
                if attempt == 2:
                    raise ApiError(f"Could not reach the RouterOS REST API on {self.host}:{self.port}: {exc}. "
                                   f"Check that /ip service {'www-ssl' if self.tls else 'www'} is enabled and allowed from this server.") from exc
        try:
            out = json.loads(raw or b"[]")
        except ValueError:
            out = None
        if resp.status >= 400:
            msg = (out or {}).get("detail") or (out or {}).get("message") if isinstance(out, dict) else ""
            if resp.status == 401:
                raise ApiError("401 unauthorized")
            if raise_trap:
                raise ApiError(msg or f"The router refused the command (HTTP {resp.status}).")
            return [("!trap", {"message": msg or str(resp.status)})], {}
        if isinstance(out, dict):
            if set(out) == {"ret"}:
                return [], {"ret": str(out["ret"])}
            out = [out]
        return [("!re", {k: str(v).lower() if isinstance(v, bool) else str(v) for k, v in r.items()}) for r in (out or [])], {}

    def print(self, path, *args):
        replies, _ = self.talk([path.rstrip("/") + ("" if path.endswith(("/print", "/getall")) else "/print"), *args])
        return [r for t, r in replies if t == "!re"]

    def close(self):
        try:
            if self.conn:
                self.conn.close()
        except Exception:
            pass
        self.conn = None


TRANSPORTS = {"api": (API_PORT, "API"), "api-ssl": (API_TLS_PORT, "API-SSL"), "rest": (443, "REST"), "rest-http": (80, "REST (http)")}


def open_api(host, username, password, transport="api", port=None):
    default, _ = TRANSPORTS.get(transport, TRANSPORTS["api"])
    port = int(port or default)
    if transport == "rest":
        return RestApi(host, username, password, port, tls=True)
    if transport == "rest-http":
        return RestApi(host, username, password, port, tls=False)
    return Api(host, username, password, port, tls=transport == "api-ssl")


# ---- helpers ----------------------------------------------------------------------------------------------------
def _int(v, default=None):
    try:
        return int(str(v).split(".")[0])
    except (TypeError, ValueError):
        return default


def _float(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _yes(v):
    return str(v).lower() in ("true", "yes")


def ros_seconds(text):
    """RouterOS durations: '1w2d03:04:05', '3d4h5m6s', '00:01:02', '5m30s' -> seconds."""
    if not text:
        return 0
    t, total = str(text), 0
    for n, u in re.findall(r"(\d+)([wdhms])(?!s)", re.sub(r"\d+ms", "", t)):
        total += int(n) * {"w": 604800, "d": 86400, "h": 3600, "m": 60, "s": 1}[u]
    m = re.search(r"(\d+):(\d\d):(\d\d)", t)
    if m:
        total += int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
    return total


def _human_uptime(sec):
    d, h, m = sec // 86400, sec // 3600 % 24, sec // 60 % 60
    return f"{d} day{'s' if d != 1 else ''}, {h} hour{'s' if h != 1 else ''}, {m} minute{'s' if m != 1 else ''}"


def _updown(sec):
    if not sec:
        return "0000h00m"
    return f"{sec // 86400}d{sec // 3600 % 24:02d}h" if sec >= 86400 else f"{sec // 3600:02d}:{sec // 60 % 60:02d}:{sec % 60:02d}"


def _rate_mbps(rate):
    """'1Gbps', '10Gbps', '100Mbps', '2.5Gbps' -> Mbit/s."""
    m = re.match(r"([\d.]+)\s*([GM])", str(rate or ""))
    if not m:
        return 0
    return int(float(m.group(1)) * (1000 if m.group(2) == "G" else 1))


def if_kind(ros_type, name):
    t = (ros_type or "").lower()
    if t in ("ether", "sfp", "sfp-sfpplus", "wlan", "wifi", "lte", "cap"):
        return "ethernet"
    if t == "bond":
        return "eth-trunk"
    if t == "vlan":
        return "sub-interface"
    if t in ("bridge", "vpls"):
        return "vlanif"
    if t in ("lo", "loopback") or name == "lo":
        return "loopback"
    return "tunnel" if t else "ethernet"


def _mac(v):
    return (v or "").lower()


def mt_read_only(command):
    """RouterOS console commands the app lets through: print, monitor (once), export, ping, traceroute."""
    c = " ".join((command or "").split())
    if not c or len(c) > 300 or any(ord(ch) < 32 for ch in c):
        return False
    if re.match(r"^(ping|tracert|traceroute)\b", c):
        return True
    words = [w for w in re.split(r"[ /]", c.split(" where ")[0]) if w]
    return any(w in READ_VERBS for w in words) and not any(w in ("set", "add", "remove", "disable", "enable", "reset", "reboot",
                                                                     "shutdown", "import", "run", "unset", "move", "edit",
                                                                     "file", "upgrade", "install") for w in words)


# ---- the router -------------------------------------------------------------------------------------------------
class MikroTikRouter(Router):
    vendor = "mikrotik"
    via = "API"
    can_configure = False
    rates_all = True             # every interface's traffic is polled, like SNMP

    def __init__(self, host, username, password, transport="api", port=None, ssh_port=22, poll=True):
        super().__init__(host)
        self.transport = transport if transport in TRANSPORTS else "api"
        self.api_port, self.ssh_port = int(port or TRANSPORTS[self.transport][0]), int(ssh_port or 22)
        self._login = (username, password)        # memory only: reconnecting, and SSH for /export
        self.api = open_api(host, username, password, self.transport, self.api_port)
        self.via = TRANSPORTS[self.transport][1]
        self.counters = {}       # interface -> (t, rx bytes, tx bytes)
        self.util = {}           # interface -> (in %, out %)
        self.speeds = {}         # interface -> Mbit/s
        self.ssh = None
        self.major = 7
        try:
            ver = self.api.print("/system/resource")[0].get("version", "")
            self.major = _int(ver.split(".")[0], 7) or 7
        except (ApiError, IndexError):
            pass
        if poll:
            self.start_poller()

    # ---- API access -----------------------------------------------------------
    def q(self, path, *args, ttl=4):
        """A cached /print, reconnecting once if the router closed the connection."""
        key = "api:" + path + "|" + "|".join(args)
        hit = self.cache.get(key)
        if ttl and hit and time.time() - hit[0] < ttl:
            return hit[1]
        with self.lock:
            try:
                out = self.api.print(path, *args)
            except ApiError as exc:
                if "closed" not in str(exc):
                    raise
                out = self._reconnect().print(path, *args)
            except OSError:
                out = self._reconnect().print(path, *args)
        self.cache[key] = (time.time(), out)
        return out

    def q_or_empty(self, path, *args, ttl=10):
        """For menus that may not exist (v6 vs v7, packages not installed): [] instead of an error."""
        try:
            return self.q(path, *args, ttl=ttl)
        except DeviceError:
            return []

    def _reconnect(self):
        self.api.close()
        self.api = open_api(self.host, *self._login, self.transport, self.api_port)
        return self.api

    def close(self):
        super().close()
        self.api.close()
        if self.ssh:
            try:
                self.ssh.disconnect()
            except Exception:
                pass

    # ---- poller ----------------------------------------------------------------
    def _resource(self, ttl=3):
        rows = self.q("/system/resource", ttl=ttl)
        return rows[0] if rows else {}

    def _cpu_mem(self):
        r = self._resource()
        total, free = _int(r.get("total-memory"), 0), _int(r.get("free-memory"), 0)
        mem = round((total - free) / total * 100) if total else None
        return _int(r.get("cpu-load")), mem, total, free

    def temps(self, ttl=30):
        rows = self.q_or_empty("/system/health", ttl=ttl)
        out = []
        for r in rows:
            if "name" in r and "value" in r:         # v7: one row per sensor
                if "temperature" in r["name"]:
                    t = _float(r["value"])
                    if t is not None:
                        out.append({"slot": "", "board": r["name"].replace("-temperature", "").replace("temperature", "").strip("-") or "board",
                                    "sensor": r["name"], "status": "Normal", "temp": t, "minor": None, "major": None})
            else:                                    # v6: one row with a field per sensor
                for k, v in r.items():
                    if "temperature" in k and _float(v) is not None:
                        out.append({"slot": "", "board": k.replace("-temperature", "").replace("temperature", "") or "board",
                                    "sensor": k, "status": "Normal", "temp": _float(v), "minor": None, "major": None})
        return out

    def _ifaces(self, ttl=4):
        return self.q("/interface", ttl=ttl)

    def _poll_once(self, t, tick):
        cpu, mem, _, _ = self._cpu_mem()
        self.history["cpu"].append((t, cpu))
        self.history["memory"].append((t, mem))
        if tick % 6 == 0:
            temps = self.temps(0)
            if temps:
                self.history["temperature"].append((t, max(r["temp"] for r in temps)))
        if tick % 60 == 0:
            self._read_speeds()
        if tick % 2 == 0:
            for r in self._ifaces(0):
                name, i, o = r.get("name", ""), _int(r.get("rx-byte")), _int(r.get("tx-byte"))
                if i is None or o is None:
                    continue
                prev = self.counters.get(name)
                self.counters[name] = (t, i, o)
                if not prev or t <= prev[0]:
                    continue
                dt = t - prev[0]
                rx, tx = max(0, i - prev[1]) * 8 / dt, max(0, o - prev[2]) * 8 / dt
                self.history["bps:" + name].append((t, round(rx), round(tx)))
                cap = (self.speeds.get(name) or 0) * 1e6
                ui, uo = (round(rx / cap * 100, 2), round(tx / cap * 100, 2)) if cap else (0.0, 0.0)
                self.util[name] = (ui, uo)
                self.history["util:" + name].append((t, ui, uo))

    def _read_speeds(self):
        """Link speeds of running Ethernet ports (one /interface/ethernet/monitor =once= call)."""
        eth = [r["name"] for r in self.q_or_empty("/interface/ethernet", ttl=300) if _yes(r.get("running", "true"))]
        if not eth:
            return
        try:
            with self.lock:
                replies, _ = self.api.talk(["/interface/ethernet/monitor", "=numbers=" + ",".join(eth), "=once="])
            for _, r in replies:
                if r.get("name") and r.get("rate"):
                    self.speeds[r["name"]] = _rate_mbps(r["rate"])
        except (DeviceError, OSError):
            pass

    # ---- commands (console, ping page, config backup) ---------------------------
    def _run(self, command, read_timeout):
        c = " ".join(command.split())
        if c.startswith(("display current-configuration", "/export", "export")):
            return self.export()
        m = re.match(r"^ping\s+(.*)$", c)
        if m:
            return self._ping(m.group(1))
        m = re.match(r"^(?:tracert|traceroute)\s+(.*)$", c)
        if m:
            return self._trace(m.group(1))
        if c.startswith(("display ", "dis ", "disp ")):
            raise DeviceError("This is a MikroTik router: use RouterOS commands, e.g. /ip route print or /interface print.")
        return self._console(c)

    def _console(self, c):
        if not mt_read_only(c):
            raise DeviceError("The console runs print, monitor, export, ping and traceroute only. Changes to MikroTik routers are not supported yet.")
        head, _, where = c.partition(" where ")
        tokens = [w for w in re.split(r"[ ]", head) if w]
        path, args = [], []
        for tok in tokens:
            if "=" in tok:
                args.append("=" + tok)
            elif tok in ("detail", "terse", "without-paging", "brief", "value-list"):
                continue
            elif tok == "count-only":
                args.append("=count-only=")
            elif tok == "once":
                args.append("=once=")
            elif path and path[-1] in READ_VERBS:      # "/interface ethernet monitor sfp28-1 once"
                args.append("=numbers=" + tok)
            else:
                path += [p for p in tok.split("/") if p]
        if not path or path[-1] not in READ_VERBS:
            raise DeviceError("End the command with print, e.g. /ip address print.")
        if path[-1] == "monitor" and "=once=" not in args:
            args.append("=once=")
        for cond in re.findall(r"([\w.-]+)\s*=\s*(\"[^\"]*\"|\S+)", where):
            args.append(f"?{cond[0]}={cond[1].strip(chr(34))}")
        replies, done = self.api.talk(["/" + "/".join(path), *args])
        rows = [r for t, r in replies if t == "!re"]
        if "ret" in done and not rows:
            return done["ret"]
        if not rows:
            return "(nothing)"
        return "\n".join(f"{i:>3} " + " ".join(f"{k}={v}" for k, v in r.items() if not k.startswith(".")) for i, r in enumerate(rows))

    @staticmethod
    def _opts(text):
        """Huawei style options from the ping page (-c 5 -s 100 -a 10.0.0.1 -m 15 -q 1 -vpn-instance X) -> dict + target."""
        parts, opts, target = text.split(), {}, ""
        i = 0
        while i < len(parts):
            p = parts[i]
            if p.startswith("-") and i + 1 < len(parts):
                opts[p.lstrip("-")] = parts[i + 1]
                i += 2
            else:
                k, eq, v = p.partition("=")
                if eq:
                    opts[k] = v
                else:
                    target = p
                i += 1
        if not re.match(r"^[A-Za-z0-9.:\-]{1,253}$", target or ""):
            raise DeviceError("Give an address or name to reach.")
        return opts, target

    def _ping(self, text):
        opts, target = self._opts(text)
        count = min(max(_int(opts.get("c") or opts.get("count"), 5), 1), 50)
        args = ["/ping", f"=address={target}", f"=count={count}"]
        if opts.get("s") or opts.get("size"):
            args.append(f"=size={min(max(_int(opts.get('s') or opts.get('size'), 56), 28), 9000)}")
        if opts.get("a") or opts.get("src-address"):
            args.append(f"=src-address={opts.get('a') or opts.get('src-address')}")
        if opts.get("vpn-instance") or opts.get("routing-table"):
            args.append(f"=vrf={opts.get('vpn-instance') or opts.get('routing-table')}" if self.major >= 7 else
                        f"=routing-table={opts.get('vpn-instance') or opts.get('routing-table')}")
        replies, _ = self.api.talk(args)
        rows = [r for t, r in replies if t == "!re"]
        lines = [f"PING {target}: {count} packets"]
        for r in rows:
            if r.get("time"):
                lines.append(f"  Reply from {r.get('host', target)}: bytes={r.get('size', '')} ttl={r.get('ttl', '')} time={r.get('time')}")
            elif r.get("status"):
                lines.append(f"  {r.get('status')}")
        last = rows[-1] if rows else {}
        lines.append(f"--- {target} ping statistics ---")
        lines.append(f"  {last.get('sent', count)} packet(s) transmitted, {last.get('received', 0)} received, {last.get('packet-loss', '100')}% packet loss")
        if last.get("avg-rtt"):
            lines.append(f"  round-trip min/avg/max = {last.get('min-rtt', '')}/{last.get('avg-rtt')}/{last.get('max-rtt', '')}")
        return "\n".join(lines)

    def _trace(self, text):
        opts, target = self._opts(text)
        args = ["/tool/traceroute", f"=address={target}", "=count=" + str(min(max(_int(opts.get("q"), 1), 1), 3)),
                "=max-hops=" + str(min(max(_int(opts.get("m"), 15), 1), 30))]
        if opts.get("a"):
            args.append(f"=src-address={opts['a']}")
        replies, _ = self.api.talk(args)
        hops = {}
        for _, r in replies:      # the router sends the table again after each round: keep the newest row per hop
            if r.get(".section") is not None or r.get("address") is not None:
                hops[r.get(".section", str(len(hops)))] = r
        lines = [f"traceroute to {target}"]
        for i, r in enumerate(hops.values(), 1):
            lines.append(f" {i:>2}  {r.get('address') or '*':<18} loss {r.get('loss', '')}  avg {r.get('avg', '')} ms  {r.get('status', '')}".rstrip())
        return "\n".join(lines)

    def export(self):
        """The configuration as /export prints it, over SSH (the API cannot print it). Sensitive values stay hidden."""
        with_lock = False
        try:
            if self.ssh is None or not self.ssh.is_alive():
                from netmiko import ConnectHandler
                self.ssh = ConnectHandler(device_type="mikrotik_routeros", host=self.host, port=self.ssh_port,
                                          username=self._login[0] + "+ct", password=self._login[1], timeout=30, conn_timeout=15)
            with_lock = True
            return self.ssh.send_command("/export terse" if self.major < 7 else "/export", read_timeout=180)
        except Exception as exc:
            if with_lock:
                raise DeviceError(f"Reading /export over SSH failed: {exc}") from exc
            raise DeviceError(f"The configuration backup reads /export over SSH (port {self.ssh_port}) with the same login: {exc}") from exc

    def check_table_size(self, command):
        if re.search(r"\broute\b.*\bprint\b", command) and "where" not in command and "count-only" not in command:
            n = self._route_count()
            if n and n > ROUTE_LIST_LIMIT:
                raise DeviceError(f"That table has {n:,} routes, too many to print here. Add a filter, e.g. "
                                  "/ip route print where dst-address=8.8.8.0/24, or add count-only.")

    def _route_count(self):
        try:
            with self.lock:
                _, done = self.api.talk(["/ip/route/print", "=count-only="])
            return _int(done.get("ret"))
        except (DeviceError, OSError):
            return None

    def execute(self, command):
        raise DeviceError("Changes to MikroTik routers are not supported yet.")

    def _configure(self, lines, commit):
        raise DeviceError("Changes to MikroTik routers are not supported yet.")

    # ---- views -----------------------------------------------------------------
    def sysname(self):
        rows = self.q("/system/identity", ttl=60)
        return (rows[0].get("name") if rows else "") or self.host

    def _addresses(self):
        out = {}
        for r in self.q_or_empty("/ip/address", ttl=30):
            if not _yes(r.get("disabled")):
                out.setdefault(r.get("interface", ""), []).append(r.get("address", ""))
        return out

    def _addresses6(self):
        out = {}
        for r in self.q_or_empty("/ipv6/address", ttl=60):
            if not _yes(r.get("disabled")) and not (r.get("address", "").lower().startswith("fe80")):
                out.setdefault(r.get("interface", ""), []).append(r.get("address", ""))
        return out

    def _brief(self):
        vlans = {r.get("name"): r for r in self.q_or_empty("/interface/vlan", ttl=60)}
        rows = []
        for r in self._ifaces():
            name = r.get("name", "")
            kind = if_kind(r.get("type"), name)
            disabled, running = _yes(r.get("disabled")), _yes(r.get("running"))
            ui, uo = self.util.get(name, (0.0, 0.0))
            v = vlans.get(name) or {}
            rows.append({"name": name, "phy": "*down" if disabled else ("up" if running else "down"),
                         "protocol": "up" if running else "down", "in_util": ui, "out_util": uo,
                         "in_errors": _int(r.get("rx-error"), 0), "out_errors": _int(r.get("tx-error"), 0),
                         "admin_down": disabled, "type": kind, "speed_mbps": self.speeds.get(name, 0),
                         "parent": v.get("interface", ""), "_r": r, "_vlan": v.get("vlan-id", ""), "ros_type": r.get("type", "")})
        return rows

    def overview(self):
        res = self._resource(10)
        board = self.q_or_empty("/system/routerboard", ttl=300)
        board = board[0] if board else {}
        ifs = self._brief()
        phys = [i for i in ifs if i["type"] in ("ethernet", "eth-trunk")]
        cpu, mem, total, free = self._cpu_mem()
        temps = self.temps()
        for i in ifs:
            i.pop("_r"), i.pop("_vlan")
        return {
            "host": self.host, "demo": False, "via": self.via, "vendor": self.vendor,
            "model": board.get("model") or res.get("board-name") or "MikroTik",
            "uptime": _human_uptime(ros_seconds(res.get("uptime"))),
            "vrp_version": res.get("version", "").split(" ")[0], "software": "RouterOS " + res.get("version", ""),
            "patch": board.get("current-firmware", ""),
            "cpu": cpu, "memory": {"percent": mem, "total_kb": total // 1024 if total else None,
                                   "used_kb": (total - free) // 1024 if total else None},
            "sysname": self.sysname(),
            "interfaces": {"total": len(phys), "up": sum(1 for i in phys if i["phy"] == "up"),
                           "admin_down": sum(1 for i in phys if i["admin_down"]), "all": len(ifs)},
            "bgp": self.bgp_peers(), "alarms": {"total": None, "critical": None},
            "max_temp": max((r["temp"] for r in temps), default=None),
            "top_interfaces": sorted([i for i in ifs if i["phy"] == "up" and i["type"] != "loopback"],
                                     key=lambda i: -(i["in_util"] + i["out_util"]))[:6],
            "poll_error": self.poll_error,
        }

    def resources(self, since=0):
        _, mem, total, free = self._cpu_mem()
        return {"cpu": self.series("cpu", since), "memory": self.series("memory", since),
                "temperature": self.series("temperature", since),
                "memory_detail": {"percent": mem, "total_kb": total // 1024 if total else None,
                                  "used_kb": (total - free) // 1024 if total else None},
                "temps": self.temps(), "poll_seconds": POLL_SECONDS, "poll_error": self.poll_error, "via": self.via}

    def interface_config(self):
        return {}

    def interfaces(self):
        v4, v6 = self._addresses(), self._addresses6()
        rows = []
        for row in self._brief():
            r, vlan = row.pop("_r"), row.pop("_vlan")
            addrs = v4.get(row["name"], [])
            row.update(ip=addrs[0] if addrs else "", ipv6=v6.get(row["name"], []), vpn="", description=r.get("comment", ""),
                       vlan=str(vlan or ""), mtu=str(r.get("actual-mtu") or r.get("mtu") or ""), l2=row["ros_type"] == "bridge",
                       secondary=addrs[1:],
                       util_history=[[x[1], x[2]] for x in list(self.history.get("util:" + row["name"], ()))[-30:]])
            rows.append(row)
        return rows

    def interface_detail(self, name):
        r = next((x for x in self._ifaces() if x.get("name") == name), None)
        if r is None:
            raise DeviceError(f"No interface {name}.")
        bps = list(self.history.get("bps:" + name, ()))
        ips = self._addresses().get(name, [])
        running, disabled = _yes(r.get("running")), _yes(r.get("disabled"))
        sp = self.speeds.get(name)
        d = {"name": name, "state": "Administratively DOWN" if disabled else ("UP" if running else "DOWN"),
             "protocol": "UP" if running else "DOWN", "description": r.get("comment", ""), "mtu": r.get("actual-mtu") or r.get("mtu"),
             "mac": _mac(r.get("mac-address")), "ip": ips[0] if ips else "", "speed": (f"{sp // 1000}G" if sp and sp >= 1000 else f"{sp}M") if sp else "",
             "duplex": "", "last_up": r.get("last-link-up-time", ""), "last_down": r.get("last-link-down-time", ""),
             "in_rate_bps": bps[-1][1] if bps else None, "out_rate_bps": bps[-1][2] if bps else None,
             "in_pps": None, "out_pps": None, "vlan": "",
             "input_bytes": _int(r.get("rx-byte")), "input_packets": _int(r.get("rx-packet")),
             "output_bytes": _int(r.get("tx-byte")), "output_packets": _int(r.get("tx-packet")),
             "crc_errors": _int(r.get("rx-error")), "in_discards": _int(r.get("rx-drop")), "config": [], "optics": self.optics().get(name)}
        d["raw"] = "\n".join(f"{k}={v}" for k, v in r.items() if not k.startswith(".")) + "\n(read over the RouterOS API)"
        return d

    def traffic(self, names, since=0):
        return {n: self.series("bps:" + n, since) for n in names}

    def optics(self):
        """SFP readings: one /interface/ethernet/monitor =once= for the SFP ports (cached a minute)."""
        hit = self.cache.get("optics")
        if hit and time.time() - hit[0] < 60:
            return hit[1]
        sfp = [r["name"] for r in self.q_or_empty("/interface/ethernet", ttl=300) if r.get("name", "").startswith(("sfp", "qsfp"))]
        out = {}
        if sfp:
            try:
                with self.lock:
                    replies, _ = self.api.talk(["/interface/ethernet/monitor", "=numbers=" + ",".join(sfp), "=once="])
                for _, r in replies:
                    if r.get("sfp-rx-power") is None:
                        continue
                    out[r.get("name")] = {"rx_dbm": _float(r.get("sfp-rx-power")), "tx_dbm": _float(r.get("sfp-tx-power")),
                                          "rx_low": None, "rx_high": None, "tx_low": None, "tx_high": None,
                                          "temp": _float(r.get("sfp-temperature")), "vendor": r.get("sfp-vendor-name", ""),
                                          "part": r.get("sfp-vendor-part-number", ""), "wavelength": _float(r.get("sfp-wavelength"))}
            except (DeviceError, OSError):
                pass
        self.cache["optics"] = (time.time(), out)
        return out

    def vlans(self):
        ifs = self.interfaces()
        return {"vlans": [{"id": str(v.get("vlan-id", "")), "type": "common", "status": "enable", "ports": v.get("interface", ""),
                           "description": v.get("comment", "") or v.get("name", "")} for v in self.q_or_empty("/interface/vlan", ttl=60)],
                "subinterfaces": [i for i in ifs if i["type"] == "sub-interface"],
                "vlanifs": [i for i in ifs if i["type"] == "vlanif"], "l2ports": [],
                "parents": [i["name"] for i in ifs if i["type"] in ("ethernet", "eth-trunk")]}

    def qos(self):
        rules = []
        for s in self.q_or_empty("/queue/simple", ttl=30):
            up, _, down = (s.get("max-limit") or "0/0").partition("/")
            rules.append({"interface": s.get("target", ""), "description": s.get("name", ""), "kind": "rate-limit",
                          "direction": "both", "cir_kbps": _int(up, 0) // 1000, "pir_kbps": _int(down, 0) // 1000, "line": s.get("name", "")})
        return {"rules": rules, "profiles": [], "queues": [], "policies": "", "interfaces": [i.get("name") for i in self._ifaces()],
                "note": "Simple queues, read-only."}

    def queue_stats(self, name):
        return {"queues": [], "raw": NOT_HERE}

    def routes(self, vpn=None, lookup=None):
        args = []
        if vpn:
            args.append(f"?routing-table={vpn}" if self.major >= 7 else f"?routing-mark={vpn}")
        if lookup:
            q = lookup if "/" in lookup else lookup + "/32"
            args.append(f"?dst-address={q}")
        n = None if lookup else self._route_count()
        static = []
        if n and n > ROUTE_LIST_LIMIT:
            rows = self.q("/ip/route", *args, "?static=true", ttl=30) + self.q("/ip/route", *args, "?connect=true", ttl=30)
            summary = f"{n:,} prefixes. Only connected and static routes are listed. Use Lookup to find the route for an address."
            large = True
        else:
            rows, summary, large = self.q("/ip/route", *args, ttl=15), "", False
        out = []
        for r in rows:
            if _yes(r.get("disabled")):
                continue
            proto = ("Direct" if _yes(r.get("connect")) else "Static" if _yes(r.get("static")) else "BGP" if _yes(r.get("bgp"))
                     else "OSPF" if _yes(r.get("ospf")) else (r.get("type") or "").upper() or "?")
            gw = r.get("gateway", "")
            nh = r.get("immediate-gw", "").split("%")[0] or (gw if re.match(r"^[\d.]+$", gw) else "")
            iface = (r.get("immediate-gw", "").split("%")[1] if "%" in r.get("immediate-gw", "") else
                     gw if not re.match(r"^[\d.]+$", gw) else "")
            row = {"destination": r.get("dst-address", ""), "protocol": proto, "preference": _int(r.get("distance"), 0),
                   "cost": _int(r.get("scope"), 0), "flags": "A" if _yes(r.get("active")) else "",
                   "next_hop": nh, "interface": iface}
            out.append(row)
            if proto == "Static":
                dst = row["destination"]
                ip, _, plen = dst.partition("/")
                static.append({"vpn": r.get("routing-table", "") if r.get("routing-table", "main") != "main" else "", "prefix": ip,
                               "mask": plen, "next_hop": nh, "interface": iface, "preference": str(r.get("distance", "1")),
                               "description": r.get("comment", ""), "line": f"/ip route add dst-address={dst} gateway={gw}"})
        res = {"routes": out, "summary": summary or f"Destinations : {len(out)}", "static": static}
        if lookup:
            res["lookup"] = lookup
        if large:
            res.update(large=True, skipped=[])
        return res

    def arp(self):
        rows = []
        for r in self.q_or_empty("/ip/arp", ttl=10):
            rows.append({"ip": r.get("address", ""), "mac": (r.get("mac-address") or "Incomplete").lower() if r.get("mac-address") else "Incomplete",
                         "expire": "", "type": "D" if _yes(r.get("dynamic")) else "S", "interface": r.get("interface", ""),
                         "vpn": "", "vlan": ""})
        static = [f"/ip arp add address={r['ip']} mac-address={r['mac']} interface={r['interface']}" for r in rows if r["type"] == "S"]
        return {"entries": rows, "static": static}

    def neighbors(self):
        rows = []
        for r in self.q_or_empty("/ip/neighbor", ttl=30):
            rows.append({"local": r.get("interface", "").split(",")[0], "exptime": "", "neighbor_port": r.get("interface-name", ""),
                         "neighbor": r.get("identity") or r.get("address") or "-", "platform": r.get("platform", ""),
                         "board": r.get("board", ""), "address": r.get("address", ""), "version": r.get("version", "")})
        return {"lldp": rows}

    def vpn_instances(self):
        rows = self.q_or_empty("/ip/vrf", ttl=60) if self.major >= 7 else []
        return {"instances": [{"name": r.get("name", ""), "rd": "", "interfaces": r.get("interfaces", "")} for r in rows], "raw": "",
                "note": "VRFs (/ip vrf)."}

    # BGP: v7 /routing/bgp/session (+ connection for configured peers), v6 /routing/bgp/peer
    def bgp_peers(self):
        peers, local_as, rid = [], "", ""
        if self.major >= 7:
            sessions = self.q_or_empty("/routing/bgp/session", ttl=10)
            seen = set()
            for s in sessions:
                addr = (s.get("remote.address") or "").split("%")[0]
                seen.add(s.get("name", "").rsplit("-", 1)[0])
                local_as = local_as or s.get("local.as", "")
                rid = rid or s.get("local.id", "")
                est = _yes(s.get("established"))
                peers.append({"peer": addr, "as": s.get("remote.as", ""), "msg_rcvd": _int(s.get("remote.messages")),
                              "msg_sent": _int(s.get("local.messages")), "out_q": None,
                              "up_down": _updown(ros_seconds(s.get("uptime"))) if est else "0000h00m",
                              "state": "Established" if est else (s.get("state") or "Idle").capitalize(),
                              "prefixes": _int(s.get("prefix-count"), 0), "active": None, "advertised": None,
                              "name": s.get("name", "")})
            for c in self.q_or_empty("/routing/bgp/connection", ttl=60):     # configured peers with no session yet
                if c.get("name") in seen:
                    continue
                local_as = local_as or c.get("as", "") or c.get("local.as", "")
                addr = (c.get("remote.address") or "").split("/")[0]
                if addr and not any(p["peer"] == addr for p in peers):
                    peers.append({"peer": addr, "as": c.get("remote.as", ""), "msg_rcvd": None, "msg_sent": None, "out_q": None,
                                  "up_down": "0000h00m", "state": "Admin-down" if _yes(c.get("disabled")) else "Idle",
                                  "prefixes": 0, "active": None, "advertised": None, "name": c.get("name", "")})
        else:
            inst = self.q_or_empty("/routing/bgp/instance", ttl=300)
            if inst:
                local_as, rid = inst[0].get("as", ""), inst[0].get("router-id", "")
            for p in self.q_or_empty("/routing/bgp/peer", ttl=10):
                st = p.get("state", "idle")
                peers.append({"peer": p.get("remote-address", ""), "as": p.get("remote-as", ""), "msg_rcvd": None, "msg_sent": None,
                              "out_q": None, "up_down": _updown(ros_seconds(p.get("uptime"))),
                              "state": "Admin-down" if _yes(p.get("disabled")) else ("Established" if st == "established" else st.capitalize()),
                              "prefixes": _int(p.get("prefix-count"), 0), "active": None, "advertised": _int(p.get("updates-sent")),
                              "name": p.get("name", "")})
        return {"router_id": rid, "local_as": str(local_as), "total": len(peers),
                "established": sum(1 for p in peers if p["state"] == "Established"), "peers": peers}

    def bgp(self):
        b = self.bgp_peers()
        b["config"] = {}
        return b

    def bgp_routes(self, peer=None, which="received", lookup=None):
        args = ["?bgp=true"]
        if lookup:
            args.append(f"?dst-address={lookup if '/' in lookup else lookup + '/32'}")
        elif peer:
            if which == "advertised":
                return {"routes": [], "raw": "Advertised routes are not listed for MikroTik routers yet."}
            args.append(f"?gateway={peer}")
        if not lookup:
            try:
                with self.lock:
                    _, done = self.api.talk(["/ip/route/print", "=count-only=", *args])
                n = _int(done.get("ret"))
            except (DeviceError, OSError):
                n = None
            if n is None or n > ROUTE_LIST_LIMIT:
                return {"routes": [], "too_many": n, "raw": f"This would list {n:,} routes, more than the {ROUTE_LIST_LIMIT:,} this page shows at once. "
                        "Use Lookup with an address or prefix." if n else "Use Lookup with an address or prefix."}
        rows = self.q("/ip/route", *args, ttl=15)
        out = [{"network": r.get("dst-address", ""), "next_hop": r.get("gateway", ""), "med": r.get("bgp-med") or r.get("bgp.med", ""),
                "local_pref": r.get("bgp-local-pref") or r.get("bgp.local-pref", ""), "pref_val": "",
                "path": r.get("bgp-as-path") or r.get("bgp.as-path", ""), "status": "*>" if _yes(r.get("active")) else "*"} for r in rows]
        return {"routes": out, "raw": "", **({"lookup": lookup} if lookup else {})}

    def ospf(self):
        peers, ifs = [], []
        if self.major >= 7:
            for n in self.q_or_empty("/routing/ospf/neighbor", ttl=10):
                peers.append({"process": n.get("instance", ""), "area": n.get("area", ""), "interface": n.get("interface", ""),
                              "neighbor": n.get("router-id", "") or n.get("address", ""), "state": (n.get("state") or "").capitalize()})
            for i in self.q_or_empty("/routing/ospf/interface", ttl=60):
                ifs.append({"area": i.get("area", ""), "interface": i.get("interface", ""), "ip": i.get("address", ""),
                            "type": i.get("type", ""), "state": i.get("state", ""), "cost": _int(i.get("cost"), 0),
                            "priority": _int(i.get("priority"), 0), "dr": i.get("dr", ""), "bdr": i.get("bdr", "")})
        else:
            for n in self.q_or_empty("/routing/ospf/neighbor", ttl=10):
                peers.append({"process": n.get("instance", ""), "area": "", "interface": n.get("interface", ""),
                              "neighbor": n.get("router-id", ""), "state": (n.get("state") or "").capitalize()})
            for i in self.q_or_empty("/routing/ospf/interface", ttl=60):
                ifs.append({"area": "", "interface": i.get("interface", ""), "ip": "", "type": i.get("network-type", ""),
                            "state": i.get("state", ""), "cost": _int(i.get("cost"), 0), "priority": _int(i.get("priority"), 0),
                            "dr": i.get("dr-address", ""), "bdr": i.get("backup-dr-address", "")})
        lsa = self.q_or_empty("/routing/ospf/lsa", ttl=30)
        return {"peers": peers, "interfaces": ifs,
                "lsdb": "\n".join(f"{r.get('area', '')} {r.get('type', '')} {r.get('id', '')} {r.get('originator', '')}" for r in lsa[:500])}

    def isis(self):
        return {"configured": False, "processes": [], "interfaces": [], "peers": [], "routes": [], "lsdb": "", "note": NOT_HERE}

    def mpls(self):
        ldp = []
        for n in self.q_or_empty("/mpls/ldp/neighbor", ttl=30):
            ldp.append({"peer": n.get("peer", "") or n.get("transport", ""), "status": "Operational" if _yes(n.get("operational")) else "Down",
                        "lam": "", "ssn_role": "", "keepalive": "", "up_time": ""})
        return {"ldp": ldp, "lsp": ""}

    def track(self, detail=True):
        """For the change tracker: BGP sessions, connected and static routes, and the bridge MAC table."""
        out = {"bgp": self.bgp_peers()["peers"], "policies": []}
        try:
            r = self.routes()
            out["routes"] = [x for x in r["routes"] if x["protocol"] in ("Direct", "Static")]
        except DeviceError:
            out["routes"] = []
        out["mac"] = [{"mac": (h.get("mac-address") or "").lower(), "vlan": h.get("vid", ""), "interface": h.get("on-interface") or h.get("interface", ""),
                       "type": "dynamic" if _yes(h.get("dynamic", "true")) else "static"}
                      for h in self.q_or_empty("/interface/bridge/host", ttl=60)][:5000]
        return out

    def route_policies(self):
        filters = self.q_or_empty("/routing/filter/rule", ttl=60) if self.major >= 7 else self.q_or_empty("/routing/filter", ttl=60)
        return {"policies": [], "prefixes": [], "as_path": "", "community": "", "peers": [],
                "filters": [{"chain": f.get("chain", ""), "rule": f.get("rule", "") or " ".join(f"{k}={v}" for k, v in f.items() if not k.startswith("."))}
                            for f in filters]}

    def system(self):
        res = self._resource(30)
        clock = self.q_or_empty("/system/clock", ttl=10)
        ntp = self.q_or_empty("/system/ntp/client", ttl=60)
        users = [{"name": u.get("name", ""), "state": "Block" if _yes(u.get("disabled")) else "Active", "service": u.get("group", ""),
                  "level": u.get("group", "")} for u in self.q_or_empty("/user", ttl=60)]
        active = self.q_or_empty("/user/active", ttl=10)
        c = clock[0] if clock else {}
        n = ntp[0] if ntp else {}
        return {"users": users,
                "online": "\n".join(f"{a.get('name', '')}  {a.get('via', '')}  {a.get('address', '')}  since {a.get('when', '')}" for a in active),
                "clock": f"{c.get('date', '')} {c.get('time', '')} {c.get('time-zone-name', '')}".strip(),
                "ntp": " ".join(f"{k}={v}" for k, v in n.items() if not k.startswith(".")),
                "ntp_servers": [s for s in re.split(r"[,\s]+", n.get("servers", "") or n.get("primary-ntp", "")) if s and s != "0.0.0.0"],
                "snmp": " ".join(f"{k}={v}" for k, v in (self.q_or_empty("/snmp", ttl=300) or [{}])[0].items() if not k.startswith(".")),
                "sysname": self.sysname(), "version": res.get("version", "")}

    def hardware(self):
        res = self._resource(60)
        board = (self.q_or_empty("/system/routerboard", ttl=600) or [{}])[0]
        health = self.q_or_empty("/system/health", ttl=30)
        lines = []
        for h in health:
            if "name" in h:
                lines.append(f"{h['name']}: {h.get('value', '')}{h.get('type', '')}")
            else:
                lines += [f"{k}: {v}" for k, v in h.items() if not k.startswith(".")]
        boards = [{"slot": "0", "type": board.get("model") or res.get("board-name", "MikroTik"), "online": "Present",
                   "register": "", "status": "Normal", "role": ""}]
        return {"boards": boards, "temperature": self.temps(),
                "power": "\n".join(l for l in lines if re.search(r"psu|power|volt|current", l, re.I)) or "(no power readings)",
                "fan": "\n".join(l for l in lines if "fan" in l.lower()) or "(no fan readings)",
                "elabel": "\n".join(f"{k}: {v}" for k, v in {**board, "architecture": res.get("architecture-name", ""),
                                                             "cpu": res.get("cpu", ""), "cpu-count": res.get("cpu-count", "")}.items()
                                    if v and not k.startswith(".")),
                "boards_elabel": "", "optics": self.optics()}

    def logs(self):
        rows = self.q_or_empty("/log", ttl=5)[-500:]
        log = []
        for r in rows:
            topics = r.get("topics", "")
            sev = "3" if "error" in topics or "critical" in topics else "4" if "warning" in topics else "6"
            log.append({"time": r.get("time", ""), "host": self.sysname(), "tag": topics, "severity": sev, "message": r.get("message", "")})
        raw = "\n".join(f"{x['time']} {x['tag']} {x['message']}" for x in log)
        alarms = [{"seq": str(i), "time": x["time"], "id": "", "name": x["tag"], "level": "Critical" if x["severity"] == "3" else "Warning",
                   "state": "", "description": x["message"]} for i, x in enumerate(log) if x["severity"] in ("3",)][-50:]
        return {"alarms": alarms, "alarms_raw": "", "log": log, "log_raw": raw}


# ---- SNMP first, the API when someone signs in ----------------------------------------------------------------------
from snmp import text as _txt  # noqa: E402
from snmp_router import IF, IFX, SYS, SnmpRouter, SshRequired, _uptime  # noqa: E402

MT = "1.3.6.1.4.1.14988.1.1"
HR_CPU = "1.3.6.1.2.1.25.3.3.1.2"           # hrProcessorLoad, one per core
HR_STORE = "1.3.6.1.2.1.25.2.3.1"           # hrStorageTable: .3 descr, .4 units, .5 size, .6 used
MT_GAUGE = MT + ".3.100.1"                  # v7 mtxrGaugeTable: .2 name, .3 value, .4 unit (1 = celsius, 2 = rpm, 3 = dV, 4 = dA, 5 = dW, 6 = status)
MT_TEMP_V6 = (MT + ".3.10.0", MT + ".3.11.0")   # mtxrHlTemperature, mtxrHlProcessorTemperature (0.1 C)
MT_OPT = MT + ".19.1.1"                     # mtxrOpticalTable: .2 name, .6 temperature, .9 tx power, .10 rx power (0.001 dBm)
MT_NEIGH = MT + ".11.1.1"                   # mtxrNeighborTable: .2 address, .3 MAC, .6 identity, .8 interface ifIndex
MT_MODEL, MT_VERSION, MT_FIRMWARE, MT_SERIAL = MT + ".7.8.0", MT + ".4.4.0", MT + ".7.4.0", MT + ".7.3.0"


class ApiRequired(SshRequired):
    """This page needs the MikroTik API: the browser asks for a login and tries again."""


class MikroTikSnmpRouter(SnmpRouter):
    """A MikroTik read over SNMP (v2c community or v3). Signing in with a MikroTik login adds the API for BGP, OSPF,
    routes, logs, users and the console; that login is kept in memory only."""
    vendor = "mikrotik"
    rates_all = True
    can_configure = False

    def __init__(self, host, snmp, api_transport="api", api_port=None, ssh_port=22):
        self.api_transport, self.api_port_cfg = api_transport, api_port
        super().__init__(host, snmp, ssh_port)
        self.via = "SNMP"

    # the API login stands where the Huawei SSH login stands in SnmpRouter
    @property
    def can_configure(self):
        return False

    def attach_ssh(self, username, password):
        api = MikroTikRouter(self.host, username, password, self.api_transport, self.api_port_cfg, self.ssh_port, poll=False)
        api.lock = self.lock
        old, self.ssh, self.ssh_user = self.ssh, api, username
        self.via = "SNMP + " + api.via
        if old:
            old.close()
        self.cache.clear()

    def _need_ssh(self):
        if self.ssh is None:
            raise ApiRequired("This page needs the MikroTik API. Sign in with a MikroTik login (a read-only user is enough).")
        return self.ssh

    def _run(self, command, read_timeout):
        return self._need_ssh()._run(command, read_timeout)

    def check_table_size(self, command):
        if self.ssh:
            self.ssh.check_table_size(command)

    def execute(self, command):
        raise DeviceError("Changes to MikroTik routers are not supported yet.")

    def _configure(self, lines, commit):
        raise DeviceError("Changes to MikroTik routers are not supported yet.")

    def interface_config(self):
        return {}

    # ---- SNMP reads, MikroTik MIB ----------------------------------------------
    def _cpu_mem(self):
        loads = [v for v in self.walk(HR_CPU, 3).values() if isinstance(v, int)]
        cpu = round(sum(loads) / len(loads)) if loads else None
        descr, size, used = self.walk(HR_STORE + ".3", 600), self.walk(HR_STORE + ".5", 60), self.walk(HR_STORE + ".6", 3)
        idx = next((i for i, d in descr.items() if "memory" in _txt(d).lower()), "65536")
        mem = round(used[idx] / size[idx] * 100) if size.get(idx) and used.get(idx) is not None else None
        return cpu, mem

    def _mem_detail(self):
        descr, size, used, units = (self.walk(HR_STORE + c, 30) for c in (".3", ".5", ".6", ".4"))
        idx = next((i for i, d in descr.items() if "memory" in _txt(d).lower()), "65536")
        u = units.get(idx) or 1024
        if not size.get(idx):
            return {"percent": None, "total_kb": None, "used_kb": None}
        return {"percent": round(used.get(idx, 0) / size[idx] * 100), "total_kb": size[idx] * u // 1024, "used_kb": used.get(idx, 0) * u // 1024}

    def entities(self):
        return {}

    def temps(self, ttl=30):
        rows = []
        names, vals, units = (self._walk_or_empty_ttl(MT_GAUGE + c, ttl) for c in (".2", ".3", ".4"))
        for idx, n in names.items():
            if units.get(idx) == 1 and isinstance(vals.get(idx), int):
                rows.append({"slot": "", "board": _txt(n), "sensor": _txt(n), "status": "Normal", "temp": vals[idx],
                             "minor": None, "major": None})
        if not rows:   # v6
            got = self.get(*MT_TEMP_V6)
            for oid, label in zip(MT_TEMP_V6, ("board", "cpu")):
                if isinstance(got.get(oid), int):
                    rows.append({"slot": "", "board": label, "sensor": label, "status": "Normal", "temp": got[oid] / 10,
                                 "minor": None, "major": None})
        return rows

    def _walk_or_empty_ttl(self, oid, ttl):
        try:
            return self.walk(oid, ttl)
        except DeviceError:
            return {}

    def optics(self):
        names = self._walk_or_empty_ttl(MT_OPT + ".2", 300)
        if not names:
            return {}
        temp, tx, rx = (self._walk_or_empty_ttl(MT_OPT + c, 30) for c in (".6", ".9", ".10"))
        dbm = lambda v: round(v / 1000, 2) if isinstance(v, int) else None
        return {_txt(n): {"rx_dbm": dbm(rx.get(i)), "tx_dbm": dbm(tx.get(i)), "rx_low": None, "rx_high": None,
                          "tx_low": None, "tx_high": None, "temp": temp.get(i), "vendor": "", "part": "", "wavelength": None}
                for i, n in names.items()}

    def _info(self):
        got = self.get(SYS + ".1.0", SYS + ".3.0", SYS + ".5.0", MT_MODEL, MT_VERSION, MT_FIRMWARE, MT_SERIAL)
        return {k: got.get(o) for k, o in (("descr", SYS + ".1.0"), ("up", SYS + ".3.0"), ("name", SYS + ".5.0"),
                                           ("model", MT_MODEL), ("version", MT_VERSION), ("firmware", MT_FIRMWARE), ("serial", MT_SERIAL))}

    # ---- views ---------------------------------------------------------------
    def overview(self):
        i = self._info()
        ifs = self._brief()
        for x in ifs:
            x["type"] = self._kind(x["name"], x["_r"])
            x["parent"] = ""
        phys = [x for x in ifs if x["type"] in ("ethernet", "eth-trunk")]
        cpu, _ = self._cpu_mem()
        temps = self.temps()
        for x in ifs:
            x.pop("_r"), x.pop("_idx")
        version = _txt(i["version"]) or (re.search(r"RouterOS\s+(\S+)", _txt(i["descr"]) or "") or [None, ""])[1]
        return {"host": self.host, "demo": False, "via": self.via, "vendor": self.vendor,
                "model": _txt(i["model"]) or re.sub(r"^RouterOS\s*", "", _txt(i["descr"])) or "MikroTik",
                "uptime": _uptime(i["up"]), "vrp_version": version, "software": "RouterOS " + version,
                "patch": _txt(i["firmware"]), "cpu": cpu, "memory": self._mem_detail(),
                "sysname": _txt(i["name"]) or self.host,
                "interfaces": {"total": len(phys), "up": sum(1 for x in phys if x["phy"] == "up"),
                               "admin_down": sum(1 for x in phys if x["admin_down"]), "all": len(ifs)},
                "bgp": self.bgp_peers(), "alarms": {"total": None, "critical": None},
                "max_temp": max((r["temp"] for r in temps), default=None),
                "top_interfaces": sorted([x for x in ifs if x["phy"] == "up" and x["type"] != "loopback"],
                                         key=lambda x: -(x["in_util"] + x["out_util"]))[:6],
                "poll_error": self.poll_error}

    IFTYPE = {6: "ethernet", 161: "eth-trunk", 135: "sub-interface", 209: "vlanif", 24: "loopback", 131: "tunnel", 23: "tunnel", 71: "ethernet"}

    def _kind(self, name, r):
        t = r.get("iftype")
        if name == "lo":
            return "loopback"
        return self.IFTYPE.get(t, "tunnel" if t else "ethernet")

    def if_table(self, ttl=8):
        rows = super().if_table(ttl)
        types = self.walk(IF + ".3", 300)
        for idx, r in rows.items():
            r["iftype"] = types.get(idx)
        return rows

    def interfaces(self):
        rows = super().interfaces()
        types = {r["name"]: r["iftype"] for r in self.if_table().values()}
        for r in rows:
            r["type"] = self._kind(r["name"], {"iftype": types.get(r["name"])})
            r["parent"] = ""
        if self.ssh:    # names of VLAN parents and comments from the API
            try:
                vl = {v.get("name"): v for v in self.ssh.q_or_empty("/interface/vlan", ttl=60)}
                for r in rows:
                    if r["name"] in vl:
                        r["parent"], r["vlan"] = vl[r["name"]].get("interface", ""), str(vl[r["name"]].get("vlan-id", ""))
            except DeviceError:
                pass
        return rows

    def resources(self, since=0):
        return {"cpu": self.series("cpu", since), "memory": self.series("memory", since),
                "temperature": self.series("temperature", since), "memory_detail": self._mem_detail(),
                "temps": self.temps(), "poll_seconds": POLL_SECONDS, "poll_error": self.poll_error, "via": self.via}

    def neighbors(self):
        if self.ssh:
            return self.ssh.neighbors()
        names = {i: _txt(v) for i, v in self.walk(IFX + ".1", 300).items()}
        addr, ident, ifx = (self._walk_or_empty_ttl(MT_NEIGH + c, 30) for c in (".2", ".6", ".8"))
        rows = [{"local": names.get(str(ifx.get(i)), str(ifx.get(i) or "")), "exptime": "", "neighbor_port": "",
                 "neighbor": _txt(ident.get(i)) or (a if isinstance(a, str) else "-"), "address": a if isinstance(a, str) else ""}
                for i, a in addr.items()]
        return rows and {"lldp": rows} or super().neighbors()

    def arp(self):
        if self.ssh:
            return self.ssh.arp()
        saved, self.ssh = self.ssh, None
        try:
            return super().arp()
        finally:
            self.ssh = saved

    def bgp_peers(self):
        if self.ssh:
            try:
                return self.ssh.bgp_peers()
            except DeviceError:
                pass
        try:
            return super().bgp_peers()        # BGP4-MIB, where the router has it
        except DeviceError:
            return {"router_id": "", "local_as": "", "total": 0, "established": 0, "peers": []}

    def bgp(self):
        b = self.bgp_peers()
        b["config"] = {}
        if not self.ssh and not b["peers"]:
            b["note"] = "Sign in with a MikroTik login to read BGP sessions over the API."
        return b

    def track(self, detail=True):
        return self.ssh.track(detail) if self.ssh else {"bgp": self.bgp_peers()["peers"]}

    def hardware(self):
        i = self._info()
        h = self.ssh.hardware() if self.ssh else None
        if h:
            h["optics"] = h.get("optics") or self.optics()
            return h
        names, vals, units = (self._walk_or_empty_ttl(MT_GAUGE + c, 30) for c in (".2", ".3", ".4"))
        lines = [f"{_txt(n)}: {vals.get(k)}" for k, n in names.items()]
        return {"boards": [{"slot": "0", "type": _txt(i["model"]) or "MikroTik", "online": "Present", "register": "", "status": "Normal", "role": ""}],
                "temperature": self.temps(),
                "power": "\n".join(l for l in lines if re.search(r"psu|power|volt|current", l, re.I)) or "(no power readings)",
                "fan": "\n".join(l for l in lines if "fan" in l.lower()) or "(no fan readings)",
                "elabel": "\n".join(f"{k}: {_txt(v)}" for k, v in (("model", i["model"]), ("serial", i["serial"]), ("firmware", i["firmware"]),
                                                                   ("software", i["version"])) if _txt(v)),
                "boards_elabel": "", "optics": self.optics()}

    def _api_view(name):
        def f(self, *a, **k):
            return getattr(self._need_ssh(), name)(*a, **k)
        return f

    routes = _api_view("routes")
    bgp_routes = _api_view("bgp_routes")
    ospf = _api_view("ospf")
    logs = _api_view("logs")
    system = _api_view("system")
    route_policies = _api_view("route_policies")
    vlans = _api_view("vlans")
    qos = _api_view("qos")
    mpls = _api_view("mpls")
    vpn_instances = _api_view("vpn_instances")

    def isis(self):
        return {"configured": False, "processes": [], "interfaces": [], "peers": [], "routes": [], "lsdb": "", "note": NOT_HERE}

    def queue_stats(self, name):
        return {"queues": [], "raw": NOT_HERE}
