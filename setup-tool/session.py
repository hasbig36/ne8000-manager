"""Talk to a Huawei VRP device over SSH, Telnet or a console cable, with no PuTTY.

One small expect engine drives all three, so the login handling is the same everywhere:
username / password, the factory "change the password now?" prompt, the console's
"set a login password" prompt on new S switches, and Y/N confirmations.

Every failure is raised as SessionError with a plain-language reason and a hint, so the
person at the laptop knows what to fix (cable, laptop IP, SSH off, wrong password, ...).
"""
import errno
import ipaddress
import re
import socket
import time

PROMPT_RE = re.compile(r"(?:^|[\r\n])\s*(<[^<>\r\n]{1,80}>|\[[~*]?[^\[\]\r\n]{1,80}\])\s*$")
CONFIRM_RE = re.compile(r"(\[Y/N\]|\[y/n\]|\(y/n\)|\[Y\(yes\)/N\(no\)/C\(cancel\)\])\s*:?\s*$", re.I)
ANSI_RE = re.compile(r" ?\x1b\[\d+D|\x1b\[[0-9;?]*[A-Za-z]")
NOROUTE = {errno.ENETUNREACH, errno.EHOSTUNREACH, 10051, 10065}     # 100xx: Windows WSAE*


class SessionError(Exception):
    def __init__(self, reason, hint="", stage=""):
        super().__init__(reason)
        self.reason, self.hint, self.stage = reason, hint, stage


class NeedNewPassword(SessionError):
    """The device will not go further until its factory password is changed."""


# ---- transports ------------------------------------------------------------------
class SSHTransport:
    kind = "ssh"

    def __init__(self, host, port, username, password, timeout=12):
        import paramiko
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())    # a new device has no known key yet
        try:
            self.client.connect(host, port=int(port), username=username, password=password, timeout=timeout,
                                banner_timeout=timeout, auth_timeout=timeout, look_for_keys=False, allow_agent=False)
        except paramiko.AuthenticationException:
            raise SessionError("The device refused the username or password.",
                               "Check the login. Common factory logins are listed below the form. "
                               "Too many wrong tries can lock the user for a few minutes.", "login")
        except paramiko.SSHException as exc:
            text = str(exc)
            if "banner" in text.lower():
                raise SessionError(f"Port {port} is open but did not answer like an SSH server ({text}).",
                                   "The device may still be booting, or this port is not SSH. Wait a minute and try again.", "ssh")
            if "incompatible" in text.lower() or "no acceptable" in text.lower() or "kex" in text.lower():
                raise SessionError(f"The SSH settings of the device and this tool do not match ({text}).",
                                   "Very old software only offers weak SSH ciphers. Try Telnet or the console cable.", "ssh")
            raise SessionError(f"SSH failed: {text}", "", "ssh")
        except OSError as exc:
            raise network_error(exc, host, port)
        key = self.client.get_transport().get_remote_server_key()
        self.fingerprint = f"{key.get_name()} {key.fingerprint}" if hasattr(key, "fingerprint") else key.get_name()
        self.chan = self.client.invoke_shell(width=250, height=1000)
        self.chan.settimeout(0.2)
        self.logged_in = True          # SSH did the username/password itself

    def send(self, text):
        self.chan.send(text)

    def recv(self):
        try:
            data = self.chan.recv(65535)
        except socket.timeout:
            return ""
        if not data and self.chan.closed:
            raise EOFError
        return data.decode("utf-8", "replace")

    def close(self):
        try:
            self.client.close()
        except Exception:
            pass


IAC, DONT, DO, WONT, WILL, SB, SE = 255, 254, 253, 252, 251, 250, 240
ECHO, SGA = 1, 3


