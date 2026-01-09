# Installation Guide

## System Requirements

- Ubuntu/Debian-based Linux system
- Root/sudo access
- Internet connection
- OpenVPN compatible VPN provider

## Prerequisites

The following packages are required:
- `openvpn` - VPN client
- `qbittorrent-nox` - Headless BitTorrent client (or `deluge`)
- `python3` - Python runtime
- `python3-pip` - Python package manager
- `curl` - HTTP client
- `iptables` - Firewall management
- `screen` - Terminal multiplexer (optional)
- `ufw` - Uncomplicated Firewall (optional)

## Quick Installation

### 1. Clone the Repository

```bash
git clone https://github.com/StewartRogers/VPN.git
cd VPN
```

### 2. Install Dependencies

Run the startup script with software installation option:

```bash
chmod +x startvpn.sh stopvpn.sh checkip.sh vpn_status.sh
./startvpn.sh
```

When prompted, choose 'y' to install required software.

Or install manually:

```bash
sudo apt-get update
sudo apt-get install -y openvpn qbittorrent-nox python3 python3-pip curl iptables screen ufw
pip3 install --user requests
```

### 3. Obtain VPN Configuration File

You need a `.ovpn` configuration file from your VPN provider:

1. Log into your VPN provider's website
2. Download an OpenVPN configuration file (`.ovpn`)
3. Either:
   - Place it in the project directory, OR
   - Provide the download URL when prompted by the script

### 4. First Run Configuration (Optional qBittorrent Setup)

If this is your first time running qBittorrent:

```bash
qbittorrent-nox
```

- Accept the legal disclaimer
- Note the default credentials (usually username: `admin`, default password shown)
- Press `Ctrl+C` to stop
- Access web UI at `http://localhost:8080` to change password

## Configuration

### Optional Configuration File

Create `~/.vpn_config.conf` or `./vpn_config.conf`:

```bash
# Monitoring intervals (seconds)
FAST_CHECK_INTERVAL=2
IP_CHECK_INTERVAL=10
MAX_RECONNECT_ATTEMPTS=3

# Network security settings
# Note: Killswitch disabled by default. Use UFW for firewall management.
# Set to true to enable iptables-based killswitch (may conflict with UFW)
SETUP_KILLSWITCH=false
PREVENT_DNS_LEAK=true
DISABLE_IPV6=true

# BitTorrent settings
BIND_TO_VPN_INTERFACE=true
TORRENT_CLIENT="qbittorrent-nox"  # or "deluge"

# Backup and log locations
BACKUP_DIR="/tmp/vpn_backups"
PID_DIR="/tmp/vpn_pids"
LOG_DIR="$HOME/.vpn_logs"
```

## Usage

### Interactive Mode (Default)

Start the VPN and BitTorrent client:

```bash
./startvpn.sh
```

Follow the prompts to:
1. Optionally install/update software
2. Configure firewall rules
3. Download or select VPN config file
4. Confirm VPN connection
5. Start monitoring

### Non-Interactive Mode (Automation)

```bash
./startvpn.sh --non-interactive --ovpn-url https://example.com/config.ovpn
```

Options:
- `--non-interactive` - Run without prompts
- `--config FILE` - Use specific config file
- `--ovpn-url URL` - Download OVPN from URL
- `--no-killswitch` - Skip killswitch setup
- `--help` - Show help

### Check Status

```bash
./vpn_status.sh
```

### Stop Services

```bash
./stopvpn.sh
```

Options:
- Choose to shutdown services only
- Optionally rename and move video files

Quick shutdown:

```bash
./stopvpn.sh --shutdown-only
```

## Security Features

When VPN connects, the following security measures are automatically applied:

### 1. Network Kill Switch
- Blocks all non-VPN internet traffic
- Allows local network access (192.168.x.x, 10.x.x.x, 172.16.x.x)
- Allows SSH access
- **Automatically removed on shutdown**

### 2. DNS Leak Prevention
- Forces all DNS queries through VPN-safe resolvers
- Prevents ISP DNS snooping
- **Original DNS settings restored on shutdown**

### 3. IPv6 Leak Prevention
- Temporarily disables IPv6 during VPN session
- **IPv6 restored to original state on shutdown**

### 4. BitTorrent Interface Binding
- Configures qBittorrent to only use VPN interface (tun0)
- Prevents accidental torrenting on regular connection
- **Original config restored on shutdown**

### 5. Auto-Reconnect
- Automatically attempts to reconnect VPN on failure
- Configurable number of attempts (default: 3)
- Emergency shutdown if all attempts fail

## Verification

After starting, verify everything is working:

```bash
# Check status
./vpn_status.sh

# Check external IP (should be VPN IP)
curl https://api.ipify.org

# Check monitoring logs
tail -f checkvpn.log

# Check application logs
tail -f ~/.vpn_logs/vpn.log
```

## Troubleshooting

See [TROUBLESHOOTING.md](TROUBLESHOOTING.md) for common issues and solutions.

## Uninstallation

1. Stop all services:
   ```bash
   ./stopvpn.sh --shutdown-only
   ```

2. Remove installed packages (optional):
   ```bash
   sudo apt-get remove openvpn qbittorrent-nox
   ```

3. Remove configuration and logs:
   ```bash
   rm -rf ~/.vpn_config.conf ~/.vpn_logs /tmp/vpn_backups /tmp/vpn_pids
   ```

## Notes

- All security measures are **fully reversible** - your system returns to normal state when you stop the VPN
- Local network access is **always preserved** - you can access file shares, printers, and SSH while VPN is running
- The system is designed to be **non-intrusive** - you can use your computer normally while VPN is active
- Process management uses **PID files** where possible for clean shutdown

## Support

For issues, questions, or contributions:
- GitHub Issues: https://github.com/StewartRogers/VPN/issues
- Documentation: See TROUBLESHOOTING.md and ENHANCEMENTS.md
