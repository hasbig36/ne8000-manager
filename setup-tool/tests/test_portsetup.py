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


ROWS = [{"name": "GigabitEthernet0/1/5", "type": "ethernet", "ip": "", "phy": "*down", "l2": False, "description": ""},
        {"name": "GigabitEthernet0/1/6", "type": "ethernet", "ip": "10.10.5.1/30", "secondary": ["192.168.1.10/24"], "phy": "up", "l2": False,
         "description": "Mgmt switch"}]
LLDP = "System name : SW-NEW\nManagement address value : 10.10.5.2\n"


class Plan(unittest.TestCase):
    def test_port_with_address(self):
        r = StubRouter(ROWS, {"display current-configuration | include first-time": "ssh client first-time enable",
                              "display lldp neighbor interface": LLDP})
        p = portsetup.plan(r, "GigabitEthernet0/1/6")
        self.assertTrue(p["ok"])
        self.assertEqual(p["candidates"], ["10.10.5.2", "192.168.1.253", "192.168.1.1"])
        self.assertEqual(p["own"], ["10.10.5.1", "192.168.1.10"])
        self.assertNotIn("lines", p)                     # read-only: nothing for the router

    def test_bare_port_cannot_be_used(self):
        r = StubRouter(ROWS, {})
        p = portsetup.plan(r, "GigabitEthernet0/1/5")
        self.assertFalse(p["ok"])
        self.assertIn("laptop tool", " ".join(p["blockers"]))
        self.assertFalse(p["first_time"])

    def test_rejects_bad_input(self):
        r = StubRouter(ROWS, {})
        for port in ("Gi0/1/5; reboot", "GigabitEthernet0/1/9", "GigabitEthernet0/1/6.100"):
            with self.assertRaises(portsetup.PortSetupError):
                portsetup.plan(r, port)

    def test_never_writes_to_the_router(self):
        import inspect
        src = inspect.getsource(portsetup)
        self.assertNotIn("_configure", src)
        self.assertNotIn("send_config_set", src)


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
        """Test fixture: the port already has its addresses (configured by hand beforehand)."""
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
        plan = {"port": "GigabitEthernet0/1/5", "candidates": ["192.168.1.253", "192.168.1.1"], "own": ["10.10.5.1", "192.168.1.10"]}
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
        self.assertNotIn("ssh client peer 192.168.1.253", self.rtr.running)


if __name__ == "__main__":
    unittest.main()
