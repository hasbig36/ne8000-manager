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

### Traffic per address (NetFlow)
The router counts traffic per interface only, so the primary and "sub" addresses on one port share one counter. With the router's NetStream export sent to the manager (UDP 2055), **Traffic per address** graphs every subnet on the router, and the IP dropdown on an interface has a Graph for each address: last hour per minute, then day, week, month and year like the interface graphs, plus the busiest addresses. A flow counts for a subnet when its source or destination is in it, or its next hop is (a block routed to a customer behind the link).

**Router setup** on that page builds the NetStream lines (1-in-1000 sampling, inbound on each interface so a flow is counted once, NetFlow v9, 1-minute export) for you to review; press **Check only** first. Only routers saved under Manager > Routers with an SNMP login are accepted; other senders are dropped. Settings: `NE8000_NETFLOW_PORT` (2055, 0 = off), `NE8000_NETFLOW_SAMPLING` (1000, used when the router does not report its sampling rate).

### Port view

The dashboard and the Interfaces page show the router's ports as tiles. A front panel shows every physical port, coloured
by state (green up, amber link up but protocol down, red down, grey disabled), with its speed, description and a traffic bar.
Each port that has sub-interfaces gets its own row of tiles, one per sub-interface (customer VLAN). The filters (Up, Down,
Busy 65%+, Errors) narrow the sub-interface tiles. Hover over a tile for its IP, VLAN, client MAC and traffic, and click it
for the live drawer. When the router reports its boards, the front panel is drawn as the chassis: every slot by number
(empty slots shown blank), each board with a status light, ports in their cages with the optic type, and the power, fan
and clock boards along the bottom. On the Interfaces page, clicking a port lists it with its sub-interfaces in the table below.

Switch the front view between **Drawing** and **Photo**. Photo asks for a straight-on picture of the router's front
(on a phone it opens the camera), then finds the router's front in it and crops away the background (rack, walls, cables beside it) and
straightens an angled shot; drag the four corners to adjust. It then asks you to tap where each port is, once.
Crop / straighten works on a saved photo too and keeps the port marks. All of this runs in the browser. After that each port is a ring on
your own photo, coloured live; hover for traffic and optics, click for details. The photo (resized to 1600 px, JPEG or
PNG only) and the positions are kept per router in `data/panel/`. Viewers can look but not change them.

**Device notes** sit beside the front view: what the next engineer must know about this router (patching, spare
slots, replaced parts, who to call). Each note shows who wrote it and when, can be pinned to the top, edited or deleted,
and can be tied to a port, which then shows a 📝 badge with the note on hover. Notes are kept per router in
`data/notes/` and every change is in the audit log. Viewers can read them.

### Speed: router data from server memory

Pages no longer wait for the router. `viewcache.py` keeps the last answer of every view per connected router in the
server's memory and serves it at once (a few ms). A background thread refreshes the busy views ahead of time while
someone uses that router: dashboard and interfaces every 5-8 s, ARP and BGP about every 20 s, QoS every minute,
hardware every 5 minutes. Older data is served at once and refreshed behind it; nothing older than 10 minutes is shown
without asking the router first. Any change made through the app drops the stored views, so the change shows at once.
The `X-Data-Age` response header says how old a view is.

### Health check

Every 5 minutes, for each saved router with an SNMP login, the manager looks for what makes customers
slow and learns each link's normal levels (about a day of memory; "unusual" needs 2 hours of history):
input errors (CRC), drops in and out, links over 90% full, SFP light outside or near the module's alarm
levels or falling from its normal, links down or flapping, router CPU/memory/temperature, and latency and
loss to the router and to addresses you list (Health check > Ping targets; "Add customers' CPEs" fills them
from ARP). Pings are sent from the manager's server without root, through an ICMP datagram socket, which
Docker allows by default (`net.ipv4.ping_group_range`). Turn it off with `NE8000_HEALTH=0`.

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

Then open http://127.0.0.1:5000. On first start the console prints an app login (`admin` and a random password). Sign in, change the password under **Manager › My account**, then add your router under **Manager › Routers** (or type its IP and SSH port), enter your router username and password, or click **Try demo mode** to explore with a simulated router (changes in demo mode only affect the simulation).

