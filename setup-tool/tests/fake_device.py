"""A pretend factory-new Huawei device for tests: SSH and Telnet, VRP8 or VRP5.

It forces a password change at first login, remembers committed config, and treats
'ip address' on the management interface as moving the device (the session drops).
"""
import re
import socket
import threading

import paramiko


class Device:
    def __init__(self, platform="vrp8", user="root", password="Changeme_123", force_change=True):
        self.platform, self.users, self.force = platform, {user: password}, {user} if force_change else set()
        self.sysname = "HUAWEI"
        self.running, self.saved, self.saves = [], [], 0
        self.lock = threading.Lock()

    def run_cli(self, io, user):
        """io: object with write(str) and readline() -> str or None."""
        cand, view = [], "user"
        p = lambda: f"<{self.sysname}>" if view == "user" else (f"[{'*' if cand else '~'}{self.sysname}]" if self.platform == "vrp8" else f"[{self.sysname}]")
        io.write("\r\nInfo: The max number of VTY users is 10.\r\n" + p())
        while True:
            line = io.readline()
            if line is None:
                return
            l = line.strip()
            out = ""
            if not l:
                pass
            elif l == "screen-length 0 temporary":
                out = "Info: The configuration takes effect on the current user terminal interface only."
            elif l == "display version":
                v = "8.220 (NE8000 V800R022C00SPC600)" if self.platform == "vrp8" else "5.170 (S5735 V200R022C00SPC500)"
                m = "HUAWEI NE8000 M1C" if self.platform == "vrp8" else "HUAWEI S5735-L48T4X-A1"
                out = f"Huawei Versatile Routing Platform Software\r\nVRP (R) software, Version {v}\r\nCopyright (C) 2012-2022 Huawei Technologies Co., Ltd.\r\n{m} uptime is 0 days, 0 hours, 3 minutes"
            elif l == "display esn":
                out = "ESN of slot mainboard: 2102351931P0K8000123"
            elif l == "system-view":
                view = "system"
                out = "Enter system view, return user view with return command."
            elif l == "return":
                if self.platform == "vrp8" and cand:
                    io.write("Warning: Uncommitted configurations found, commit them before exiting? [Y(yes)/N(no)/C(cancel)]:")
                    a = (io.readline() or "").strip().upper()
                    if a == "Y":
                        self._commit(cand, io)
                    if a in ("Y", "N"):
                        cand, view = [], "user"
                else:
                    view = "user"
            elif l == "commit" and view == "system":
                if self._commit(cand, io):
                    return
                cand = []
            elif l == "save":
                io.write("Warning: The current configuration will be written to the device. Continue? [Y/N]:")
                if (io.readline() or "").strip().upper() == "Y":
                    self.saved, self.saves = list(self.running), self.saves + 1
                    out = "Now saving the current configuration to the slot 17\r\nInfo: Save the configuration successfully."
            elif l == "quit" and view == "user":
                return
            elif view == "user" and self.network is not None and re.match(r"^(stelnet|telnet)( -a \S+)? (\S+)$", l):
                out = self._hop(io, l)
            elif view == "user" and l.startswith("ping ") and self.network is not None:
                ip = l.split()[-1]
                out = (f"  PING {ip}: 56  data bytes\r\n    Reply from {ip}: bytes=56 Sequence=1 ttl=255 time=1 ms\r\n  --- {ip} ping statistics ---\r\n    0.00% packet loss"
                       if ip in self.network else f"  PING {ip}: 56  data bytes\r\n    Request time out\r\n  --- {ip} ping statistics ---\r\n    100.00% packet loss")
            elif view == "user" and l == "display current-configuration | include first-time":
                out = "ssh client first-time enable" if "ssh client first-time enable" in self.running else ""
            elif view == "user" and l.startswith("display arp interface"):
                out = "IP ADDRESS      MAC ADDRESS    EXPIRE(M) TYPE        INTERFACE      VPN-INSTANCE\r\n" + "".join(
                    f"{ip:<16}00e0-fc12-3456  20        D-0         {l.split()[-1]}\r\n" for ip in self.network)
            elif view == "system":
                if l.startswith("bogus") or l.startswith("abort"):
                    out = "                ^\r\nError: Unrecognized command found at '^' position."
                elif l.startswith("rsa local-key-pair create"):
                    io.write("The key name will be: HUAWEI_Host\r\nThe range of public key size is (2048 ~ 4096).\r\nInput the bits in the modulus [default = 3072]:")
                    io.readline()
                elif l == "undo telnet server enable":
                    io.write("Warning: Telnet server will be disabled. Continue? [Y/N]:")
                    io.readline()
                if self.platform == "vrp8":
                    if "Error" not in out:
                        cand.append(l)
                elif "Error" not in out:
                    if self._apply([l], io):
                        return
            else:
                out = "Error: Unrecognized command found at '^' position."
            io.write(("\r\n" + out if out else "") + "\r\n" + p())

    home = None           # on a device behind a router: that router's network dict
    network = None        # on a router: {ip: Device} of the devices behind its ports

    def _hop(self, io, line):
        parts = line.split()
        ssh, ip = parts[0] == "stelnet", parts[-1]
        user = ""
        if ssh:
            io.write("\r\nPlease input the username:")
            user = (io.readline() or "").strip()
        io.write(f"\r\nTrying {ip} ...\r\nPress CTRL+K to abort\r\n")
        dev = self.network.get(ip)
        if dev is None:
            return "Error: Failed to connect to the remote host."
        if ssh:
            if "ssh client first-time enable" not in self.running:
                return "Error: Failed to verify the server's public key."
            io.write(f"Connected to {ip} ...\r\nThe server is not authenticated. Continue to access it? [Y/N]:")
            if (io.readline() or "").strip().upper() != "Y":
                return ""
            io.write("\r\nSave the server's public key? [Y/N]:")
            if (io.readline() or "").strip().upper() == "Y":
                self.running.append(f"ssh client peer {ip}")
            io.write("\r\nEnter password:")
        else:
            io.write(f"Connected to {ip} ...\r\n\r\nLogin authentication\r\n\r\nUsername:")
            user = (io.readline() or "").strip()
            io.write("\r\nPassword:")
        pw = (io.readline() or "").strip()
        if dev.users.get(user) != pw:
            return "Error: Failed to verify the password." if ssh else "Error: Username or password error."
        if dev.login_prompts(io, user):
            dev.run_cli(io, user)
        return "\r\nInfo: The connection was closed by the remote host."

    def _commit(self, lines, io):
        return self._apply(lines, io)

    def _apply(self, lines, io):
        """True when the session should drop (management address moved)."""
        moved = False
        with self.lock:
            for l in lines:
                self.running.append(l)
                m = re.match(r"sysname (\S+)", l)
                if m:
                    self.sysname = m.group(1)
                m = re.match(r"local-user (\S+) password irreversible-cipher (\S+)", l)
                if m:
                    self.users[m.group(1)] = m.group(2)
                if re.match(r"ip address ", l) and self.network is None:
                    moved = True
                    if self.home is not None:      # reachable at the new address from the router
                        self.home[l.split()[2]] = self
        return moved

    def login_prompts(self, io, user):
        if user in self.force:
            io.write("\r\nWarning: The password needs to be changed. Change now? [Y/N]:")
            if (io.readline() or "").strip().upper() != "Y":
                io.write("\r\nError: The password must be changed at first login.\r\n")
                return False
            io.write("\r\nPlease enter old password:")
            if (io.readline() or "").strip() != self.users[user]:
                io.write("\r\nError: The old password is wrong.\r\n")
                return False
            io.write("\r\nPlease enter new password:")
            a = (io.readline() or "").strip()
            io.write("\r\nPlease confirm new password:")
            b = (io.readline() or "").strip()
            if a != b or len(a) < 8:
                io.write("\r\nError: The passwords do not match.\r\n")
                return False
            self.users[user] = a
            self.force.discard(user)
            io.write("\r\nInfo: The password has been changed successfully.\r\n")
        return True


