# VPN BitTorrent Project - Enhancement Recommendations

## Design Constraints

Based on user requirements:
1. ✅ **Reversible Changes**: All security measures must be cleanly removed on intentional shutdown
2. ✅ **Preserve Local Network**: Must not block local IP ranges or SSH access
3. ✅ **System Usability**: Computer must remain fully usable during VPN/torrent operation

---

## 🔴 Priority 1: Critical Security Enhancements

### 1.1 Network Kill Switch (with Local Network Exception)
**Problem**: If VPN drops, BitTorrent traffic could leak through regular internet connection.

**Solution**: Implement iptables rules that:
- Allow all local network traffic (192.168.0.0/16, 10.0.0.0/8, 172.16.0.0/12)
- Allow established connections
- Allow SSH (port 22) from local networks
- Block all other outbound traffic except through VPN (tun0)
- **Cleanup**: All rules removed automatically on shutdown

**Implementation**:
```bash
# In startvpn.sh - after VPN connects
setup_killswitch() {
    # Save existing rules for restoration
    sudo iptables-save > /tmp/iptables.backup
    
    # Allow loopback
    sudo iptables -A OUTPUT -o lo -j ACCEPT
    
    # Allow local network traffic
    sudo iptables -A OUTPUT -d 192.168.0.0/16 -j ACCEPT
    sudo iptables -A OUTPUT -d 10.0.0.0/8 -j ACCEPT
    sudo iptables -A OUTPUT -d 172.16.0.0/12 -j ACCEPT
    
    # Allow established connections
    sudo iptables -A OUTPUT -m state --state ESTABLISHED,RELATED -j ACCEPT
    
    # Allow VPN traffic
    sudo iptables -A OUTPUT -o tun+ -j ACCEPT
    
    # Allow VPN connection itself (to VPN server)
    VPN_SERVER=$(grep "^remote " /etc/openvpn/client/*.ovpn | head -1 | awk '{print $2}')
    VPN_PORT=$(grep "^remote " /etc/openvpn/client/*.ovpn | head -1 | awk '{print $3}')
    sudo iptables -A OUTPUT -d $VPN_SERVER -p udp --dport $VPN_PORT -j ACCEPT
    
    # Drop everything else
    sudo iptables -A OUTPUT -j DROP
}

# In stopvpn.sh - restore original rules
cleanup_killswitch() {
    if [ -f /tmp/iptables.backup ]; then
        sudo iptables-restore < /tmp/iptables.backup
        rm /tmp/iptables.backup
    fi
}
```

**Benefits**:
- Prevents IP leaks if VPN disconnects
- Preserves local network access (file shares, printers, SSH)
- Fully reversible on shutdown

---

### 1.2 DNS Leak Prevention (Reversible)
**Problem**: DNS queries might leak through ISP DNS servers instead of VPN DNS.

**Solution**:
- Backup original `/etc/resolv.conf`
- Configure system to use VPN DNS servers only during operation
- Restore original DNS settings on shutdown

**Implementation**:
```bash
# In startvpn.sh
setup_dns() {
    sudo cp /etc/resolv.conf /etc/resolv.conf.backup
    echo "nameserver 1.1.1.1" | sudo tee /etc/resolv.conf
    echo "nameserver 1.0.0.1" | sudo tee -a /etc/resolv.conf
    sudo chattr +i /etc/resolv.conf  # Prevent changes
}

# In stopvpn.sh
cleanup_dns() {
    sudo chattr -i /etc/resolv.conf
    if [ -f /etc/resolv.conf.backup ]; then
        sudo mv /etc/resolv.conf.backup /etc/resolv.conf
    fi
}
```

---

### 1.3 IPv6 Leak Prevention (Temporary)
**Problem**: IPv6 traffic might bypass VPN tunnel.

**Solution**: Temporarily disable IPv6 during VPN session, restore on shutdown.

**Implementation**:
```bash
# In startvpn.sh
disable_ipv6() {
    # Save current state
    CURRENT_IPV6=$(sysctl net.ipv6.conf.all.disable_ipv6 | awk '{print $3}')
    echo $CURRENT_IPV6 > /tmp/ipv6.backup
    
    # Disable IPv6
    sudo sysctl -w net.ipv6.conf.all.disable_ipv6=1
    sudo sysctl -w net.ipv6.conf.default.disable_ipv6=1
}

# In stopvpn.sh
restore_ipv6() {
    if [ -f /tmp/ipv6.backup ]; then
        ORIGINAL=$(cat /tmp/ipv6.backup)
        sudo sysctl -w net.ipv6.conf.all.disable_ipv6=$ORIGINAL
        sudo sysctl -w net.ipv6.conf.default.disable_ipv6=$ORIGINAL
        rm /tmp/ipv6.backup
    fi
}
```

