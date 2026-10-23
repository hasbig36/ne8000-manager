# NE8000 Manager

A Winbox-style web GUI for the Huawei NetEngine 8000 M8 (VRP8). It runs on a PC or server that can reach the router's management IP, connects over SSH, and you use it from any browser. No internet access is needed: there are no external scripts or fonts.

## Features

| Menu | What you can see | What you can change |
|---|---|---|
| **Dashboard** | CPU, memory, uptime, temperature, ports up, BGP sessions, alarms, CPU/memory graphs, busiest interfaces | |
| **Interfaces** | Every port, sub-interface and logical interface: state, IP, VPN, VLAN, utilisation bars, errors. Click a row for a **live Rx/Tx graph**, counters, MAC, speed, config and raw output | Enable/disable, description, MTU, add/remove IP, reset counters, create sub-interfaces and loopbacks, delete logical interfaces |
| **VLAN** | Dot1q sub-interfaces, bridge VLANs with member ports, VLANIF interfaces, layer-2 ports | Create dot1q / dot1q-termination / QinQ sub-interfaces, VLAN + VLANIF, trunk/access ports, back to routed |
| **Queues / QoS** | Rate limits (CAR), port shaping, port-queue scheduling, traffic policies, **live per-queue pass/drop statistics** | Rate limit any interface in/out (like a MikroTik simple queue), port shaping, queue WFQ/PQ weights, "simple queue" policies (ACL + classifier + CAR) and binding them |
| **IP** | Addresses, routing table (public or per VPN), static routes, ARP, VPN instances, LLDP neighbors | Add/remove addresses, static routes, static ARP, VPN instances |
| **Routing** | BGP peers with state, prefixes, messages, received/advertised routes, full BGP table; OSPF neighbors, interfaces, LSDB; MPLS LDP sessions and LSPs | Add/remove/disable BGP peers (MD5, update source, multihop, route-policies, max prefixes), hard reset or soft refresh, advertise networks; OSPF networks, interface cost, process reset |
| **System** | Resource graphs (CPU, memory, temperature, last hour), boards, power, fans, serial numbers, local users, sessions, clock, NTP, SNMP, alarms, log buffer | Users (add, delete, password), sysname, NTP servers, SNMP community, syslog server |
| **Planning** | Saved network plans with drawing, tasks, executions and version history | Draw a design, generate or add tasks, check or execute them on the router, restore old versions |
| **Tools** | Traffic monitor (up to 6 interfaces side by side), ping and traceroute with source/VPN, console for display commands, config editor, backup | Config editor with "check only" and "apply & commit", save to startup, download running config |

### Splynx billing (Billing › Splynx billing)
Keeps reseller ports in line with Splynx:
- **Blocked** (or disabled) in Splynx → the customer's router port is **shut down**.
- **Active** again (Splynx re-activates the customer after a payment) → the port is **enabled** again.

How customers are matched to ports, in this order:
1. **Manual mapping** you set on the page (or "do not manage").
2. **IP**: the customer's Splynx service IPv4 (or routed subnet) falls inside an interface's IP network. The most specific interface wins.
3. **Description**: the interface description contains the Splynx customer ID or login (for example `Reseller FAST CABLE 552927`). Useful for resellers that have no IP in Splynx.

Safety rules:
- Only ports that billing shut down are re-enabled. A port an engineer shut by hand is left alone.
- A port shared by several customers is only shut when **all** of them are blocked.
- **Dry run** mode shows what would happen without touching the router. **Enforce** applies changes automatically every interval (default 120 s), and every change is logged.
- Filters: partners (resellers), locations and the Splynx router (e.g. `BNG-NDOVU1`).

Setup: in Splynx go to Administration › API keys, create a key with read access to customers, internet services, partners, locations and routers, then enter the Splynx URL, key and secret on the page and press **Test connection**. The settings are saved in `billing.json` next to the app (readable only by your user); the log and the list of ports shut by billing are in `billing_state.json`. Billing runs while the app is connected to the router.