## Hosting
The app is ready to run on a server for your whole NOC team. Put it on a Linux server (or VM) that can reach the router's management IP, and put HTTPS in front of it.

### App logins
Everyone signs in to NE8000 Manager first, then connects to the router with their own SSH account.
- **Roles:** *admin* (everything, including app users), *operator* (everything except app users), *viewer* (can look at everything, every change is refused).
- Admins add routers under **Manager › Routers** (name, management IP, SSH port, optional default username). Everyone then picks a router from a list on the connect screen, and once at least one router is saved the app only connects to saved routers. Router passwords are never stored.
- Admins manage logins under **Manager › App users**. Passwords are stored as salted hashes, need at least 10 characters, and a password change or deleted user signs that person out everywhere.
- After 5 failed sign-ins from one address that address is locked for 5 minutes (20 failures lock the username).
- 5 failed router SSH logins lock that app user's router connections for 5 minutes. Viewers cannot read the full configuration (it contains password hashes and SNMP communities).
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
| `NE8000_ALLOWED_ROUTERS` | empty (any) | extra routers the app may connect to besides the ones saved under Manager › Routers: IPs, subnets or hostnames, comma-separated |
| `NE8000_ALLOW_DEMO` | 1 | show "Try demo mode" |
| `NE8000_DEFAULT_HOST` / `NE8000_DEFAULT_PORT` | empty / 22 | pre-fill the router form when no routers are saved |
| `NE8000_SESSION_HOURS` | 12 | how long a sign-in lasts |
| `NE8000_THREADS` | 16 | web server threads |
| `NE8000_SECRET_KEY` | generated, saved in the data folder | session signing key |