class TelnetTransport:
    """Minimal Telnet (telnetlib is gone from Python 3.13): agree to echo and go-ahead, refuse the rest."""
    kind = "telnet"

    def __init__(self, host, port, timeout=12):
        try:
            self.sock = socket.create_connection((host, int(port)), timeout=timeout)
        except OSError as exc:
            raise network_error(exc, host, port, telnet=True)
        self.sock.settimeout(0.2)
        self.logged_in = False
        self.fingerprint = ""

    def send(self, text):
        self.sock.sendall(text.replace("\n", "\r\n").encode("utf-8"))

    def recv(self):
        try:
            data = self.sock.recv(65535)
        except socket.timeout:
            return ""
        if not data:
            raise EOFError
        out, i = bytearray(), 0
        while i < len(data):
            b = data[i]
            if b == IAC and i + 1 < len(data):
                cmd = data[i + 1]
                if cmd in (DO, DONT, WILL, WONT) and i + 2 < len(data):
                    opt = data[i + 2]
                    if cmd == DO:
                        self.sock.sendall(bytes([IAC, WILL if opt == SGA else WONT, opt]))
                    elif cmd == WILL:
                        self.sock.sendall(bytes([IAC, DO if opt in (ECHO, SGA) else DONT, opt]))
                    i += 3
                    continue
                if cmd == SB:
                    end = data.find(bytes([IAC, SE]), i)
                    i = len(data) if end < 0 else end + 2
                    continue
                if cmd == IAC:
                    out.append(IAC)
                i += 2
                continue
            out.append(b)
            i += 1
        return out.decode("utf-8", "replace")

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


class SerialTransport:
    kind = "console"

    def __init__(self, port, baud=9600):
        try:
            import serial
            self.ser = serial.Serial(port, int(baud), timeout=0.2)
        except Exception as exc:
            raise SessionError(f"Could not open the console port {port}: {exc}",
                               "Check the USB console cable is plugged in, its driver is installed, and no other "
                               "program (PuTTY, screen) has the port open.", "console")
        self.logged_in = False
        self.fingerprint = ""
        self.send("\r")         # wake the console up

    def send(self, text):
        self.ser.write(text.replace("\n", "\r").encode("utf-8"))

    def recv(self):
        data = self.ser.read(4096)
        return data.decode("utf-8", "replace") if data else ""

    def close(self):
        try:
            self.ser.close()
        except Exception:
            pass


def list_serial_ports():
    try:
        from serial.tools import list_ports
    except ImportError:
        return []
    return [{"device": p.device, "description": p.description or ""} for p in list_ports.comports()]


def network_error(exc, host, port, telnet=False):
    proto = "Telnet" if telnet else "SSH"
    if isinstance(exc, socket.gaierror):
        return SessionError(f"'{host}' is not a valid address or name.", "Type the device's IP address, e.g. 192.168.1.253.", "address")
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return SessionError(f"No answer from {host} on port {port}.",
                            "Check the cable and link light, that the device has finished booting, and that the laptop "
                            "has an address in the same network as the device (see 'This laptop' above).", "network")
    if isinstance(exc, ConnectionRefusedError):
        return SessionError(f"{host} is reachable but refused {proto} on port {port}.",
                            f"{proto} is not switched on there. Try " + ("SSH" if telnet else "Telnet (port 23)") +
                            ", or the console cable, to do the first setup.", "port")
    if getattr(exc, "errno", None) in NOROUTE or getattr(exc, "winerror", None) in NOROUTE:
        return SessionError(f"This laptop has no route to {host}.",
                            "Give the laptop's network card a fixed address in the device's network "
                            "(for 192.168.1.253 use e.g. 192.168.1.10 / 255.255.255.0) and plug into the management port.", "network")
    return SessionError(f"Could not reach {host}:{port}: {exc}", "", "network")


