"""Find Huawei devices from the laptop: its own networks, plus the usual factory addresses.

Each address is probed on SSH (22) and Telnet (23). An SSH banner such as
"SSH-2.0-HUAWEI-1.5" names the vendor; a Telnet greeting often does too.
"""
import concurrent.futures
import ipaddress
import socket

from adopt import ssh_banner, vendor_of, guess_platform

MAX_HOSTS = 1024
# Addresses Huawei devices commonly come with from the factory (model and software dependent).
FACTORY = [("192.168.1.253", "S-series switch management port (MEth) factory address"),
           ("192.168.1.1", "AR router / web-managed switch factory address"),
           ("192.168.0.1", "factory address on some models"),
           ("169.254.1.1", "link-local factory address on some models")]


def local_networks():
    """The laptop's IPv4 networks, skipping loopback and link-local auto addresses."""
    try:
        import ifaddr
        adapters = ifaddr.get_adapters()
    except Exception:
        adapters = []
    nets = []
    for a in adapters:
        for ip in a.ips:
            if not isinstance(ip.ip, str):
                continue
            try:
                itf = ipaddress.ip_interface(f"{ip.ip}/{ip.network_prefix}")
            except ValueError:
                continue
            if itf.ip.is_loopback:
                continue
            nets.append({"adapter": a.nice_name, "address": str(itf.ip), "network": str(itf.network),
                         "prefix": itf.network.prefixlen, "link_local": itf.ip.is_link_local})
    if not nets:                         # fallback: the address used for the default route
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("10.255.255.255", 1))
            ip = s.getsockname()[0]
            s.close()
            nets.append({"adapter": "default", "address": ip, "network": str(ipaddress.ip_network(f"{ip}/24", strict=False)),
                         "prefix": 24, "link_local": False})
        except OSError:
            pass
    return nets


def scan_targets(nets, extra=""):
    """Addresses to probe: each laptop network (a /24 around the laptop when the network is bigger),
    the factory addresses, and any subnet the user typed."""
    hosts, notes = [], []
    for n in nets:
        if n["link_local"]:
            continue
        net = ipaddress.ip_network(n["network"])
        if net.num_addresses > 256:
            net = ipaddress.ip_network(f"{n['address']}/24", strict=False)
            notes.append(f"{n['network']} is large, so only {net} (around this laptop) is scanned. Type a subnet to scan more.")
        hosts += [str(h) for h in net.hosts() if str(h) != n["address"]]
    for e in [x for x in (extra or "").replace(",", " ").split() if x]:
        try:
            net = ipaddress.ip_network(e, strict=False)
        except ValueError:
            raise ValueError(f"'{e}' is not a subnet. Use e.g. 10.10.0.0/24.")
        if net.version != 4:
            raise ValueError("Only IPv4 can be scanned.")
        if net.num_addresses > MAX_HOSTS:
            raise ValueError(f"{e} has {net.num_addresses} addresses; scan at most {MAX_HOSTS} (a /22) at a time.")
        hosts += [str(h) for h in (net.hosts() if net.num_addresses > 2 else net)]
    hosts += [a for a, _ in FACTORY]
    seen, out = set(), []
    for h in hosts:
        if h not in seen:
            seen.add(h)
            out.append(h)
    return out, notes


def telnet_banner(host, port=23, timeout=1.5):
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.settimeout(timeout)
            data = b""
            try:
                data = s.recv(400)
                if len(data) < 60:
                    s.settimeout(0.8)
                    data += s.recv(400)
            except socket.timeout:
                pass
            text = bytes(b for b in data if b < 128 and (b >= 32 or b in (10, 13))).decode("ascii", "replace")
            return " ".join(text.split())[:160] or "(open)"
    except OSError:
        return None


def probe(host, timeout=1.2):
    ssh = ssh_banner(host, 22, timeout)
    tel = telnet_banner(host, 23, timeout) if ssh is None or "huawei" not in ssh.lower() else None
    if ssh is None and tel is None:
        return None
    vendor = vendor_of(ssh) or vendor_of(tel)
    if not vendor and tel and ("user access verification" in tel.lower() or "warning: telnet is not a secure" in tel.lower()):
        vendor = "Huawei?"          # the usual Huawei Telnet greeting, without the name
    factory = dict(FACTORY).get(host, "")
    return {"host": host, "ssh": ssh is not None, "telnet": tel is not None, "banner": (ssh or tel or "")[:160],
            "vendor": vendor or "unknown", "huawei": vendor.startswith("Huawei") if vendor else False,
            "platform": guess_platform(ssh or "") or guess_platform(tel or ""), "factory": factory,
            "method": "ssh" if ssh is not None else "telnet"}


def scan(extra="", progress=None, probe_fn=probe):
    nets = local_networks()
    hosts, notes = scan_targets(nets, extra)
    found, done = [], 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=128) as ex:
        futs = {ex.submit(probe_fn, h): h for h in hosts}
        for f in concurrent.futures.as_completed(futs):
            done += 1
            r = f.result()
            if r:
                found.append(r)
            if progress and done % 16 == 0:
                progress(done, len(hosts))
    key = lambda r: (not r["huawei"], tuple(int(x) for x in r["host"].split(".")))
    return {"networks": nets, "scanned": len(hosts), "notes": notes, "devices": sorted(found, key=key)}
