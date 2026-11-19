"""What the app's own server looks like: machine, CPU, memory, disk, uptime and the app process.

Reads /proc and /sys only (Linux), so it needs no extra packages and works inside Docker, where the
CPU, memory, load and uptime figures are the host's (the container shares its kernel)."""
import os
import platform
import shutil
import socket
import sys
import threading
import time
from collections import deque

STARTED = time.time()
_lock = threading.Lock()
_last_cpu = None  # (time, busy, total) from the previous call


def _read(path, default=""):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    except OSError:
        return default


def _cpu_times():
    line = _read("/proc/stat").splitlines()
    if not line or not line[0].startswith("cpu "):
        return None
    v = [int(x) for x in line[0].split()[1:]]
    idle = v[3] + (v[4] if len(v) > 4 else 0)
    total = sum(v[:8])
    return total - idle, total


def cpu_percent():
    """CPU in use across all cores, in %, since the last call (or over 0.3 s on the first)."""
    global _last_cpu
    with _lock:
        now = _cpu_times()
        if not now:
            return None
        prev = _last_cpu
        if not prev or time.time() - prev[0] > 600:
            time.sleep(0.3)
            prev, now = (0, *now), _cpu_times()
        _last_cpu = (time.time(), *now)
        db, dt = now[0] - prev[1], now[1] - prev[2]
        return round(100.0 * db / dt, 1) if dt > 0 else 0.0


def _meminfo():
    out = {}
    for line in _read("/proc/meminfo").splitlines():
        k, _, v = line.partition(":")
        parts = v.split()
        if parts and parts[0].isdigit():
            out[k] = int(parts[0]) * 1024
    return out


def _cpu_model():
    model, flags = "", ""
    for line in _read("/proc/cpuinfo").splitlines():
        k, _, v = line.partition(":")
        k = k.strip()
        if k in ("model name", "Model", "Hardware") and not model:
            model = v.strip()
        elif k == "flags" and not flags:
            flags = v
    return model, " hypervisor " in f" {flags} "


def _container_memory():
    """The container's memory limit and use (cgroup v2, then v1); limit None when unlimited."""
    for lim, cur in (("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory.current"),
                     ("/sys/fs/cgroup/memory/memory.limit_in_bytes", "/sys/fs/cgroup/memory/memory.usage_in_bytes")):
        lv, cv = _read(lim), _read(cur)
        if cv.isdigit():
            limit = int(lv) if lv.isdigit() and int(lv) < 1 << 60 else None
            return {"limit": limit, "used": int(cv)}
    return None


def _os_name():
    for line in _read("/etc/os-release").splitlines():
        if line.startswith("PRETTY_NAME="):
            return line.split("=", 1)[1].strip('"')
    return platform.system()


def _host_os():
    """The host's distribution as far as the kernel build string tells (Docker hides the host's files)."""
    v = _read("/proc/version")
    for name in ("Ubuntu", "Debian", "Red Hat", "CentOS", "Rocky", "AlmaLinux", "Fedora", "SUSE", "Alpine", "Amazon"):
        if name.lower() in v.lower():
            return name
    return ""


def _platform():
    """Where it runs: the machine maker / product from DMI (e.g. QEMU, VMware, OpenStack, a hardware vendor)."""
    vendor = _read("/sys/class/dmi/id/sys_vendor")
    product = _read("/sys/class/dmi/id/product_name")
    board = _read("/sys/class/dmi/id/board_vendor")
    hyper = _read("/sys/hypervisor/type")
    return {"vendor": vendor or board, "product": product, "hypervisor": hyper}


def _dir_size(path, limit=20000):
    total = n = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
            n += 1
            if n >= limit:
                return total, n, True
    return total, n, False


def _rss():
    for line in _read("/proc/self/status").splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) * 1024
    return None