### Network plans (Planning › Network plans)
Design a change before you make it, run it on the router, and keep a record of it:
1. **Design**: draw the network on a canvas. Add the NE8000, routers, switches, OLTs, resellers, customers, upstreams, servers, towers and notes, drag them into place, and use **Connect devices** to draw links. Each device and link has properties (management IP, Splynx ID, BGP AS, NE8000 port, VLAN, IP/prefix, rate limit, VPN, notes).
2. **Tasks**: the router changes the plan makes. Press **Generate tasks from design** to turn links from the NE8000 into sub-interface and rate-limit tasks (and resellers with a BGP AS and peer IP into BGP peers), or add tasks by hand: sub-interfaces, interface changes, IPs, loopbacks, VLANs, port modes, CAR, shaping, static routes, VPN instances, BGP, OSPF or custom config lines. Every task shows the exact VRP commands.
3. **Execute**: **Check on router** sends the tasks and aborts (syntax check, nothing changes). **Execute & commit** runs them in order, commits each one and stops at the first failure. The plan's status becomes executed or failed.
4. **Record**: every save keeps the previous version with who changed it, when, and what changed. You can view or restore any version (the current one stays in the history). Every check and execution is logged with the router, the person, and the router's output for each task. **Print / PDF record** produces a one-page record with the drawing, devices, commands, executions and history.

Plans are saved as JSON files in the `plans/` folder next to the app, one file per plan, so they can be backed up or copied to another PC. Plan statuses: draft, approved, checked, executed, failed, cancelled. Passwords are never stored in plans.

### Safe changes
Every form shows the exact VRP commands before anything is sent. You then choose:
- **Apply & commit**: lines are sent in system-view and committed (VRP8 two-stage configuration).
- **Check only**: lines are sent and then aborted, so the router checks syntax but nothing changes.

Nothing is saved to startup until you press **Save config**.

### Graphs
While you are connected, the app polls CPU and memory every 5 s, temperature every 30 s, and interface utilisation every 10 s, and keeps one hour of history. Rx/Tx graphs are calculated from interface byte counters for the interfaces you are looking at.

## Requirements on the router
SSH must be enabled for a user with level 3 rights, for example:
```
stelnet server enable
aaa
 local-user admin password irreversible-cipher <password>
 local-user admin service-type ssh
 local-user admin level 3
 quit
ssh user admin
ssh user admin authentication-type password
ssh user admin service-type stelnet
user-interface vty 0 4
 authentication-mode aaa
 protocol inbound ssh
commit
```
(Adjust to your AAA setup.)

## Run it
You need Python 3.9 or newer.

- **Windows**: double-click `run.bat`.
- **Linux / macOS**: `./run.sh`

Then open http://127.0.0.1:5000. On first start the console prints an app login (`admin` and a random password). Sign in, change the password under **Manager › My account**, then enter the router's IP, SSH port, username and password, or click **Try demo mode** to explore with a simulated router (changes in demo mode only affect the simulation).

## Hosting
The app is ready to run on a server for your whole NOC team. Put it on a Linux server (or VM) that can reach the router's management IP, and put HTTPS in front of it.

### App logins
Everyone signs in to NE8000 Manager first, then connects to the router with their own SSH account.
- **Roles:** *admin* (everything, including app users), *operator* (everything except app users), *viewer* (can look at everything, every change is refused).
- Admins manage logins under **Manager › App users**. Passwords are stored as salted hashes, need at least 10 characters, and a password change or deleted user signs that person out everywhere.
- After 5 failed sign-ins from one address that address is locked for 5 minutes (20 failures lock the username).
- **Manager › Audit log** records sign-ins, router connections, every change with its commands, config edits, saves, billing changes and plan executions, with the person and their IP. Plan versions and executions are recorded under the signed-in user.
- Forgot the admin password? On the server: `python app.py --add-user admin --role admin` (or `docker compose exec ne8000 python app.py --add-user admin`) sets a new one.

### Quick install (Ubuntu / Debian)
```
sudo ./install.sh                                  # LAN / VPN use on 127.0.0.1:8080
sudo DOMAIN=ne8000.example.com ./install.sh        # with automatic HTTPS
sudo ./update.sh                                   # later: pull the latest version and restart (data is backed up first)
```