# ---- the expect engine -------------------------------------------------------------
class Shell:
    def __init__(self, transport, log=None):
        self.t = transport
        self.buf = ""
        self.log = log if log is not None else []
        self.prompt = ""

    def _read(self, wait):
        chunk = self.t.recv()
        if chunk:
            chunk = ANSI_RE.sub("", chunk).replace("\r\n", "\n")
            self.buf += chunk
        elif wait:
            time.sleep(0.05)
        return chunk

    def expect(self, patterns, timeout=20):
        """Read until one of the regexes matches the end of the buffer. Returns (index, text)."""
        end = time.time() + timeout
        while True:
            try:
                got = self._read(True)
            except EOFError:
                raise SessionError("The device closed the session.", "It may have rebooted, or the session limit is reached.", "session")
            if not got:
                for i, p in enumerate(patterns):
                    if p.search(self.buf):
                        text, self.buf = self.buf, ""
                        return i, text
            if time.time() > end:
                tail = self.buf[-300:].strip()
                raise SessionError("The device stopped answering.", f"Last text from it: {tail!r}" if tail else
                                   "Nothing came back. On a console cable check the speed (usually 9600).", "session")

    def login(self, username, password, new_password=""):
        pats = [  # checked in this order once the device goes quiet
            ("fail", re.compile(r"(?i)(authentication fail|username or password|password invalid|login failed|access denied)")),
            ("locked", re.compile(r"(?i)(user|account|ip|address).{0,40}\b(is|been) locked")),
            ("prompt", PROMPT_RE),
            ("user", re.compile(r"(?i)(user ?name|login)\s*:\s*$")),
            ("old", re.compile(r"(?i)old password\s*:?\s*$")),
            ("confirm", re.compile(r"(?i)(confirm|re-?enter|retype|again).{0,30}password\s*:?\s*$")),
            ("new", re.compile(r"(?i)(new|enter) password\s*(\([^)]*\))?\s*:?\s*$")),
            ("password", re.compile(r"(?i)password\s*:\s*$")),
            ("change", re.compile(r"(?i)(change now|change the password|modify the password).{0,40}(\[y/n\])\s*:?\s*$")),
            ("yn", CONFIRM_RE),
            ("enter", re.compile(r"(?i)(press enter|press return|press any key)[^\n]*$")),
        ]
        names = [n for n, _ in pats]
        rx = [r for _, r in pats]
        pw_sent, changed = 0, False
        for _ in range(16):
            try:
                i, text = self.expect(rx, timeout=25)
            except SessionError as exc:
                if exc.reason == "The device closed the session." and not new_password and pw_sent:
                    raise NeedNewPassword("The device wants its factory password changed before anyone can log in.",
                                          "Enter a new password for this user below and check again.", "login")
                raise
            self.log.append(text)
            k = names[i]
            if k == "fail":
                raise SessionError("The device refused the username or password.",
                                   "Check the login. Common factory logins are listed below the form. "
                                   "Too many wrong tries can lock the user for a few minutes.", "login")
            if k == "locked":
                raise SessionError("The device says this user or address is locked after too many wrong passwords.",
                                   "Wait 5 to 10 minutes (or use the console cable) and try again with the right password.", "login")
            if k == "prompt":
                self.prompt = PROMPT_RE.search(text).group(1)
                return changed
            if k == "user":
                self.t.send(username + "\n")
            elif k == "old":
                self.t.send(password + "\n")
            elif k in ("confirm", "new"):
                first_console = "login password" in text.lower()
                if k == "new" and not changed and not first_console and "enter password" in text.lower() and not new_password:
                    pw_sent += 1          # some consoles say "Enter password:" for the normal login
                    if pw_sent > 2:
                        raise SessionError("The device refused the password.", "Check the login.", "login")
                    self.t.send(password + "\n")
                    continue
                if not new_password:
                    raise NeedNewPassword("The device asks for a new password before it can be used.",
                                          "Enter the new password for this user below and check again.", "login")
                self.t.send(new_password + "\n")
                changed = True
            elif k == "password":
                pw_sent += 1
                if pw_sent > 2:
                    raise SessionError("The device refused the username or password.",
                                       "Check the login. Common factory logins are listed below the form.", "login")
                self.t.send(password + "\n")
            elif k == "change":
                if not new_password:
                    raise NeedNewPassword("The device wants its factory password changed before it can be used.",
                                          "Enter a new password for this user below and check again.", "login")
                self.t.send("Y\n")
            elif k == "yn":
                self.t.send("N\n")
            elif k == "enter":
                self.t.send("\n")
        raise SessionError("Could not get to the device's command prompt.", "Try the console cable.", "login")

    def cmd(self, line, timeout=60, answer="Y"):
        """Run one command. Y/N questions get `answer`; a key-size question gets the default."""
        self.t.send(line + "\n")
        pats = [PROMPT_RE, CONFIRM_RE, re.compile(r"(?i)(modulus|bits in the modulus|key length)[^\n]*:\s*$"),
                re.compile(r"(?i)---- more ----\s*$")]
        out = ""
        for _ in range(20):
            i, text = self.expect(pats, timeout)
            out += text
            if i == 0:
                self.prompt = PROMPT_RE.search(text).group(1)
                break
            if i == 1:
                self.t.send(answer + "\n")
            elif i == 2:
                self.t.send("\n")
            else:
                self.t.send(" ")
        self.log.append(out)
        body = out
        if body.lstrip().startswith(line.strip()):
            body = body.lstrip()[len(line.strip()):]
        return PROMPT_RE.sub("", body).strip("\n")

    def close(self):
        try:
            self.t.send("quit\n")
        except Exception:
            pass
        self.t.close()