def report(data_dir):
    model, virtual = _cpu_model()
    cpus = os.cpu_count() or 0
    load = [float(x) for x in _read("/proc/loadavg", "0 0 0").split()[:3]]
    mem = _meminfo()
    mt, ma = mem.get("MemTotal"), mem.get("MemAvailable", mem.get("MemFree"))
    st, sf = mem.get("SwapTotal", 0), mem.get("SwapFree", 0)
    try:
        du = shutil.disk_usage(data_dir)
        disk = {"path": data_dir, "total": du.total, "used": du.used, "free": du.free}
    except OSError:
        disk = None
    size, files, more = _dir_size(data_dir)
    up = _read("/proc/uptime").split()
    plat = _platform()
    out = {
        "time": time.time(),
        "host": {
            "hostname": socket.gethostname(),
            "kernel": platform.release(),
            "arch": platform.machine(),
            "host_os": _host_os(),
            "os": _os_name(),
            "container": os.path.exists("/.dockerenv") or "docker" in _read("/proc/1/cgroup"),
            "virtual": virtual or bool(plat["hypervisor"]),
            **plat,
            "timezone": time.strftime("%Z %z"),
        },
        "cpu": {"model": model, "cores": cpus, "percent": cpu_percent(), "load": load},
        "memory": {"total": mt, "available": ma, "used": (mt - ma) if mt and ma is not None else None,
                   "swap_total": st, "swap_used": st - sf},
        "container_memory": _container_memory(),
        "disk": disk,
        "data": {"size": size, "files": files, "more": more},
        "uptime": float(up[0]) if up else None,
        "app": {"started": STARTED, "uptime": time.time() - STARTED, "rss": _rss(), "threads": threading.active_count(),
                "python": sys.version.split()[0], "pid": os.getpid()},
    }
    out["warnings"] = warnings(out)
    out["history"] = list(HISTORY)
    out["version"] = VERSION
    return out


# one sample a minute for the last 24 h: [time, cpu %, memory %, disk %, load (1 min)]
HISTORY = deque(maxlen=1440)
VERSION = (os.environ.get("NE8000_VERSION") or _read(os.path.join(os.path.dirname(os.path.abspath(__file__)), "VERSION")))[:40]


def sample(data_dir):
    mem = _meminfo()
    mt, ma = mem.get("MemTotal"), mem.get("MemAvailable", mem.get("MemFree"))
    try:
        du = shutil.disk_usage(data_dir)
        dp = round(100 * du.used / (du.used + du.free), 1) if du.used + du.free else None
    except OSError:
        dp = None
    load = _read("/proc/loadavg", "0").split()
    HISTORY.append([round(time.time()), cpu_percent(), round(100 * (mt - ma) / mt, 1) if mt and ma is not None else None, dp,
                    float(load[0]) if load else None])


def start(data_dir):
    def loop():
        while True:
            try:
                sample(data_dir)
            except Exception:  # keep sampling whatever one read did
                pass
            time.sleep(60)
    threading.Thread(target=loop, name="serverinfo", daemon=True).start()


def warnings(r):
    w = []
    c, m, d = r["cpu"], r["memory"], r["disk"]
    if c["cores"] and c["load"][1] > c["cores"]:
        w.append(("critical" if c["load"][1] > 2 * c["cores"] else "warning",
                  f"The server is overloaded: the 5-minute load is {c['load'][1]:.1f} on {c['cores']} CPU core(s)."))
    if m.get("total") and m.get("used") is not None:
        pct = 100 * m["used"] / m["total"]
        if pct >= 90:
            w.append(("critical" if pct >= 95 else "warning", f"Memory is {pct:.0f}% used. The app may slow down or be stopped by the system."))
    if m.get("swap_total") and m["swap_used"] / m["swap_total"] > 0.5:
        w.append(("warning", "More than half of the swap is in use, so the server is short of memory."))
    cm = r.get("container_memory")
    if cm and cm.get("limit") and cm["used"] / cm["limit"] > 0.9:
        w.append(("warning", "The app is close to its container memory limit."))
    if d:
        pct = 100 * d["used"] / (d["used"] + d["free"]) if d["used"] + d["free"] else 0
        if pct >= 80:
            w.append(("critical" if pct >= 92 else "warning",
                      f"The disk holding the app's data is {pct:.0f}% full. Backups, graphs and logs stop being saved when it is full."))
    return [{"severity": s, "text": t} for s, t in w]
