"""End-to-end tests against the fake device. Run: python -m unittest discover -s setup-tool/tests"""
import os
import socket
import sys
import logging
import unittest

logging.getLogger("paramiko").setLevel(logging.CRITICAL)

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.join(HERE, ".."), os.path.join(HERE, "..", "..")]

import configure                 # noqa: E402
import discover                  # noqa: E402
import session                   # noqa: E402
from fake_device import Device, serve_ssh, serve_telnet   # noqa: E402

FIELDS = {"hostname": "BNG-TEST1", "admin_user": "netadmin", "admin_password": "Str0ng-Pass1", "ntp": "10.10.0.1",
          "snmp_user": "monitor", "snmp_auth_key": "authpass123", "snmp_priv_key": "privpass123", "tz_offset": "+03:00", "tz_name": "EAT",
          "dns": "8.8.8.8", "ssh_only": True}


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Login(unittest.TestCase):
    def setUp(self):
        self.dev = Device("vrp8")
        self.srv, self.port = serve_ssh(self.dev)

    def tearDown(self):
        self.srv.close()

    def target(self, **kw):
        t = {"method": "ssh", "host": "127.0.0.1", "port": self.port, "username": "root", "password": "Changeme_123"}
        t.update(kw)
        return t

    def test_factory_password_must_change(self):
        with self.assertRaises(session.NeedNewPassword):
            session.open_session(self.target())
        sh = session.open_session(self.target(new_password="NewPass@2026"))
        self.assertTrue(sh.password_changed)
        info = session.identify(sh)
        sh.close()
        self.assertEqual(info["platform"], "vrp8")
        self.assertEqual(info["model"], "HUAWEI NE8000 M1C")
        self.assertEqual(info["esn"], "2102351931P0K8000123")
        self.assertEqual(self.dev.users["root"], "NewPass@2026")

    def test_wrong_password(self):
        with self.assertRaises(session.SessionError) as cm:
            session.open_session(self.target(password="nope"))
        self.assertEqual(cm.exception.stage, "login")

    def test_refused_and_timeout_reasons(self):
        e = session.tcp_check("127.0.0.1", free_port())
        self.assertEqual(e.stage, "port")
        self.assertIn("refused", e.reason)
        e = session.tcp_check("192.0.2.123", 22, timeout=1)    # TEST-NET: never answers
        self.assertIn(e.stage, ("network",))

    def test_bad_address(self):
        with self.assertRaises(session.SessionError):
            session.valid_host("10.0.0.300 x")


class Apply(unittest.TestCase):
    def run_apply(self, platform, method):
        dev = Device(platform, user="admin", password="admin@huawei.com")
        s1, sp = serve_ssh(dev, host="0.0.0.0")
        s2, tp = serve_telnet(dev)
        try:
            t = {"method": method, "host": "127.0.0.1", "port": sp if method == "ssh" else tp, "ssh_port": sp,
                 "username": "admin", "password": "admin@huawei.com", "new_password": "First@Login1"}
            f = dict(FIELDS, mgmt_interface="Vlanif10" if platform == "vrp5" else "GigabitEthernet0/0/0",
                     mgmt_ip="127.0.0.2/8", mgmt_vlan="10" if platform == "vrp5" else "", create_ssh_key=method != "ssh")
            text = "\n".join(configure.build(f, platform, method))
            log = []
            r = configure.apply(t, f, text, log.append)
            return dev, r, log
        finally:
            s1.close()
            s2.close()

    def check(self, dev, r, log):
        self.assertTrue(r["ready"], (r, "\n".join(log)))
        self.assertEqual(dev.sysname, "BNG-TEST1")
        self.assertEqual(dev.users.get("netadmin"), "Str0ng-Pass1")
        self.assertIn("clock timezone EAT add 03:00:00", dev.saved)
        self.assertIn("dns server 8.8.8.8", dev.saved)
        self.assertTrue(any(l.startswith("ip address 127.0.0.2") for l in dev.saved), dev.saved)
        self.assertEqual(r["host"], "127.0.0.2")

    def test_vrp8_over_ssh(self):
        self.check(*self.run_apply("vrp8", "ssh"))

    def test_vrp5_over_telnet(self):
        dev, r, log = self.run_apply("vrp5", "telnet")
        self.check(dev, r, log)
        self.assertIn("rsa local-key-pair create", dev.saved)

    def test_check_only_changes_nothing(self):
        dev = Device("vrp8", force_change=False)
        s, p = serve_ssh(dev)
        try:
            t = {"method": "ssh", "host": "127.0.0.1", "port": p, "username": "root", "password": "Changeme_123"}
            text = "\n".join(configure.build(FIELDS, "vrp8", "ssh")) + "\nbogus line"
            r = configure.check_only(t, text, lambda x: None)
            self.assertFalse(r["ok"])
            self.assertEqual(len(r["errors"]), 1)
            self.assertEqual(dev.running, [])
            self.assertEqual(dev.sysname, "HUAWEI")
        finally:
            s.close()

    def test_rejected_line_commits_nothing(self):
        dev = Device("vrp8", force_change=False)
        s, p = serve_ssh(dev)
        try:
            t = {"method": "ssh", "host": "127.0.0.1", "port": p, "username": "root", "password": "Changeme_123"}
            text = "sysname X\nbogus line"
            r = configure.apply(t, FIELDS, text, lambda x: None)
            self.assertFalse(r["ready"])
            self.assertEqual(dev.running, [])
        finally:
            s.close()


class LaptopNet(unittest.TestCase):
    def test_address_choice(self):
        import laptopnet
        self.assertEqual(laptopnet.laptop_address("192.168.1.0/24")[:2], ("192.168.1.10", "255.255.255.0"))
        self.assertEqual(laptopnet.laptop_address("192.168.0.1/24")[0], "192.168.0.10")
        with self.assertRaises(laptopnet.NetError):
            laptopnet.laptop_address("10.0.0.0/8")

    def test_mac_service_names(self):
        import laptopnet
        out = ("An asterisk (*) denotes that a network service is disabled.\n(1) USB 10/100/1000 LAN\n"
               "(Hardware Port: USB 10/100/1000 LAN, Device: en5)\n\n(2) Wi-Fi\n(Hardware Port: Wi-Fi, Device: en0)\n")
        orig = laptopnet._run
        laptopnet._run = lambda args, timeout=60: (0, out)
        try:
            self.assertEqual(laptopnet.mac_services(), {"en5": "USB 10/100/1000 LAN", "en0": "Wi-Fi"})
        finally:
            laptopnet._run = orig

    def test_rejects_unknown_adapter(self):
        import laptopnet
        with self.assertRaises(laptopnet.NetError):
            laptopnet.join('eth0" & calc', "192.168.1.0/24")


class Discover(unittest.TestCase):
    def test_targets(self):
        hosts, notes = discover.scan_targets([{"network": "10.0.0.0/16", "address": "10.0.5.9", "link_local": False}], "172.16.0.0/30")
        self.assertIn("10.0.5.1", hosts)
        self.assertNotIn("10.0.5.9", hosts)
        self.assertIn("172.16.0.1", hosts)
        self.assertIn("192.168.1.253", hosts)
        self.assertTrue(notes)
        with self.assertRaises(ValueError):
            discover.scan_targets([], "10.0.0.0/8")

    def test_probe_sees_huawei_banner(self):
        dev = Device("vrp8")
        s, p = serve_ssh(dev)
        try:
            from adopt import ssh_banner, vendor_of
            self.assertEqual(vendor_of(ssh_banner("127.0.0.1", p)), "Huawei")
        finally:
            s.close()


if __name__ == "__main__":
    unittest.main()
