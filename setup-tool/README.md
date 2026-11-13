# Huawei Device Setup (laptop tool)

A single program for a Windows or Mac laptop that finds a new Huawei device, checks the connection, and gives it its first configuration, so it is ready to use with no PuTTY and no separate manual step. Nothing needs to be installed: no Python, no terminal program.

## Get it
Download the zip for your laptop from the **Actions** tab of this repository (workflow *Huawei Device Setup*, newest run, **Artifacts**):
- `HuaweiDeviceSetup-Windows.zip`: unzip and double-click `HuaweiDeviceSetup.exe`. If Windows SmartScreen warns, choose *More info → Run anyway* (the program is not code-signed).
- `HuaweiDeviceSetup-macOS-AppleSilicon.zip` (Apple Silicon Macs; Intel Macs are not built): unzip, then **right-click → Open** the first time (the app is not notarised).

It opens a page in your browser. Everything runs on the laptop (`127.0.0.1`); nothing is sent anywhere else and no password is saved. Press **Quit** on the page, or close the window, to stop it.

## The five steps
1. **Find**: for a device plugged straight into the laptop, **Join that network and search** gives the laptop's wired port an address in the device's factory network (192.168.1.10 for 192.168.1.x, after the Windows / macOS administrator prompt) and searches. On Windows a port on DHCP gets the fixed address and goes back to DHCP afterwards; on macOS the address is added beside the normal one. **Put the laptop back** (or Quit) undoes it. Routers such as the NE8000 usually have no factory address: use the console cable. The search itself every network the laptop is on (a /24 around the laptop on larger networks) plus the usual Huawei factory addresses (192.168.1.253, 192.168.1.1, 192.168.0.1, 169.254.1.1) for SSH (22) and Telnet (23), and marks the Huawei ones. You can add a subnet to search. Click a device to use it.
2. **Connect**: if nothing is found, type the address, port, username and password, or choose **Telnet** or **Console cable** (USB serial, 9600 by default). **Check connection** goes step by step (address, same network, port answers, log in, identify) and says where it stopped and why: no route from the laptop, no answer (cable, booting, wrong network), SSH switched off (and whether Telnet is open instead), wrong password, user locked, or not a Huawei VRP device. When a factory-new device insists on a password change at first login, the tool asks for the new password and does the change.
3. **Settings**: hostname, admin user and password (SSH), management interface / IP / VLAN, default gateway, DNS, NTP, syslog, time zone, SNMPv3 user with an allowed source, login banner, SSH-only, and the SSH host key for a device reached by Telnet or console.
4. **Review and apply**: every command is shown and can be edited. **Check only** (routers, VRP8) sends the lines and throws them away. **Apply** sends them, commits (VRP8), saves, then logs in again over SSH with the **new admin user** to prove it works. Only then is the new management address set; the tool follows the device to it and saves there. If a line is rejected on a router, nothing is committed.
5. **Ready**: a clear *ready* / *not finished* result with each step. If the new address is on a network the laptop cannot reach, the tool says whether the change is saved and offers **Finish at the new address** once you have moved the laptop. **Save a setup report** downloads a text record (passwords masked).

The commands come from the same template as *Manager → Adopt* in NE8000 Manager (`adopt.py`), so a device set up with the laptop matches one adopted from the web app.

## Supported devices
Huawei VRP8 routers (NE8000, NE40E, CE, ATN: two-stage commit) and VRP5 S-series switches (lines take effect at once). The platform is read from `display version`.

## Develop
```
cd setup-tool
pip install -r requirements.txt
python huawei_setup.py                                   # opens the browser
python -m unittest discover -s tests -v                  # end-to-end tests against a fake device
python tests/fake_device.py vrp8                         # a fake device on ssh 2222 / telnet 2323 (root / Changeme_123)
pip install pyinstaller && pyinstaller huawei-setup.spec # build dist/HuaweiDeviceSetup
```
