# VPN BitTorrent Scripts

This repository contains Bash and Python scripts to manage a secure VPN-enforced BitTorrent setup with continuous monitoring and automatic security measures.

## Features

### 🔒 Security
- **Network Kill Switch** - Blocks all non-VPN traffic while preserving local network access
- **DNS Leak Prevention** - Forces all DNS queries through VPN resolvers
- **IPv6 Leak Prevention** - Temporarily disables IPv6 during VPN session
- **BitTorrent Interface Binding** - Ensures torrent traffic only uses VPN interface
- **Reversible on clean shutdown** - `stopvpn.sh` / Stop VPN restore the system. Note that after a *failure* the kill switch is deliberately left up so nothing leaks — run `./remove_killswitch.sh` to restore network access

### 🔄 Reliability
- **Fail-closed monitoring** - On VPN failure or IP leak, torrenting stops and the kill switch stays up. There is no automatic reconnect; use `./startvpn.sh` or the web UI's Force Reconnect
- **Continuous Monitoring** - Fast process/interface checks + periodic IP verification
- **PID-Based Process Management** - Clean, reliable service shutdown
- **Structured Logging** - Detailed logs with automatic rotation

### 🛠️ Usability
- **Interactive & Non-Interactive Modes** - Manual or automated operation
- **Configuration File Support** - Customize all settings via config file
- **Status Dashboard** - Quick status check script (`vpn_status.sh`)
- **Comprehensive Documentation** - Installation, troubleshooting, and enhancement guides

## Quick Start

### Installation

```bash
git clone https://github.com/StewartRogers/VPN.git
cd VPN
chmod +x *.sh
./startvpn.sh
```

Follow the prompts to install dependencies and configure VPN.

### Basic Usage

```bash
# Start VPN and BitTorrent client
./startvpn.sh

# Check status
./vpn_status.sh

# Stop everything
./stopvpn.sh
```

### Non-Interactive Mode

```bash
# Automated startup
./startvpn.sh --non-interactive --ovpn-url https://example.com/config.ovpn

# Quick shutdown
./stopvpn.sh --shutdown-only
```

## Scripts

- **startvpn.sh** - Start VPN and qbittorrent-nox with security measures
- **stopvpn.sh** - Stop services, restore system settings, optionally manage files
- **checkip.sh** - Continuous VPN monitoring; stops torrenting and exits on failure
- **vpn_active.py** - VPN verification (process, interface, IP check)
- **vpn_status.sh** - Display current status of all services
- **vpn_config.conf** - Configuration file for customizing behavior

## Web App

A browser-based dashboard is available as an alternative to the CLI scripts.

### Start the web app

```bash
cd VPN
./start_web.sh
# Open http://<pi-ip>:5000
```

`start_web.sh` loads `webapp/.env`, checks your Python version and dependencies,
and prints the URL it is serving on. Stop it with `./stop_web.sh`.

Install Python dependencies first if needed:

```bash
pip install -r webapp/requirements.txt
```

### API authentication

**The API is never served unauthenticated.** On first start with no
`VPN_API_TOKEN` set, the app generates one, appends it to `webapp/.env`
(mode 0600), and prints it once:

```
No VPN_API_TOKEN was set, so one was generated.
Token:    xY3k...
Saved to: /home/pi/VPN/webapp/.env
```

Paste that into the web UI once — it is stored in your browser and the token
persists across restarts. All API requests must include
`Authorization: Bearer <token>`.

To set your own instead, put it in `webapp/.env` before first start:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

The token matters even on a trusted LAN, because it is what prevents CSRF: a
cross-origin HTML form cannot set an `Authorization` header, so without a token
any website you visit could drive `/api/vpn/stop` from your browser and tear
down the kill switch while torrents are running.

### Configuration (`webapp/.env`)

Copy `webapp/.env.example` to `webapp/.env`. All values are optional:

| Variable | Default | Purpose |
|---|---|---|
| `VPN_API_TOKEN` | auto-generated | Bearer token for the API |
| `HOME_IP` | auto-detected | Pre-VPN ISP IP to compare against |
| `BIND_HOST` | `0.0.0.0` | Interface to bind; `127.0.0.1` for local only |
| `PORT` | `5000` | Listen port — re-run `sudo bash ufw_base.sh` after changing |
| `ORGANIZER_ROOTS` | `$HOME` | Comma-separated dirs the file organizer may touch |
| `ACCESS_LOG` | off | Set to `1` to log every request (includes the SSE token) |

### Workflow

1. **Page load** — home IP is auto-detected and the monitor is auto-configured
2. **Step 1 — VPN** — paste a `.ovpn` URL to download a config, then click Start VPN
3. **Step 2 — Monitor** — start the monitoring daemon (watches for IP leaks; stops everything if one is found)
4. **Step 3 — qBittorrent** — start qBittorrent (button is disabled until monitor is running and VPN is secure)

Live OpenVPN logs stream directly in the dashboard. The organizer tab (`/organizer`) provides a file-rename and move tool for downloaded video files.

### sudo requirements for the web app

The `pi` user needs passwordless sudo for several system operations. See [INSTALL.md](INSTALL.md) and [CLAUDE.md](CLAUDE.md) for the full sudoers template.

## Documentation

- **[INSTALL.md](INSTALL.md)** - Detailed installation and setup guide
- **[TROUBLESHOOTING.md](TROUBLESHOOTING.md)** - Common issues and solutions
- **[ENHANCEMENTS.md](ENHANCEMENTS.md)** - Technical implementation details

## System Requirements

- Ubuntu/Debian-based Linux
- OpenVPN
- qbittorrent-nox (or deluge)
- Python 3
- Root/sudo access

## Configuration

Create `~/.vpn_config.conf` or `./vpn_config.conf`:

```bash
# Monitoring
FAST_CHECK_INTERVAL=2
IP_CHECK_INTERVAL=10

# Security
# Note: Killswitch disabled by default. Use UFW for firewall management.
SETUP_KILLSWITCH=false
PREVENT_DNS_LEAK=true
DISABLE_IPV6=true
BIND_TO_VPN_INTERFACE=true
```

## Design Principles

✅ **Reversible** - All changes automatically reverted on shutdown  
✅ **Non-Intrusive** - Local network and SSH access always preserved  
✅ **User-Friendly** - Works for both manual and automated workflows  
✅ **Fail-Safe** - Emergency shutdown if VPN cannot be secured  

## License

MIT License - See [LICENSE](LICENSE) for details

## Contributing

Issues and pull requests welcome! See [ENHANCEMENTS.md](ENHANCEMENTS.md) for implementation details.

## Author

Stewart Rogers

Copyright (c) 2022-2025
