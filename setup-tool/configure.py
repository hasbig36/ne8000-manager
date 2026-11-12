"""Build, check and apply the first configuration, then prove the device is ready.

The command template is NE8000 Manager's (adopt.build), so a device set up from the laptop
looks the same as one adopted from the web app. Apply order:
  1. everything except the new management address; commit (VRP8) and save
  2. log in again with the NEW admin user over SSH, to prove the device is reachable and usable
  3. the new management address last (the session may move to it), save, and find it there
"""
import ipaddress
import re
import time

import adopt
from adopt import AdoptError, PHASE2, split_phases, new_address
from session import SessionError, open_session, identify, tcp_check

TZ_RE = re.compile(r"^([+-])(\d{1,2}):?(\d{2})?$")


def build(fields, platform, method):
    lines = adopt.build(fields, platform)
    g = lambda k: str(fields.get(k) or "").strip()
    extra = []
    tz = g("tz_offset")
    if tz:
        m = TZ_RE.match(tz)
        if not m or int(m.group(2)) > 14:
            raise AdoptError("Time zone: an offset from UTC such as +03:00.")
        name = g("tz_name") or "LOCAL"
        if not re.match(r"^[A-Za-z]{1,10}$", name):
            raise AdoptError("Time zone name: up to 10 letters, e.g. EAT.")
        extra.append(f"clock timezone {name} {'add' if m.group(1) == '+' else 'minus'} {int(m.group(2)):02d}:{m.group(3) or '00'}:00")
    for v in re.split(r"[,\s]+", g("dns")):
        if v:
            try:
                ip = str(ipaddress.IPv4Address(v))
            except ValueError:
                raise AdoptError(f"DNS: '{v}' is not an IPv4 address.")
            if "dns resolve" not in extra:
                extra.append("dns resolve")
            extra.append(f"dns server {ip}")
    if fields.get("create_ssh_key"):        # before "stelnet server enable", which needs a host key
        lines.insert(1, "rsa local-key-pair create")
    cut = lines.index(PHASE2) if PHASE2 in lines else len(lines)
    lines = lines[:cut] + extra + lines[cut:]
    head = [f"# Initial configuration for {platform.upper()}, reached by {method}. Edit freely; lines starting with # are ignored."]
    return head + lines


def _push(sh, lines, log):
    errors = []
    for l in lines:
        out = sh.cmd(l, timeout=120)
        log(f"{sh.prompt} {l.strip()}" + (f"\n{out}" if out.strip() else ""))
        if "Error:" in out:
            errors.append(f"{l.strip()}: {out[out.index('Error:'):].splitlines()[0]}")
    return errors


def _discard(sh, log):
    """Leave system-view without committing (VRP8). 'abort' is not usable on every VRP8,
    so leave with 'return' and answer N to 'commit them before exiting?'."""
    for _ in range(3):
        if sh.prompt.startswith("<"):
            break
        sh.cmd("return", timeout=30, answer="N")
    log("(not committed)")


def _commit_and_save(sh, platform, log):
    if platform == "vrp8":
        out = sh.cmd("commit", timeout=180)
        log(f"{sh.prompt} commit" + (f"\n{out}" if out.strip() else ""))
        if "Error:" in out or "failed" in out.lower():
            raise SessionError("The device refused the commit.", out.strip()[-400:], "commit")
    sh.cmd("return", timeout=30)
    out = sh.cmd("save", timeout=300, answer="Y")
    log(f"{sh.prompt} save\n{out}")
    if not re.search(r"(?i)success", out):
        raise SessionError("The configuration was applied but saving it may have failed.", out.strip()[-400:], "save")


def check_only(target, text, log):
    p1, p2 = split_phases(text)
    sh = open_session(target)
    try:
        info = identify(sh)
        if info["platform"] != "vrp8":
            raise SessionError("S switches (VRP5) apply each line as it is typed, so there is no check-only run.",
                               "Review the lines and press Apply.", "check")
        sh.cmd("system-view", timeout=30)
        errors = _push(sh, p1 + p2, log)
        _discard(sh, log)
        return {"ok": not errors, "errors": errors}
    finally:
        sh.close()


def ssh_port(target):
    return int(target.get("ssh_port") or (target.get("port") if target.get("method") == "ssh" else 0) or 22)


