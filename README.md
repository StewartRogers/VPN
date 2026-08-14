# VPN BitTorrent Scripts

Bash and Python tooling to run qBittorrent behind an OpenVPN tunnel on a
Raspberry Pi, with a UFW kill switch and a monitor that shuts torrenting down
the moment the tunnel stops carrying traffic.

There are two front ends over the same machinery:

- **CLI** — `startvpn.sh` brings up the tunnel and hands off to `checkip.sh`
- **Web** — `start_web.sh` serves a dashboard at `http://<pi-ip>:5000`

## How it protects you

- **UFW kill switch** — outgoing traffic defaults to `deny`; only the tunnel,
  the VPN server's endpoint, and your LAN are allowed out. Applied *before*
  OpenVPN starts and left up if anything fails, so a drop is a blackout rather
  than a leak.
- **DNS leak prevention** — `/etc/resolv.conf` is replaced with Cloudflare
  resolvers and locked with `chattr +i` (web path).
- **IPv6 leak prevention** — IPv6 is disabled at the kernel level before
  OpenVPN starts, and `ufw_base.sh` forces `IPV6=yes` in `/etc/default/ufw` so
  the firewall rules cover IPv6 too.
- **Interface binding** — qBittorrent is bound to `tun0` by name *and* by live
  IP, so it cannot fall back to the physical NIC.
- **Verified start** — torrenting only begins once the tunnel exists, carries
  the default route, and exits on an IP that is not your home IP.

### Failure behaviour: fail-stop, not auto-reconnect

Neither monitor reconnects on its own. On VPN process death, `tun0` going away,
the default route leaving the tunnel, a new global IPv6 address, an exit IP
matching your home IP, or three consecutive failed IP lookups, the monitor
**stops qBittorrent and exits with the kill switch still active**. The web
monitor also stops OpenVPN. You are offline until you intervene — deliberately,
because a silent reconnect loop is exactly when leaks happen.

The web UI has a **Force Reconnect** button for a manual retry. There is no
`MAX_RECONNECT_ATTEMPTS` setting; retries at *startup* are governed by
`MAX_STARTUP_ATTEMPTS`.

## Quick start

```bash
git clone https://github.com/StewartRogers/VPN.git
cd VPN
chmod +x *.sh
./startvpn.sh
```

Follow the prompts to install dependencies and select or download a `.ovpn`
config. See [INSTALL.md](INSTALL.md) for the full walkthrough.

### CLI usage

```bash
./startvpn.sh                    # interactive: config, kill switch, VPN, monitor
./stopvpn.sh                     # stop everything, restore base state
./stopvpn.sh --shutdown-only     # non-interactive: tear down, no prompts
```

Non-interactive:

```bash
./startvpn.sh --non-interactive --ovpn-url https://example.com/config.ovpn
```

Flags: `--non-interactive`, `--ovpn-url URL`, `--no-killswitch`, `--help`.
`--no-killswitch` starts the VPN but **not** the monitor — `checkip.sh` refuses
to run without an active kill switch.

### Checking state by hand

```bash
sudo ufw status verbose        # kill switch: look for "deny (outgoing)"
ip route get 8.8.8.8           # should say "dev tun0"
curl -s https://api.ipify.org  # should NOT be your home IP
pgrep -x openvpn && pgrep -f qbittorrent-nox
```

## Web app

```bash
pip3 install -r webapp/requirements.txt
./start_web.sh
# open http://<pi-ip>:5000
```

`start_web.sh` reads `webapp/.env` and `vpn_config.conf`, then honours these
environment variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| `VPN_API_TOKEN` | unset | Require `Authorization: Bearer <token>` on every API call |
| `BIND_HOST` | `0.0.0.0` | Interface to bind; set to your LAN IP to narrow exposure |
| `PORT` | `5000` | Listen port |
| `HOME_IP` | unset | Pre-configure the monitor's home IP at startup |
| `ACCESS_LOG` | off | Set to `1` to log every HTTP request |