### Good practice
- Do not expose port 8080 directly. Only the HTTPS proxy should reach it, and ideally only your office or VPN should reach the proxy (firewall or the proxy's allow list).
- The app runs as one process with many threads (waitress). Router sessions and graphs live in that process, so do not run several copies behind a load balancer.
- Back up the data folder; it holds the plans, the audit log, users and billing settings.
- Billing enforcement runs while at least one person is connected to that router, and only one billing loop runs per router however many people are connected.

## Notes
- Router SSH credentials are only used to open the SSH session and are never written to disk. Passwords and SNMP communities are masked in previews and results.
- **SNMP first.** An admin can give a saved router a read-only SNMPv3 login under Manager > Routers. Connect then uses SNMP with no password: dashboard, interfaces, traffic graphs, optics, BGP, ARP, LLDP neighbours, hardware and resources. Pages that need the router CLI (routes, QoS, VLANs, console, config, changes and billing enforcement) ask for an SSH login when opened. If SNMP fails and a password was typed, Connect falls back to SSH. The SNMP passwords are stored in `routers.json` (mode 600) in the data folder and are never sent to browsers.
- Uses Netmiko's `huawei_vrpv8` driver.
- Parsers follow standard VRP8 output. If a table is empty on your software release, the raw command output is shown instead (or run the command in Console), and the parser in `parsers.py` can be adjusted from a sample.
- Some features depend on the line cards and licences in your chassis (for example bridge VLANs, port-queue). The router's own error message is shown if it rejects a command.

### Search, NOC view and live data

- **Search everything** (top bar; press / or Ctrl+K): interfaces, addresses, ARP, BGP peers (by IP, AS or description), VLANs, VPNs, route-policies and prefix lists, QoS, LLDP neighbours, Splynx customers, the network map, notes, backups, the change log and the app's pages. It searches what the server already holds, so it never waits for the router. A result opens the right page, filtered to it (a BGP peer opens its statistics).
- **NOC** (top bar): the live network map full screen and dark, for a wall screen: traffic in and out, warnings, BGP sessions up, links and interfaces down (down links flash red), CPU, memory, and a ticker of the latest changes. Esc leaves it.
- **Warnings on the map** (NOC, and the Network map page): a ⚠ on a line, a device or a town, amber for a warning and flashing red for critical. Its tooltip, the side panel and the Warnings tile say what, since when and what to do. They come from the health check and the live data: high latency or loss, **distance changed** (the shortest ping round trip stepped up and stayed up, so traffic takes a longer path; about 100 km of fibre per extra millisecond), a port **filling up** (80% of the port or of the line's QoS plan) or full, a link or BGP session **flapping** or down, CRC errors and drops, SFP light weak or falling, router CPU, memory and temperature, and **fibre events from an OTDR** (below).
- **Zoom:** − / + / Fit in the NOC header, or the mouse wheel; drag empty space to move around.
- **Kenya map** (NOC header: Diagram | Kenya map): the network on a map of Kenya with its counties, the router's town as the hub (Nairobi unless placed elsewhere) and a glowing line to every town it reaches, coloured by load (orange normal, yellow and red busy, flashing red down) with the traffic flowing. A device's town comes from its name or description (MARSABIT-…, …-KISUMU), else the town of the device it hangs off; click a device or line in the side panel to set its town by hand (stored by the app, the same for everyone). Click a town for its devices and warnings, double-click to zoom in on its devices. Map data: Natural Earth (public domain) and geoBoundaries (CC BY 4.0).
- **Draw a line** (NOC header, + Line): for a link the router cannot see by itself. Pick the router interface it uses and the device at the far end (one on the map, or a new one with its town). If the far end is a router saved under Manager > Routers with an SNMP login, pick its interface too: the line then shows traffic from both ends and warns when the far end stops reporting or the two ends disagree. Lines are kept by the app (never sent to the router) and can be changed or deleted from the side panel.
- **Same device on two links:** when the same LLDP neighbour name, or the same neighbour MAC in the ARP table, shows up on two or more interfaces, the lines are marked "2× same device" with their role: primary / backup (one carries 90% or more of the traffic), both active (sharing the load) or idle. When one of them goes down the remaining line gets a "Redundancy lost" warning.
- **Major sites** (NOC header: Major sites): Google, YouTube, Facebook, WhatsApp, Instagram, TikTok, X, Wikipedia, ChatGPT, Netflix, Safaricom and eCitizen (Edit the list in the side panel). Every 5 minutes the app's server pings each one (5 pings) and traces the path to it (one probe per hop, no root needed); the server's traffic goes out through the router, so this is the way customers reach them. The map of the region shows the Mombasa landing, the submarine cables to Djibouti, Europe, the Gulf and South Africa, and a route to each site with traffic moving along it, coloured by delay; where a site is served from is estimated from its delay. Click a site for its delay now and normally, loss, the hops, the BGP upstream it leaves by, a day's graph, and the changes logged (delay jumps, path changes).
- **OTDR / fibre monitoring feed:** an OTDR or fibre monitoring system can post fibre events to `POST /api/otdr` with the header `Authorization: Bearer <NE8000_OTDR_TOKEN>` (set that variable in the container's environment) and a JSON body like `{"router": "102.216.116.1", "link": "GigabitEthernet0/1/6.301", "event": "break", "distance_km": 12.4, "loss_db": 3.1, "source": "EXFO"}`. `link` is an interface name or a line label on the map; `event` is break, loss, bend, reflectance, or ok (clears that fibre). The event shows on the health page and as a warning on that line for 7 days. A signed-in admin can post the same body from the app's session.
- **Live data:** ports, interfaces and map traffic update every 5 s. The slower router reads (the map, System, Route policies, VLANs, OSPF, IS-IS, MPLS, VPNs, logs, LLDP) are kept fresh in the background, so pages open from memory. Stored router data is only dropped after a real change on the router (a commit, reset, save or restore), not after a Review, Check only, note or backup.
- **SSH stays connected:** the app sends SSH keep-alives and a harmless Enter when the session has been idle for 4 minutes (so the router's vty idle-timeout never closes it), and logs in again by itself with the login held in memory if the router or the network drops the session. After the app restarts (a deploy), sign in to SSH again: the password is never written to disk.

### Backups and restore (Tools › Backup)

Copies of the running configuration are kept on the manager's server, in `<data>/backups/<router>/` as plain `.cfg` text with a small `.json` beside each, so any backup of the data folder covers them.

- **Back up now** keeps a copy by hand, with an optional note. **Upload a .cfg** keeps a file from your PC (up to 8 MB).
- **Automatic copies**: after every change committed in the app (actions, config editor, restore), after every Save, once a day while someone is signed in with SSH, and before every restore. An automatic copy is kept only when the configuration really changed (time stamps and comments are ignored). The newest 60 automatic copies are kept; pinned, by-hand and uploaded copies stay until deleted.
- The app **never saves to startup and never restores by itself**. Save stays a button press.
- **Compare with router** shows which sections differ and a line-by-line diff.
- **Restore…** replaces the whole running configuration with the copy, the router's own way: the copy is copied to `cfcard:ne8000mgr-restore.cfg` over SFTP (over the open SSH session, or a second login on the router's `sftp server port`; the password is held in memory only), then `load configuration file … replace` in system view. **Check only** shows the router's own `display configuration candidate changes` and throws the candidate away. To commit, type the copy's id. A before-restore copy of the router is kept first, and the commit carries the description `ne8000-manager restore <id>`. The file on cfcard: is removed afterwards. Press Save to keep the restored configuration over a reboot.

### Change log and route policies

**Change log** (top of the menu) records what changed on the router and when, in `data/changes.db`
(events kept 6 months, numbers 3 months):
- ARP: hosts that appear, disappear (after 3 missed checks) or change MAC (a moved host, an IP clash or spoofing);
- MAC table: new, gone or moved entries (empty on a pure layer-3 router);
- routes: every direct and static route added, removed or with a new next hop. A full Internet table is never
  compared route by route: routes per protocol are counted about every 5 minutes, shown as a graph, and a jump or
  drop of 5% or more is logged;
- protocol sessions (BGP, OSPF, IS-IS, LDP): every time one goes down or comes back up, with the router's reason
  (from `display bgp peer X log-info`, or the BGP error code over SNMP) and how long it was down;
- BGP route counts: received, active and advertised per peer, kept as numbers; a 20% drop or jump is logged.

For every saved router with an SNMP login it checks ARP, BGP, OSPF, IS-IS, LDP and the route total every minute,
around the clock (`NE8000_CHANGES=0` turns this off). Routes, the MAC table and route-policy counters need the CLI,
so they are checked about every 5 minutes while someone is signed in with SSH, and after every change made in the app.

The BGP page shows active and advertised routes per peer, the change in received routes over 1 hour and 24 hours,
each peer's import / export policy, a History graph per peer and the recent session changes.

**Route policies** lists every route-policy with its nodes, what each matches, what it applies, the router's match
counters (with the matches in the last hour and day), which BGP peers use it, and the prefix lists with their
counters. "What would a policy do to a prefix?" walks the rules for one prefix the way the router does; rules about
the AS path or communities depend on the route, and are shown as "depends".

Policies can be edited on that page: add, edit or remove a rule, make a new policy, copy one under a new name,
delete one, attach it to or take it off a BGP peer, and add, edit or remove prefix-list entries. Every change goes
through the usual Review / Check only / Apply dialog. A policy used by a peer asks for its name to be typed and
offers a soft refresh of those peers after the commit; a policy or prefix list in use cannot be deleted.

## Files
- `app.py`: web server, API and login checks
- `auth.py`: app users, roles, sign-in throttling, audit log
- `device.py`: SSH session, background poller, views
- `parsers.py`: VRP8 output parsers
- `actions.py`: every change, validated and turned into CLI lines
- `demo.py`: simulated NE8000 M8 for demo mode
- `billing.py`: Splynx billing sync
- `plans.py`: network plans, versions and execution
- `netflow.py`: NetFlow v5/v9/IPFIX collector for traffic per address
- `changes.py`: change log (ARP, MAC, routes, sessions, BGP counts, policy counters); `policy.py`: route-policy check for one prefix
- `health.py, panel.py`: smart health check (errors, drops, full links, SFP light, flaps, latency and loss, CPU/memory/heat)
- `templates/`, `static/`: the browser UI
- `Dockerfile`, `docker-compose.yml`, `.env.example`, `deploy/`: hosting (Caddy, nginx, systemd)