### Option 1: Docker (recommended)
```
cp .env.example .env        # edit it: domain, admin password, router IP, demo on/off
docker compose up -d
docker compose logs ne8000  # shows the first admin password if you did not set one
```
The app listens on 127.0.0.1:8080 on the server, and its data lives in `./data`. For automatic HTTPS with Let's Encrypt, point a DNS name at the server, set `NE8000_DOMAIN` in `.env`, open ports 80 and 443, and run `docker compose --profile https up -d` (bundled Caddy). If you already run nginx or Caddy, use `deploy/nginx.conf` or `deploy/Caddyfile` instead. If the router's management network is only reachable from the host itself (VPN, policy routes), switch the service to `network_mode: host` (see the comment in `docker-compose.yml`). If Docker Hub is rate-limited, build with a mirror: `docker build --build-arg BASE_IMAGE=<mirror>/python:3.12-slim .`

### Option 2: systemd (no Docker)
Follow the steps at the top of `deploy/ne8000.service`: copy the app to `/opt/ne8000-gui`, create a virtualenv, install `requirements.txt`, enable the service, then put `deploy/nginx.conf` or `deploy/Caddyfile` in front of it.

### Settings (environment variables)
| Variable | Default | What it does |
|---|---|---|
| `NE8000_DATA_DIR` | app folder (`/data` in Docker) | users, plans, billing settings, audit log, session key |
| `NE8000_HOST` / `NE8000_PORT` | 127.0.0.1 / 5000 (0.0.0.0 / 8080 in Docker) | listen address |
| `NE8000_ADMIN_USER` / `NE8000_ADMIN_PASSWORD` | admin / random | first admin, only used when there are no users yet |
| `NE8000_BEHIND_PROXY` | 0 | set to 1 behind an HTTPS proxy: HTTPS-only login cookie, real client IPs from X-Forwarded-For |
| `NE8000_TRUSTED_PROXY` | `*` | address of the proxy whose X-Forwarded headers are trusted (keep the app port private) |
| `NE8000_ALLOW_DEMO` | 1 | show "Try demo mode" |
| `NE8000_DEFAULT_HOST` / `NE8000_DEFAULT_PORT` | empty / 22 | pre-fill the router form |
| `NE8000_SESSION_HOURS` | 12 | how long a sign-in lasts |
| `NE8000_THREADS` | 16 | web server threads |
| `NE8000_SECRET_KEY` | generated, saved in the data folder | session signing key |

### Good practice
- Do not expose port 8080 directly. Only the HTTPS proxy should reach it, and ideally only your office or VPN should reach the proxy (firewall or the proxy's allow list).
- The app runs as one process with many threads (waitress). Router sessions and graphs live in that process, so do not run several copies behind a load balancer.
- Back up the data folder; it holds the plans, the audit log, users and billing settings.
- Billing enforcement runs while at least one person is connected to that router, and only one billing loop runs per router however many people are connected.

## Notes
- Router credentials are only used to open the SSH session and are never written to disk. Passwords and SNMP communities are masked in previews and results.
- Uses Netmiko's `huawei_vrpv8` driver.
- Parsers follow standard VRP8 output. If a table is empty on your software release, the raw command output is shown instead (or run the command in Console), and the parser in `parsers.py` can be adjusted from a sample.
- Some features depend on the line cards and licences in your chassis (for example bridge VLANs, port-queue). The router's own error message is shown if it rejects a command.

## Files
- `app.py`: web server, API and login checks
- `auth.py`: app users, roles, sign-in throttling, audit log
- `device.py`: SSH session, background poller, views
- `parsers.py`: VRP8 output parsers
- `actions.py`: every change, validated and turned into CLI lines
- `demo.py`: simulated NE8000 M8 for demo mode
- `billing.py`: Splynx billing sync
- `plans.py`: network plans, versions and execution
- `templates/`, `static/`: the browser UI
- `Dockerfile`, `docker-compose.yml`, `.env.example`, `deploy/`: hosting (Caddy, nginx, systemd)
