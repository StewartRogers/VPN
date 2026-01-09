# VPN BitTorrent Scripts

This repository contains Bash and Python scripts to manage a secure VPN-enforced BitTorrent setup with continuous monitoring and automatic security measures.

## Features

### 🔒 Security
- **Network Kill Switch** - Blocks all non-VPN traffic while preserving local network access
- **DNS Leak Prevention** - Forces all DNS queries through VPN resolvers
- **IPv6 Leak Prevention** - Temporarily disables IPv6 during VPN session
- **BitTorrent Interface Binding** - Ensures torrent traffic only uses VPN interface
- **Fully Reversible** - All security measures automatically removed on shutdown

### 🔄 Reliability
- **Auto-Reconnect** - Automatically reconnects VPN on failure (configurable attempts)
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
- **checkip.sh** - Continuous VPN monitoring with auto-reconnect
- **vpn_active.py** - VPN verification (process, interface, IP check)
- **vpn_status.sh** - Display current status of all services
- **vpn_config.conf** - Configuration file for customizing behavior

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
MAX_RECONNECT_ATTEMPTS=3

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