---

### 1.4 BitTorrent Interface Binding (Application-Level)
**Problem**: qbittorrent-nox might send traffic through non-VPN interface.

**Solution**: Configure qbittorrent-nox to bind exclusively to tun0 interface.

**Implementation**:
```bash
# In startvpn.sh - before starting qbittorrent
configure_qbittorrent_binding() {
    CONFIG_FILE="$HOME/.config/qBittorrent/qBittorrent.conf"
    
    # Ensure config directory exists
    mkdir -p "$(dirname "$CONFIG_FILE")"
    
    # Create or update config to bind to tun0
    if [ -f "$CONFIG_FILE" ]; then
        # Backup existing config
        cp "$CONFIG_FILE" "${CONFIG_FILE}.backup"
    fi
    
    # Set interface binding
    crudini --set "$CONFIG_FILE" "Preferences" "Connection\\InterfaceName" "tun0"
    crudini --set "$CONFIG_FILE" "Preferences" "Connection\\InterfaceAddress" "0.0.0.0"
    
    # Or use sed if crudini not available
    # This ensures qbittorrent only uses tun0
}

# In stopvpn.sh - restore original config
restore_qbittorrent_config() {
    CONFIG_FILE="$HOME/.config/qBittorrent/qBittorrent.conf"
    if [ -f "${CONFIG_FILE}.backup" ]; then
        mv "${CONFIG_FILE}.backup" "$CONFIG_FILE"
    fi
}
```

**Alternative**: Manual configuration through qbittorrent web UI:
1. Access web UI at http://localhost:8080
2. Go to Settings → Advanced → Network Interface
3. Select "tun0" from dropdown
4. Save settings

---

## 🟡 Priority 2: Reliability Improvements

### 2.1 Auto-Reconnect Mechanism
**Problem**: Currently, VPN failure causes full shutdown. Better to attempt reconnection.

**Solution**: Modify checkip.sh to attempt VPN reconnection before giving up.

**Implementation**:
```bash
# In checkip.sh
attempt_reconnect() {
    echo "$(date): Attempting VPN reconnection..."
    
    # Stop existing VPN
    sudo pkill -f openvpn
    sleep 2
    
    # Restart VPN
    XCONFIGFILE=$(ls /etc/openvpn/client/*.ovpn | head -1)
    sudo openvpn --config $XCONFIGFILE --log /var/log/openvpn.log --daemon
    
    # Wait for connection
    sleep 10
    
    # Check if successful
    if check_openvpn_process && check_vpn_interface; then
        echo "$(date): Reconnection successful"
        return 0
    else
        echo "$(date): Reconnection failed"
        return 1
    fi
}

# In main loop - before shutdown
MAX_RECONNECT_ATTEMPTS=3
reconnect_count=0

while [[ "$active" == "secure" ]]; do
    # ... existing checks ...
    
    if ! check_openvpn_process || ! check_vpn_interface; then
        if [ $reconnect_count -lt $MAX_RECONNECT_ATTEMPTS ]; then
            reconnect_count=$((reconnect_count + 1))
            echo "$(date): VPN failure detected - reconnect attempt $reconnect_count/$MAX_RECONNECT_ATTEMPTS"
            
            if attempt_reconnect; then
                reconnect_count=0  # Reset counter on success
                continue
            fi
        else
            echo "$(date): Max reconnection attempts reached - shutting down"
            active="notsecure"
            break
        fi
    fi
done
```

---

### 2.2 Configuration File
**Problem**: Hard-coded paths and settings make customization difficult.

**Solution**: Create optional configuration file for user preferences.

**Implementation**:
```bash
# Create vpn_config.conf
cat > vpn_config.conf << 'EOF'
# VPN Configuration File

# Monitoring intervals (seconds)
FAST_CHECK_INTERVAL=2
IP_CHECK_INTERVAL=10
MAX_RECONNECT_ATTEMPTS=3

# Paths
VPN_HOME="/etc/openvpn/"
VPN_CLIENT_HOME="/etc/openvpn/client/"
VPN_LOG_FILE="/var/log/openvpn.log"

# Network settings
ALLOW_LOCAL_NETWORK=true
SETUP_KILLSWITCH=true
PREVENT_DNS_LEAK=true
DISABLE_IPV6=true

# BitTorrent settings
BIND_TO_VPN_INTERFACE=true
TORRENT_CLIENT="qbittorrent-nox"  # or "deluge"

# File management
DEFAULT_VIDEO_DEST="/media/videos"
EOF

# In scripts - source config if exists
if [ -f "$HOME/.vpn_config.conf" ]; then
    source "$HOME/.vpn_config.conf"
elif [ -f "./vpn_config.conf" ]; then
    source "./vpn_config.conf"
fi
```