Unauthenticated by default — fine on a trusted LAN, but set a token if the box
is reachable from anywhere else:

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"   # generate one
VPN_API_TOKEN=<token> ./start_web.sh
```

### Workflow

Each step is a separate, deliberate click. Starting the VPN does **not** start
anything else.

1. **Step 1 — VPN Config** — download a `.ovpn` by URL, or upload one
2. **Step 2 — VPN** — apply the kill switch and bring up the tunnel
3. **Step 3 — Monitor** — start the leak monitor
4. **Step 4 — qBittorrent** — start the client

The Step 4 button is disabled until the monitor is running and the tunnel is
verified, and `POST /api/qbt/start` enforces the same conditions server-side —
so a stale tab or a stray `curl` cannot start torrents early. Live OpenVPN logs
stream into the dashboard. **Stop All** tears down in reverse order —
qBittorrent, monitor, OpenVPN, then the kill switch — and each process is
confirmed stopped before the next step; if qBittorrent will not die, the kill
switch stays up rather than opening the ISP link under a live client.

`/organizer` is a separate tab for renaming and moving downloaded video files.

Stop the web app with `bash stop_web.sh`, which performs the same ordered
teardown as `stopvpn.sh` (torrents → OpenVPN → app → DNS/IPv6 → UFW last).

## Files

| File | Purpose |
| --- | --- |
| `startvpn.sh` | Interactive/automated startup; hands off to `checkip.sh` |
| `checkip.sh` | Shell monitor — fast process/interface checks + periodic IP checks |
| `stopvpn.sh` | Ordered teardown and system restore |
| `vpn_active.py` | One-shot leak check used by `checkip.sh` |
| `qbt_config.py` | Applies the tun0 bind, save path and download limit to qBittorrent's config (both paths) |
| `ufw_base.sh` | Base UFW state; `UFW_OUT_POLICY` selects the outgoing default |
| `ufw_killswitch.sh` | Kill switch — calls `ufw_base.sh deny`, then allows the tunnel and LAN |
| `remove_killswitch.sh` | Emergency recovery if the kill switch locks you out |
| `start_web.sh` / `stop_web.sh` | Web app lifecycle |
| `webapp/` | Flask dashboard (`app.py`, `monitor.py`, `organizer.py`) |
| `vpn_config.conf` | Optional settings, also readable from `~/.vpn_config.conf` |

The firewall is **UFW exclusively** — `ufw_base.sh` and `ufw_killswitch.sh` are
the only things that touch it. There are no `iptables` calls in this project.

## Configuration

`~/.vpn_config.conf` takes precedence over `./vpn_config.conf`. Every key below
is read by something; anything else in the file is inert.

```bash
FAST_CHECK_INTERVAL=2        # seconds between process/interface/route checks
IP_CHECK_INTERVAL=10         # seconds between external-IP leak checks
MAX_STARTUP_ATTEMPTS=3       # connection attempts before giving up (both paths)
MAX_SESSIONS=20              # session logs kept in LOG_DIR

VPN_CLIENT_HOME="/etc/openvpn/client/"
VPN_LOG_FILE="/var/log/openvpn.log"
PID_DIR="/tmp/vpn_pids"
LOG_DIR="$SCRIPT_DIR/vpn_logs"
BACKUP_DIR="$HOME/.vpn_backups"

QBT_SAVE_PATH="/mnt/hdddisk/"   # blank = qBittorrent's own default
QBT_MAX_ACTIVE_DOWNLOADS=5      # torrents downloading at once; unset = leave
                                # qBittorrent's own queue settings alone

# LAN ranges the kill switch allows out on the physical NIC.
# Default is all of RFC1918; narrow it if you want.
# LAN_CIDRS="10.0.0.0/8 172.16.0.0/12 192.168.0.0/16"
```

## Logs

| Path | Contents |
| --- | --- |
| `vpn_logs/session_<timestamp>.log` | One `checkip.sh` run; `vpn_logs/latest.log` symlinks the newest |
| `vpn_logs/vpn.log` | `startvpn.sh` / `stopvpn.sh` events, rotated to `vpn.log.1` |
| `/var/log/openvpn.log` | OpenVPN daemon |
| `qbit.log` | qBittorrent stdout |

The web app keeps its log in memory and streams it to the browser.

## Requirements

Debian/Ubuntu (developed on Raspberry Pi OS), `openvpn`, `qbittorrent-nox`,
`ufw`, `python3` (3.9+) with `requests` and `flask`, plus `curl`, `pgrep`, `ip`,
and `ss`. The web app needs passwordless sudo for several operations — see
[INSTALL.md](INSTALL.md).

## Tests

```bash
python3 -m pytest -q
```

68 tests covering the monitor loop, the kill-switch state machine, leak
detection, the SSRF guard on `.ovpn` downloads, and the torrent start gate. CI
runs `flake8 --select=E9,F` and pytest on every push and PR to `master`.

## Documentation

- **[INSTALL.md](INSTALL.md)** — installation, sudoers, first run
- **[TROUBLESHOOTING.md](TROUBLESHOOTING.md)** — diagnosis and recovery
- **[ENHANCEMENTS.md](ENHANCEMENTS.md)** — what is implemented, and why
- **[TODO.md](TODO.md)** — open items and known gaps
- **[CLAUDE.md](CLAUDE.md)** — orientation for AI-assisted work

## License

MIT — see [LICENSE](LICENSE).

## Author

Stewart Rogers · Copyright (c) 2022-2026