class ChanIO:
    def __init__(self, chan, echo=True):
        self.chan, self.echo, self.buf = chan, echo, b""

    def write(self, s):
        try:
            self.chan.sendall(s.encode()) if hasattr(self.chan, "sendall") else self.chan.send(s.encode())
        except Exception:
            pass

    def readline(self):
        while b"\r" not in self.buf and b"\n" not in self.buf:
            try:
                d = self.chan.recv(1024)
            except Exception:
                return None
            if not d:
                return None
            d = re.sub(rb"\xff[\xfb-\xfe].|\xff.", b"", d).replace(b"\x00", b"")
            if self.echo:
                self.write(d.decode(errors="replace").replace("\r", "").replace("\n", ""))
            self.buf += d
        i = min(x for x in (self.buf.find(b"\r"), self.buf.find(b"\n")) if x >= 0)
        line, self.buf = self.buf[:i], self.buf[i + 1:].lstrip(b"\r\n")
        return line.decode(errors="replace")


class _Server(paramiko.ServerInterface):
    def __init__(self, dev):
        self.dev, self.user = dev, None

    def check_auth_password(self, username, password):
        if self.dev.users.get(username) == password:
            self.user = username
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        return "password"

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_shell_request(self, channel):
        return True

    def check_channel_pty_request(self, *a):
        return True