---

### 2.3 Enhanced Logging
**Problem**: Current logging is basic and logs grow unbounded.

**Solution**: Add structured logging with automatic rotation.

**Implementation**:
```bash
# Add to all scripts
LOG_DIR="$HOME/.vpn_logs"
mkdir -p "$LOG_DIR"

log_message() {
    local level=$1
    local message=$2
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [$level] $message" | tee -a "$LOG_DIR/vpn.log"
}

# Log rotation function
rotate_logs() {
    if [ -f "$LOG_DIR/vpn.log" ]; then
        local size=$(stat -f%z "$LOG_DIR/vpn.log" 2>/dev/null || stat -c%s "$LOG_DIR/vpn.log")
        if [ $size -gt 10485760 ]; then  # 10MB
            mv "$LOG_DIR/vpn.log" "$LOG_DIR/vpn.log.1"
            [ -f "$LOG_DIR/vpn.log.1" ] && gzip "$LOG_DIR/vpn.log.1"
        fi
    fi
}

# Usage
log_message "INFO" "Starting VPN connection"
log_message "ERROR" "VPN connection failed"
log_message "WARN" "IP leak detected"
```

---

## 🟢 Priority 3: Operational Enhancements

### 3.1 Non-Interactive Mode
**Problem**: Scripts require user interaction, preventing automation.

**Solution**: Add command-line arguments for unattended operation.

**Implementation**:
```bash
# In startvpn.sh - add argument parsing
usage() {
    echo "Usage: $0 [OPTIONS]"
    echo "Options:"
    echo "  --non-interactive    Run without prompts"
    echo "  --config FILE        Use specific config file"
    echo "  --ovpn-url URL       Download OVPN from URL"
    echo "  --no-killswitch      Skip killswitch setup"
    echo "  --help               Show this help"
}

NON_INTERACTIVE=false
while [[ $# -gt 0 ]]; do
    case $1 in
        --non-interactive)
            NON_INTERACTIVE=true
            shift
            ;;
        --config)
            CONFIG_FILE="$2"
            shift 2
            ;;
        --ovpn-url)
            OVPN_URL="$2"
            shift 2
            ;;
        --help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            usage
            exit 1
            ;;
    esac
done
```

---

### 3.2 Status Check Script
**Problem**: No easy way to check current VPN/torrent status.

**Solution**: Create simple status script.

**Implementation**:
```bash
# Create vpn_status.sh
#!/bin/bash

echo "==================================="
echo "VPN & BitTorrent Status"
echo "==================================="
echo ""

# Check OpenVPN
if pgrep -f openvpn > /dev/null; then
    echo "✓ OpenVPN: Running"
    VPN_IP=$(curl -s https://api.ipify.org)
    echo "  External IP: $VPN_IP"
else
    echo "✗ OpenVPN: Not running"
fi

# Check VPN interface
if ip link show tun0 &>/dev/null; then
    echo "✓ VPN Interface: Up"
else
    echo "✗ VPN Interface: Down"
fi

# Check qbittorrent
if pgrep -f qbittorrent-nox > /dev/null; then
    echo "✓ qBittorrent: Running"
    # Check binding
    BINDING=$(netstat -tulpn 2>/dev/null | grep qbittorrent | head -1)
    echo "  Listening on: $BINDING"
else
    echo "✗ qBittorrent: Not running"
fi

# Check monitoring script
if pgrep -f checkip.sh > /dev/null; then
    echo "✓ VPN Monitor: Running"
    if [ -f "./checkvpn.log" ]; then
        echo "  Last check: $(tail -1 checkvpn.log)"
    fi
else
    echo "✗ VPN Monitor: Not running"
fi

echo ""
echo "==================================="
```

---

### 3.3 Systemd Service Files (Optional)
**Problem**: Manual script management, no auto-start on boot.

**Solution**: Create systemd service files for automatic management.

