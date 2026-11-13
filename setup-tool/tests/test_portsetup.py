"""Setting up a device through a router port: a fake NE8000 with a fake new switch behind it.
Run: python -m unittest discover -s setup-tool/tests"""
import logging
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..", "..")
sys.path[:0] = [HERE, os.path.join(HERE, ".."), ROOT]
logging.getLogger("paramiko").setLevel(logging.CRITICAL)
logging.getLogger("netmiko").setLevel(logging.CRITICAL)

import portsetup                             # noqa: E402
from device import RealRouter                 # noqa: E402
from fake_device import Device, serve_ssh     # noqa: E402

FIELDS = {"hostname": "SW-PORT-TEST", "admin_user": "netadmin", "admin_password": "Str0ng-Pass1", "ssh_only": True,
          "mgmt_interface": "Vlanif10", "mgmt_vlan": "10", "mgmt_ip": "10.10.5.2/30", "gateway": "10.10.5.1"}


class StubRouter:
    """Enough of a Router for plan()."""
    def __init__(self, rows, outputs):
        self.rows, self.outputs = rows, outputs

    def interfaces(self):
        return self.rows

    def run(self, cmd, read_timeout=60):
        for k, v in self.outputs.items():
            if cmd.startswith(k):
                return v
        return ""


ROWS = [{"name": "GigabitEthernet0/1/5", "type": "ethernet", "ip": "unassigned", "phy": "*down", "l2": False, "description": ""},
        {"name": "GigabitEthernet0/1/6", "type": "ethernet", "ip": "10.0.0.1", "phy": "up", "l2": False, "description": "Customer"},
        {"name": "GigabitEthernet0/1/6.100", "type": "sub-interface", "ip": "", "phy": "up", "l2": False}]
DEFAULT_ONLY = "Route Flags: R - relay\n Destination/Mask    Proto   Pre  Cost        Flags NextHop         Interface\n        0.0.0.0/0   Static  60   0             RD  102.0.0.1       GigabitEthernet0/1/0\n"


class Plan(unittest.TestCase):
    def test_free_port(self):
        r = StubRouter(ROWS, {"display ip routing-table": DEFAULT_ONLY})
        p = portsetup.plan(r, "GigabitEthernet0/1/5", "192.168.1.0/24", "10.10.5.1/30")
        self.assertTrue(p["ok"], p["blockers"])
        self.assertIn(" undo shutdown", p["lines"])
        self.assertIn(" ip address 10.10.5.1 255.255.255.252", p["lines"])
        self.assertIn(" ip address 192.168.1.10 255.255.255.0 sub", p["lines"])
        self.assertIn("ssh client first-time enable", p["lines"])
        self.assertIn(" undo ip address 192.168.1.10 255.255.255.0 sub", p["undo"])
        self.assertEqual(p["candidates"][0], "192.168.1.253")

    def test_busy_port_and_used_network(self):
        used = DEFAULT_ONLY + "   192.168.1.0/26  Static  60   0             RD  102.0.0.9       GigabitEthernet0/1/2\n"
        r = StubRouter(ROWS, {"display ip routing-table": used})
        p = portsetup.plan(r, "GigabitEthernet0/1/6", "192.168.1.0/24")
        self.assertFalse(p["ok"])
        text = " ".join(p["blockers"])
        self.assertIn("sub-interfaces", text)
        self.assertIn("10.0.0.1", text)
        self.assertIn("192.168.1.0/26", text)

    def test_rejects_bad_input(self):
        r = StubRouter(ROWS, {})
        for port, net, addr in (("Gi0/1/5; reboot", "192.168.1.0/24", ""), ("GigabitEthernet0/1/5", "10.0.0.0/8", ""),
                                ("GigabitEthernet0/1/5", "192.168.1.0/24", "192.168.1.5/24")):
            with self.assertRaises(portsetup.PortSetupError):
                portsetup.plan(r, port, net, addr)


class ThroughRouter(unittest.TestCase):
    def setUp(self):
        self.rtr = Device("vrp8", user="noc", password="Router@123", force_change=False)
        self.rtr.sysname = "BNG-TEST"
        self.sw = Device("vrp5", user="admin", password="admin@huawei.com")
        self.rtr.network = {"192.168.1.253": self.sw}
        self.sw.home = self.rtr.network
        self.srv, port = serve_ssh(self.rtr)
        self.r = RealRouter("127.0.0.1", "noc", "Router@123", port, poll=False)

    def tearDown(self):
        self.r.close()
        self.srv.close()

    def open_port(self):
        lines = ["interface GigabitEthernet0/1/5", " undo shutdown", " ip address 10.10.5.1 255.255.255.252",
                 " ip address 192.168.1.10 255.255.255.0 sub", "quit", "ssh client first-time enable"]
        out = portsetup.locked(self.r, lambda conn: self.r._configure(lines, True))
        self.assertNotIn("Error:", out)
        self.assertIn("ssh client first-time enable", self.rtr.running)

    def test_needs_first_time_enable(self):
        t = {"method": "ssh", "host": "192.168.1.253", "username": "admin", "password": "admin@huawei.com", "new_password": "First@Login1"}
        with self.assertRaises(portsetup.devsession.SessionError) as cm:
            portsetup.locked(self.r, lambda conn: portsetup.ViaRouter(conn, t).first())
        self.assertIn("public key", cm.exception.reason)
        self.assertEqual(self.r.run("display version").count("VRP"), 1)       # router line still usable

    def test_full_setup_through_port(self):
        self.open_port()
        plan = {"port": "GigabitEthernet0/1/5", "candidates": ["192.168.1.253", "192.168.1.1"], "temp": "192.168.1.10/24"}
        found = portsetup.locked(self.r, lambda conn: portsetup.find(self.r, conn, plan))
        self.assertIn("192.168.1.253", found)
        t = {"method": "ssh", "host": "192.168.1.253", "username": "admin", "password": "admin@huawei.com"}
        with self.assertRaises(portsetup.devsession.NeedNewPassword):
            portsetup.locked(self.r, lambda conn: portsetup.ViaRouter(conn, t).first())
        t["new_password"] = "First@Login1"
        text = "\n".join(portsetup.devconfigure.build(FIELDS, "vrp5", "router port"))
        log = []
        res = portsetup.locked(self.r, lambda conn: portsetup.devconfigure.apply(t, FIELDS, text, log.append, reach=portsetup.ViaRouter(conn, t)))
        self.assertTrue(res["ready"], (res, "\n".join(log)))
        self.assertEqual(res["host"], "10.10.5.2")
        self.assertEqual(self.sw.sysname, "SW-PORT-TEST")
        self.assertIn("ip address 10.10.5.2 255.255.255.252", self.sw.saved)
        self.assertNotIn("ssh client peer 192.168.1.253", self.rtr.running)      # never stored the key on the router
        self.assertIn("VRP", self.r.run("display version"))                        # router line back to normal
        out = portsetup.locked(self.r, lambda conn: self.r._configure(["interface GigabitEthernet0/1/5",
                                                                       " undo ip address 192.168.1.10 255.255.255.0 sub", "quit"], True))
        self.assertNotIn("Error:", out)


if __name__ == "__main__":
    unittest.main()
