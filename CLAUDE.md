# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

VPN monitoring and kill-switch system for a Raspberry Pi running OpenVPN + qBittorrent. The system continuously monitors VPN connectivity, detects IP leaks, and shuts down torrenting if the VPN is compromised.

There are **two independent control planes** over the same machine, and they are not equivalent:

- **CLI plane** — `startvpn.sh` → `checkip.sh` (monitor) → `stopvpn.sh`
- **Web plane** — `start_web.sh` → `webapp/app.py` (Flask) → `webapp/monitor.py` (`VPNMonitor`)

`webapp/monitor.py` reimplements `checkip.sh`'s state machine in Python and does more: DNS pinning with `chattr +i`, IPv6 sysctl disable, and a qBittorrent `InterfaceAddress` hard bind. The shell plane does none of those. Starting via the web UI and stopping via `stopvpn.sh` therefore leaves `/etc/resolv.conf` immutable and IPv6 off — use `remove_killswitch.sh` to undo that.

## Running the Scripts

```bash
# Start the whole CLI flow (kill switch + OpenVPN + monitor + qBittorrent)
./startvpn.sh

# Start VPN monitoring only (pass your home/ISP IP as argument)
./checkip.sh <home_ip>

# Check current status of all services
./vpn_status.sh

# One-shot VPN check. Exit codes: 0 = secure, 1 = confirmed leak,
# 2 = could not determine. 0 is also shell success, so
# `if python3 vpn_active.py "$ip"; then start_torrenting; fi` is correct.
python3 vpn_active.py <home_ip>

# Web UI
./start_web.sh
./stop_web.sh

# Emergency: remove the kill switch when it has locked you out
./remove_killswitch.sh
```

## Architecture

### Component Relationships

`checkip.sh` is the CLI monitoring daemon. It calls `vpn_active.py` for full IP checks and manages the qBittorrent process lifecycle directly.

**Two-tier monitoring loop in `checkip.sh`:**
- **Fast checks** (every `FAST_CHECK_INTERVAL` seconds, default 2s): OpenVPN process (`pgrep -x`), tun0 interface (`ip link`), and default route (`ip route get 8.8.8.8`)
- **Full IP checks** (every `IP_CHECK_INTERVAL` seconds, default 10s): calls `vpn_active.py`, which queries external IP services and compares against the home IP argument

`checkip.sh` reads `vpn_active.py`'s **stdout** (`secure` / `leak` / `error`), not its exit code. Both are kept in sync; change one and you must change the other.

**Failure response:** stop qBittorrent, then exit. **There is no auto-reconnect in `checkip.sh`** — it was removed. The kill switch is deliberately left active on exit so nothing leaks; `stopvpn.sh` removes it. The web app has a manual "Force Reconnect" button (`VPNMonitor.attempt_reconnect`); the monitor loop does not reconnect on its own.

### The kill switch

UFW-based, not raw iptables. `ufw_killswitch.sh` applies it; `ufw_base.sh` restores the base state (and takes `OUTGOING_POLICY=deny` so the kill-switch path can enable the firewall with egress already blocked rather than transiting an allow-outgoing window).

**Detecting whether it is active requires checking two things**, and every consumer does:

```bash
sudo ufw status verbose | grep -q "deny (outgoing)"      # UFW's configured policy
sudo iptables -S OUTPUT  | grep -q "ufw-before-output"   # the LIVE kernel chain
```

`ufw status` reads UFW's own config files. Flushing the iptables OUTPUT chain (`iptables -F OUTPUT`) removes UFW's jump rules so nothing filters at all, while `ufw status` keeps reporting `deny (outgoing)`. Checking only the first is a fail-open: the machine reports itself protected while it is wide open. **Never flush the OUTPUT chain to disable the kill switch** — drive UFW instead.

Consumers: `checkip.sh:check_killswitch_active`, `vpn_status.sh`, `VPNMonitor.check_killswitch_active`.

### Untrusted `.ovpn` configs

A downloaded or uploaded `.ovpn` is installed to `/etc/openvpn/client/` and then run by `sudo openvpn`, and `ufw_killswitch.sh` parses its `remote` line to decide which egress to whitelist. It therefore chooses both the tunnel endpoint and the firewall hole opened for it.

Both install paths validate before installing — `webapp/monitor.py:validate_ovpn_config` and the equivalent `grep` in `startvpn.sh` — rejecting directives that run commands, write files as root, or open a control socket (`up`, `down`, `script-security`, `plugin`, `status`, `writepid`, `management*`, …). `--script-security 0` is also passed on both command lines. Keep the two lists in sync.

### Key Files