**Implementation**:
```ini
# /etc/systemd/system/vpn-torrent.service
[Unit]
Description=VPN-Protected BitTorrent Service
After=network.target

[Service]
Type=forking
User=YOUR_USERNAME
ExecStart=/path/to/startvpn.sh --non-interactive
ExecStop=/path/to/stopvpn.sh --shutdown-only
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

---

## 🔵 Priority 4: Code Quality Improvements

### 4.1 Input Validation
**Problem**: Limited validation of user inputs and file paths.

**Solution**: Add comprehensive validation functions.

**Implementation**:
```bash
validate_url() {
    local url=$1
    if [[ ! "$url" =~ ^https?:// ]]; then
        echo "Error: Invalid URL format"
        return 1
    fi
    return 0
}

validate_directory() {
    local dir=$1
    if [ ! -d "$dir" ]; then
        echo "Error: Directory does not exist: $dir"
        return 1
    fi
    if [ ! -r "$dir" ]; then
        echo "Error: Directory not readable: $dir"
        return 1
    fi
    return 0
}

validate_ip() {
    local ip=$1
    if [[ ! "$ip" =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$ ]]; then
        echo "Error: Invalid IP address format"
        return 1
    fi
    return 0
}
```

---

### 4.2 Error Handling
**Problem**: Limited error handling in scripts.

**Solution**: Add trap handlers and error checks.

**Implementation**:
```bash
# Add to beginning of scripts
set -euo pipefail  # Exit on error, undefined vars, pipe failures

# Cleanup on exit
cleanup_on_exit() {
    local exit_code=$?
    if [ $exit_code -ne 0 ]; then
        log_message "ERROR" "Script exited with error code $exit_code"
        # Perform cleanup
        cleanup_killswitch
        cleanup_dns
        restore_ipv6
    fi
}

trap cleanup_on_exit EXIT
trap 'log_message "ERROR" "Script interrupted"; exit 130' INT TERM
```

---

### 4.3 PID-Based Process Management
**Problem**: Using `pkill -f` with process names can affect wrong processes.

**Solution**: Store PIDs and use them for process management.

**Implementation**:
```bash
PID_DIR="/tmp/vpn_pids"
mkdir -p "$PID_DIR"

# When starting services
start_qbittorrent() {
    qbittorrent-nox &
    echo $! > "$PID_DIR/qbittorrent.pid"
}

start_checkip() {
    ./checkip.sh $YHOMEIP &
    echo $! > "$PID_DIR/checkip.pid"
}

# When stopping services
stop_service() {
    local service=$1
    local pid_file="$PID_DIR/${service}.pid"
    
    if [ -f "$pid_file" ]; then
        local pid=$(cat "$pid_file")
        if kill -0 $pid 2>/dev/null; then
            kill $pid
            rm "$pid_file"
            echo "Stopped $service (PID: $pid)"
        else
            echo "$service not running (stale PID file)"
            rm "$pid_file"
        fi
    else
        echo "No PID file found for $service"
    fi
}
```

---

## 📚 Priority 5: Documentation

### 5.1 Installation Guide
Create INSTALL.md with:
- System requirements
- Dependency installation
- First-time setup steps
- Configuration options

### 5.2 Troubleshooting Guide
Create TROUBLESHOOTING.md with:
- VPN won't connect
- BitTorrent not starting
- IP leak detection
- Common error messages

### 5.3 Architecture Overview
Create ARCHITECTURE.md with:
- Component diagram
- Data flow
- Security model
- Process lifecycle

---

## Implementation Roadmap

### Phase 1: Critical Security (Week 1)
1. Implement kill switch with local network exceptions
2. Add DNS leak prevention
3. Configure BitTorrent interface binding
4. Test all security measures
5. Verify clean shutdown/restoration

### Phase 2: Reliability (Week 2)
1. Add auto-reconnect logic
2. Create configuration file
3. Implement enhanced logging
4. Add status check script

### Phase 3: Usability (Week 3)
1. Add non-interactive mode
2. Improve error handling
3. Add input validation
4. Create PID-based process management

### Phase 4: Documentation (Week 4)
1. Write installation guide
2. Create troubleshooting guide
3. Document architecture
4. Add usage examples

---

## Testing Checklist

- [ ] VPN connects successfully
- [ ] Kill switch blocks non-VPN traffic
- [ ] Local network access preserved (SSH, file shares)
- [ ] DNS queries go through VPN
- [ ] BitTorrent bound to VPN interface
- [ ] Auto-reconnect works on VPN failure
- [ ] Clean shutdown restores all settings
- [ ] System fully usable during operation
- [ ] Logs rotate properly
- [ ] Status script shows accurate info

---

## Notes

All enhancements are designed with these principles:
1. **Reversible**: Every change can be undone on shutdown
2. **Non-blocking**: Local network and system usage preserved
3. **Minimal**: Smallest possible changes to achieve goals
4. **Safe**: Multiple layers of validation and error handling
