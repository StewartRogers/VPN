# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

VPN monitoring and kill-switch system for a Raspberry Pi running OpenVPN + qBittorrent. The system continuously monitors VPN connectivity, detects IP leaks, and auto-reconnects or shuts down torrenting if the VPN is compromised.

## Running the Scripts

```bash
# Start VPN monitoring (pass your home/ISP IP as argument)
./checkip.sh <home_ip>

# Check current status of all services
sudo ufw status verbose        # kill switch: look for "deny (outgoing)"
ip route get 8.8.8.8           # should say "dev tun0"
curl -s https://api.ipify.org  # should NOT be your home IP

# One-shot VPN check (returns exit code 1=secure, 0=not secure)
python3 vpn_active.py <home_ip>
```

## Architecture

### Component Relationships

`checkip.sh` is the central monitoring daemon. It calls `vpn_active.py` for full IP checks and manages the qBittorrent process lifecycle directly.

**Two-tier monitoring loop in `checkip.sh`:**
- **Fast checks** (every `FAST_CHECK_INTERVAL` seconds, default 2s): bash-level checks for OpenVPN process (`pgrep`) and tun0 interface (`ip link`)
- **Full IP checks** (every `IP_CHECK_INTERVAL` seconds, default 10s): calls `vpn_active.py` which queries external IP services and compares against the home IP argument

**Failure response sequence:**
1. Immediately stop qBittorrent (PID file in `/tmp/vpn_pids/`, fallback to `pkill`)
2. Attempt auto-reconnect up to `MAX_RECONNECT_ATTEMPTS` (default 3) times
3. On reconnect success: restart qBittorrent, reset counters
4. On max failures: call `stopvpn.sh --shutdown-only` and exit

### Key Files

- `checkip.sh` - Main monitoring daemon; all orchestration logic lives here
- `vpn_active.py` - Python IP-leak detector; checks OpenVPN process, tun0 interface, and external IP via multiple fallback services (ipify, httpbin, api64.ipify)

There is no status script. `vpn_status.sh` was removed — nothing called it, and
it reported the kill switch by grepping `iptables -L OUTPUT`, which never
matches because UFW keeps its rules in `ufw-user-output`. The web dashboard's
status panel covers the same ground and reads live state.

### Configuration

Optional config file loaded at startup from `~/.vpn_config.conf` or `./vpn_config.conf`:

```bash
FAST_CHECK_INTERVAL=2       # seconds between process/interface checks
IP_CHECK_INTERVAL=10        # seconds between full IP leak checks
MAX_RECONNECT_ATTEMPTS=3    # reconnect tries before emergency shutdown
PID_DIR=/tmp/vpn_pids       # where qBittorrent PID file is stored
```

### VPN Reconnect Logic

`attempt_reconnect()` in `checkip.sh`: kills openvpn, finds the first `.ovpn` file in `/etc/openvpn/client/`, restarts it as a daemon, waits 15 seconds, then verifies with both process/interface checks and a full IP check before restarting qBittorrent.

### Log Files

- `checkvpn.log` - All stdout/stderr from `checkip.sh` (overwritten on each run)
- `qbit.log` - qBittorrent stdout when started by `checkip.sh`
- `/var/log/openvpn.log` - OpenVPN daemon log (written during reconnect)

## Dependencies

- `python3` with `requests` library
- `openvpn`, `qbittorrent-nox` (or `deluged`)
- Standard tools: `pgrep`, `ip`, `curl`, `ss`, `ufw`, `sudo`

The firewall is **UFW exclusively**. There are no `iptables` calls in this
project — `ufw_base.sh` and `ufw_killswitch.sh` are the only things that touch
the firewall.

## sudo Requirements (web app)

The pi user needs passwordless sudo for all of the following. Create `/etc/sudoers.d/vpn-webapp`:

```
# OpenVPN and process control
pi ALL=(ALL) NOPASSWD: /usr/sbin/openvpn
pi ALL=(ALL) NOPASSWD: /usr/bin/pkill
pi ALL=(ALL) NOPASSWD: /bin/mv
pi ALL=(ALL) NOPASSWD: /bin/rm
pi ALL=(ALL) NOPASSWD: /bin/chmod
pi ALL=(ALL) NOPASSWD: /bin/chown
pi ALL=(ALL) NOPASSWD: /bin/cat /var/log/openvpn.log
pi ALL=(ALL) NOPASSWD: /bin/cat /etc/openvpn/client/*.ovpn

# Kill switch. Both entries are required:
#   ufw          - check_killswitch_active() runs 'sudo ufw status verbose'
#   bash <script> - setup_killswitch()/teardown_killswitch() run the ufw_*.sh
#                   scripts, which must be root to change firewall rules
pi ALL=(ALL) NOPASSWD: /usr/sbin/ufw
pi ALL=(ALL) NOPASSWD: /bin/bash /home/pi/VPN/ufw_base.sh
pi ALL=(ALL) NOPASSWD: /bin/bash /home/pi/VPN/ufw_killswitch.sh

# IPv6 disable/restore
pi ALL=(ALL) NOPASSWD: /sbin/sysctl

# DNS leak prevention
pi ALL=(ALL) NOPASSWD: /usr/bin/tee /etc/resolv.conf
pi ALL=(ALL) NOPASSWD: /usr/bin/chattr
pi ALL=(ALL) NOPASSWD: /bin/cp /etc/resolv.conf *
```

Adjust the two `bash` paths to wherever this repo is checked out. Without the
`ufw` and `bash` entries the web app cannot apply the kill switch:
`setup_killswitch()` fails, `check_killswitch_active()` returns `False`, and
`checkip.sh` refuses to start.

Verify with:

```bash
sudo -n ufw status verbose >/dev/null && echo "ufw: ok" || echo "ufw: MISSING"
sudo -n bash /home/pi/VPN/ufw_base.sh --help >/dev/null 2>&1 \
  && echo "scripts: ok" || echo "scripts: check the bash paths above"
```