- `checkip.sh` — CLI monitoring daemon
- `vpn_active.py` — one-shot IP-leak detector; checks OpenVPN process, tun0 **up** state, routing via tun0, and external IP (ipify, httpbin). `api64.ipify` is deliberately **not** used: it is dual-stack and an IPv6 answer can never equal the IPv4 home IP, so every check would read "secure"
- `vpn_status.sh` — read-only status display
- `webapp/app.py` — Flask routes and auth
- `webapp/monitor.py` — `VPNMonitor`: the web plane's full state machine
- `webapp/organizer.py` — media file organizer used by the web UI
- `organize.py` — the interactive CLI organizer, invoked from `stopvpn.sh`
- `ufw_killswitch.sh` / `ufw_base.sh` — the only writers of firewall state

### Configuration

Optional config file loaded at startup from `~/.vpn_config.conf` or `./vpn_config.conf`:

```bash
FAST_CHECK_INTERVAL=2       # seconds between process/interface checks
IP_CHECK_INTERVAL=10        # seconds between full IP leak checks
PID_DIR=/tmp/vpn_pids       # where the qBittorrent PID file is stored
```

Web app configuration lives in `webapp/.env` (see `webapp/.env.example`): `VPN_API_TOKEN`, `HOME_IP`, `BIND_HOST`, `PORT`, `ORGANIZER_ROOTS`, `ACCESS_LOG`.

**The web API is never unauthenticated.** If `VPN_API_TOKEN` is unset, `app.py` generates one, appends it to `webapp/.env` (mode 0600), and prints it once. The token is what prevents CSRF: a cross-origin form cannot set an `Authorization` header, so without it any website the operator visits can drive `/api/vpn/stop` and friends from their browser.

### Log Files

- `vpn_logs/session_<timestamp>.log` — per-session output from `checkip.sh`; `vpn_logs/latest.log` symlinks to the current one
- `qbit.log` — qBittorrent stdout
- `/var/log/openvpn.log` — OpenVPN daemon log

## Testing

```bash
python3 -m pytest            # full suite
python3 -m flake8 --select=E9,F vpn_active.py organize.py webapp/*.py tests/
```

CI runs both on push and PR to `master` (`.github/workflows/ci.yml`, Python 3.9).

Note that the shell enforcement layer (`checkip.sh`, `ufw_*.sh`) has no automated tests — changes there need manual verification on the Pi.

## Dependencies

- `python3` with `requests`, `flask`, `python-dotenv`
- `openvpn`, `qbittorrent-nox` (or `deluged`)
- `ufw` — **required**; the kill switch depends on it
- Standard tools: `pgrep`, `ip`, `curl`, `ss`, `iptables`, `sudo`

## sudo Requirements (web app)

The pi user needs passwordless sudo for the commands below. Create `/etc/sudoers.d/vpn-webapp`:

```
# OpenVPN lifecycle
pi ALL=(ALL) NOPASSWD: /usr/sbin/openvpn
pi ALL=(ALL) NOPASSWD: /usr/bin/pkill
pi ALL=(ALL) NOPASSWD: /bin/cat /var/log/openvpn.log

# Config install
pi ALL=(ALL) NOPASSWD: /bin/mv
pi ALL=(ALL) NOPASSWD: /bin/rm
pi ALL=(ALL) NOPASSWD: /bin/chmod
pi ALL=(ALL) NOPASSWD: /bin/chown

# Kill switch — ufw for state changes, iptables to READ the live chain
pi ALL=(ALL) NOPASSWD: /usr/sbin/ufw
pi ALL=(ALL) NOPASSWD: /sbin/iptables
pi ALL=(ALL) NOPASSWD: /bin/bash /home/pi/VPN/ufw_killswitch.sh
pi ALL=(ALL) NOPASSWD: /bin/bash /home/pi/VPN/ufw_base.sh

# IPv6 disable/restore
pi ALL=(ALL) NOPASSWD: /sbin/sysctl

# DNS leak prevention
pi ALL=(ALL) NOPASSWD: /usr/bin/tee /etc/resolv.conf
pi ALL=(ALL) NOPASSWD: /usr/bin/chattr
pi ALL=(ALL) NOPASSWD: /bin/cp /etc/resolv.conf *
```

Adjust the two script paths to wherever the repo actually lives.

**Known weaknesses in this policy, if you are hardening it:** the unrestricted `rm`, `mv`, `pkill` and `sysctl` grants are each root-equivalent on their own (`sudo mv /tmp/x /etc/sudoers.d/x`). Granting `NOPASSWD` on the two scripts by absolute path is only safe while those files are not writable by `pi` — otherwise the web app's own file-move endpoints could rewrite the script that runs as root. A single root-owned wrapper with a fixed verb list would be the better design.