# ---- opening a session ---------------------------------------------------------------
def tcp_check(host, port, timeout=3):
    """None when the port answers, else a SessionError saying why not."""
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return None
    except OSError as exc:
        return network_error(exc, host, port, telnet=int(port) == 23)


def valid_host(host):
    host = (host or "").strip()
    if not host:
        raise SessionError("Enter the device's address.", "", "address")
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        if re.match(r"^[A-Za-z0-9.\-]{1,253}$", host):
            return host
        raise SessionError(f"'{host}' is not a valid address.", "Type the device's IP address, e.g. 192.168.1.253.", "address")


def open_session(target, log=None):
    """target: {method: ssh|telnet|console, host, port, serial_port, baud, username, password, new_password}"""
    m = target.get("method") or "ssh"
    user, pw, newpw = target.get("username") or "", target.get("password") or "", target.get("new_password") or ""
    if m == "console":
        t = SerialTransport(target.get("serial_port") or "", target.get("baud") or 9600)
    else:
        host = valid_host(target.get("host"))
        port = int(target.get("port") or (22 if m == "ssh" else 23))
        if m == "ssh":
            t = SSHTransport(host, port, user, pw)
        else:
            t = TelnetTransport(host, port)
    sh = Shell(t, log)
    try:
        changed = sh.login(user, pw, newpw)
        sh.password_changed = changed
        sh.cmd("screen-length 0 temporary", timeout=15)
    except Exception:
        t.close()
        raise
    return sh


def identify(sh):
    ver = sh.cmd("display version", timeout=30)
    m = re.search(r"VRP \(R\) software, Version (\d+)", ver)
    if not m:
        raise SessionError("Logged in, but this does not look like a Huawei VRP device.",
                           "'display version' did not show a VRP version.", "identify")
    platform = "vrp8" if int(m.group(1)) >= 8 else "vrp5"
    vm = re.search(r"VRP \(R\) software, Version (\S+(?: \([^)]*\))?)", ver)
    model = ""
    for l in ver.splitlines():
        mm = re.match(r"(?i)^\s*(huawei\s+\S+.*?)\s+uptime is", l)
        if mm:
            model = mm.group(1)
            break
    uptime = re.search(r"uptime is ([^\n]+)", ver)
    esn = ""
    try:
        e = sh.cmd("display esn", timeout=15)
        em = re.search(r"(?i)ESN[^:]*:\s*(\S+)", e)
        esn = em.group(1) if em else ""
    except SessionError:
        pass
    name = sh.prompt.strip("<>[]~*")
    return {"platform": platform, "version": vm.group(1) if vm else "", "model": model, "sysname": name,
            "uptime": uptime.group(1).strip() if uptime else "", "esn": esn}
