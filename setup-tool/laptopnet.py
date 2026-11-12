"""Put the laptop's wired port on a device's factory network, and put it back.

A new device plugged straight into the laptop sits at a factory address (for example
192.168.1.253) that the laptop cannot reach until its own port has an address in the same
network. Changing that needs administrator rights, so the OS asks for them:
  Windows: netsh, run elevated (a UAC prompt). The port is set to a fixed address and set
           back to automatic (DHCP) afterwards.
  macOS:   ifconfig alias, via the administrator password prompt. The extra address sits
           beside the normal one and is removed afterwards (or at the next restart).
"""
import ipaddress
import re
import subprocess
import sys

CHANGED = {}            # adapter id -> what to undo


class NetError(Exception):
    pass


def _run(args, timeout=60):
    p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def mac_services():
    """BSD device (en5) -> network service name (USB 10/100/1000 LAN)."""
    try:
        _, out = _run(["/usr/sbin/networksetup", "-listnetworkserviceorder"], 15)
    except Exception:
        return {}
    res, last = {}, ""
    for l in out.splitlines():
        m = re.match(r"^\(\d+\)\s+(.+)$", l.strip())
        if m:
            last = m.group(1)
        m = re.search(r"Device:\s*([\w.]+)\)", l)
        if m and last:
            res[m.group(1)] = last
    return res


WIRELESS = re.compile(r"(?i)wi-?fi|wireless|wlan|airport|bluetooth|vpn|virtual|vmware|hyper-v|loopback|tailscale|zerotier|utun|awdl|llw|bridge|docker")


def adapters():
    """Wired-looking adapters, with their current IPv4 addresses."""
    try:
        import ifaddr
        raw = ifaddr.get_adapters()
    except Exception:
        raw = []
    svc = mac_services() if sys.platform == "darwin" else {}
    out = []
    for a in raw:
        name = a.nice_name if sys.platform == "win32" else a.name
        label = svc.get(a.name, a.nice_name) if sys.platform == "darwin" else a.nice_name
        if WIRELESS.search(label) or WIRELESS.search(a.name) or a.name == "lo" or a.name.startswith("lo"):
            continue
        if sys.platform == "darwin" and a.name not in svc:
            continue
        ips = [f"{i.ip}/{i.network_prefix}" for i in a.ips if isinstance(i.ip, str)]
        out.append({"id": name, "label": label, "addresses": ips, "changed": name in CHANGED})
    return out


def laptop_address(factory_net):
    net = ipaddress.ip_network(factory_net, strict=False)
    if net.version != 4 or not 16 <= net.prefixlen <= 30:
        raise NetError("Use an IPv4 network like 192.168.1.0/24.")
    hosts = list(net.hosts())
    pick = hosts[9] if len(hosts) > 20 else hosts[-2]      # .10 on a /24: clear of .1 and .253
    return str(pick), str(net.netmask), net.prefixlen


def _check_adapter(adapter_id):
    if not re.match(r"^[\w .\-()#]{1,80}$", adapter_id or "") or adapter_id not in [a["id"] for a in adapters()]:
        raise NetError("Pick one of the laptop's wired network ports.")
    return adapter_id


def join(adapter_id, factory_net):
    adapter_id = _check_adapter(adapter_id)
    ip, mask, plen = laptop_address(factory_net)
    if sys.platform == "win32":
        # a port on DHCP is switched to a fixed address (and back to DHCP later);
        # a port that already has a fixed address just gets one more (removed later)
        _, dhcp = _run(["powershell", "-NoProfile", "-Command",
                        f"(Get-NetIPInterface -InterfaceAlias '{adapter_id}' -AddressFamily IPv4).Dhcp"], 30)
        on_dhcp = "disabled" not in dhcp.lower()
        verb = f"set address name=\"{adapter_id}\" static {ip} {mask}" if on_dhcp else f"add address name=\"{adapter_id}\" {ip} {mask}"
        ps = ("$p = Start-Process -FilePath netsh -Verb RunAs -Wait -PassThru -WindowStyle Hidden -ArgumentList "
              f"'interface ipv4 {verb}'; exit $p.ExitCode")
        code, out = _run(["powershell", "-NoProfile", "-Command", ps], 120)
        if code != 0:
            raise NetError("Windows did not change the address (the administrator prompt may have been declined). " + out.strip()[-200:])
        CHANGED[adapter_id] = ("win-dhcp" if on_dhcp else "win-add", ip)
    elif sys.platform == "darwin":
        cmd = f"/sbin/ifconfig {adapter_id} alias {ip} netmask {mask}"
        code, out = _run(["/usr/bin/osascript", "-e", f'do shell script "{cmd}" with administrator privileges'], 120)
        if code != 0:
            raise NetError("macOS did not add the address (the password prompt may have been cancelled). " + out.strip()[-200:])
        CHANGED[adapter_id] = ("mac", ip)
    else:
        raise NetError(f"On this system set the address by hand: {ip}/{plen} on {adapter_id}.")
    return {"adapter": adapter_id, "address": f"{ip}/{plen}"}


def restore(adapter_id):
    how = CHANGED.get(adapter_id)
    if not how:
        return False
    kind, ip = how
    if kind.startswith("win"):
        verb = f"set address name=\"{adapter_id}\" dhcp" if kind == "win-dhcp" else f"delete address name=\"{adapter_id}\" {ip}"
        ps = ("$p = Start-Process -FilePath netsh -Verb RunAs -Wait -PassThru -WindowStyle Hidden -ArgumentList "
              f"'interface ipv4 {verb}'; exit $p.ExitCode")
        code, out = _run(["powershell", "-NoProfile", "-Command", ps], 120)
    else:
        code, out = _run(["/usr/bin/osascript", "-e", f'do shell script "/sbin/ifconfig {adapter_id} -alias {ip}" with administrator privileges'], 120)
    if code != 0:
        raise NetError("Could not put the port back: " + out.strip()[-200:])
    CHANGED.pop(adapter_id, None)
    return True


def restore_all():
    for a in list(CHANGED):
        try:
            restore(a)
        except Exception:
            pass