def verify_login(host, username, password, log, tries=3, wait=4, port=22):
    """Log in over SSH with the new admin user. Returns (device info, open session) or raises the last error."""
    last = None
    for n in range(tries):
        try:
            sh = open_session({"method": "ssh", "host": host, "port": port, "username": username, "password": password})
        except SessionError as exc:
            last = exc
            log(f"(SSH login as {username} at {host}: {exc.reason})")
            time.sleep(wait)
            continue
        try:
            info = identify(sh)
            return info, sh
        except Exception:
            sh.close()
            raise
    raise last


def apply(target, fields, text, log):
    """Returns a report dict. `log` is called with each step so the screen can follow along."""
    p1, p2 = split_phases(text)
    admin, admin_pw = str(fields.get("admin_user") or "").strip(), fields.get("admin_password") or ""
    report = {"steps": [], "ready": False, "host": target.get("host") or "", "warnings": []}
    step = lambda name, ok, detail="": (report["steps"].append({"name": name, "ok": ok, "detail": detail}), log(("OK  " if ok else "FAIL ") + name + (f": {detail}" if detail else "")))

    sh = open_session(target)
    try:
        info = identify(sh)
        report["device"] = info
        step("Connected and identified", True, f"{info['model'] or 'Huawei'} VRP {info['version']}")
        sh.cmd("system-view", timeout=30)
        errors = _push(sh, p1, log)
        if errors and info["platform"] == "vrp8":
            _discard(sh, log)
            step("Basic settings", False, "the device rejected some lines, nothing was committed")
            report["errors"] = errors
            return report
        if errors:
            report["warnings"].append("Some lines were rejected (the switch applied the others):\n" + "\n".join(errors))
        _commit_and_save(sh, info["platform"], log)
        step("Basic settings committed and saved", True, f"{len(p1)} lines" + (f", {len(errors)} rejected" if errors else ""))
    finally:
        sh.close()

    host = report["host"] if target.get("method") != "console" else ""
    nh = new_address(p2)
    sp = ssh_port(target)
    if host and admin:
        try:
            v, s2 = verify_login(host, admin, admin_pw, log, port=sp)
            s2.close()
            step(f"New admin '{admin}' can log in over SSH", True, f"{host}, name {v['sysname']}")
        except SessionError as exc:
            step(f"New admin '{admin}' can log in over SSH", False, f"{exc.reason} {exc.hint}".strip())
            report["warnings"].append("Check the admin login before moving the management address.")
            if p2:
                report["pending_phase2"] = True
                return report

    if p2:
        sh = open_session(target) if target.get("method") == "console" else None
        if sh is None:
            try:
                sh = open_session({"method": "ssh", "host": host, "port": sp, "username": admin, "password": admin_pw})
            except SessionError:
                sh = open_session(target)
        moved_ok = False
        try:
            sh.cmd("system-view", timeout=30)
            errors = _push(sh, p2, log)
            if errors and info["platform"] == "vrp8":
                _discard(sh, log)
                step("Management address", False, "; ".join(errors))
                return report
            _commit_and_save(sh, info["platform"], log)
            moved_ok = True
            step("Management address set and saved", True, nh)
        except SessionError as exc:
            log(f"(the session ended while the management address moved: {exc.reason})")
        finally:
            sh.close()
        if nh and admin:
            if tcp_check(nh, sp, timeout=3) is None:
                try:
                    v, s3 = verify_login(nh, admin, admin_pw, log, tries=4, port=sp)
                    if not moved_ok:
                        s3.cmd("save", timeout=300, answer="Y")
                        step("Management address set and saved", True, f"{nh} (saved from the new address)")
                        moved_ok = True
                    s3.close()
                    report["host"] = nh
                    step(f"Reachable at the new address {nh}", True)
                except SessionError as exc:
                    step(f"Reachable at the new address {nh}", False, exc.reason)
            else:
                report["warnings"].append(
                    f"This laptop cannot reach {nh} (normal when it is on another network). " +
                    ("The setting is saved." if moved_ok else
                     "The address is set but NOT saved yet: connect to the device's new network and press 'Finish at the new address'."))
                if not moved_ok:
                    report["pending_save"] = nh
    report["ready"] = all(s["ok"] for s in report["steps"]) and not report.get("pending_save")
    return report


def finish_at(host, username, password, log, port=22):
    v, sh = verify_login(host, username, password, log, tries=2, port=port)
    try:
        out = sh.cmd("save", timeout=300, answer="Y")
        log(f"save\n{out}")
    finally:
        sh.close()
    return {"ready": True, "host": host, "device": v, "steps": [{"name": f"Logged in at {host} and saved", "ok": True, "detail": v["sysname"]}], "warnings": []}