HOST_KEY = paramiko.RSAKey.generate(2048)


def serve_ssh(dev, port=0, host="127.0.0.1"):
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((host, port))
    s.listen(8)

    def one(c):
        t = paramiko.Transport(c)
        t.local_version = "SSH-2.0-HUAWEI-1.5"
        t.add_server_key(HOST_KEY)
        srv = _Server(dev)
        try:
            t.start_server(server=srv)
            chan = t.accept(20)
            if chan is None:
                return
            io = ChanIO(chan)
            if dev.login_prompts(io, srv.user):
                dev.run_cli(io, srv.user)
            chan.close()
        except Exception:
            pass
        finally:
            t.close()

    def loop():
        while True:
            try:
                c, _ = s.accept()
            except OSError:
                return
            threading.Thread(target=one, args=(c,), daemon=True).start()
    threading.Thread(target=loop, daemon=True).start()
    return s, s.getsockname()[1]


def serve_telnet(dev, port=0, host="127.0.0.1"):
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((host, port))
    s.listen(8)

    def one(c):
        io = ChanIO(c)
        try:
            c.sendall(bytes([255, 251, 1, 255, 251, 3]))
            io.write("\r\nWarning: Telnet is not a secure protocol, and it is recommended to use Stelnet.\r\n\r\nLogin authentication\r\n\r\n\r\nUsername:")
            user = (io.readline() or "").strip()
            io.echo = False
            io.write("\r\nPassword:")
            pw = (io.readline() or "").strip()
            io.echo = True
            if dev.users.get(user) != pw:
                io.write("\r\nError: Username or password error.\r\n")
                c.close()
                return
            if dev.login_prompts(io, user):
                dev.run_cli(io, user)
        except Exception:
            pass
        finally:
            c.close()

    def loop():
        while True:
            try:
                c, _ = s.accept()
            except OSError:
                return
            threading.Thread(target=one, args=(c,), daemon=True).start()
    threading.Thread(target=loop, daemon=True).start()
    return s, s.getsockname()[1]


if __name__ == "__main__":
    import sys
    import time
    d = Device(sys.argv[1] if len(sys.argv) > 1 else "vrp8")
    _, p1 = serve_ssh(d, 2222, "0.0.0.0")
    _, p2 = serve_telnet(d, 2323)
    print(f"fake {d.platform} device: ssh 127.0.0.1:{p1}  telnet 127.0.0.1:{p2}  login root / Changeme_123")
    while True:
        time.sleep(60)
