#!/bin/bash
#
# Copyright (c) 2022-2025 Stewart Rogers
# SPDX-License-Identifier: MIT
#
# This licensed under the MIT License
# A short and simple permissive license with conditions only requiring
# preservation of copyright and license notices. Licensed works, modifications,
# and larger works may be distributed under different terms and without source code.
#
# Usage: ./startvpn.sh [OPTIONS]
# Options:
#   --non-interactive    Run without prompts (requires config file)
#   --config FILE        Use specific config file
#   --ovpn-url URL       Download OVPN from URL
#   --no-killswitch      Skip killswitch setup
#   --help               Show help
# Author: Stewart Rogers
# Date: August 2025
#

#
# Load configuration file if exists
#
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
if [ -f "$HOME/.vpn_config.conf" ]; then
    source "$HOME/.vpn_config.conf"
elif [ -f "$SCRIPT_DIR/vpn_config.conf" ]; then
    source "$SCRIPT_DIR/vpn_config.conf"
fi

# Set default values if not in config
BACKUP_DIR="${BACKUP_DIR:-/tmp/vpn_backups}"
PID_DIR="${PID_DIR:-/tmp/vpn_pids}"
LOG_DIR="${LOG_DIR:-$SCRIPT_DIR/vpn_logs}"
SETUP_KILLSWITCH="${SETUP_KILLSWITCH:-false}"
PREVENT_DNS_LEAK="${PREVENT_DNS_LEAK:-true}"
DISABLE_IPV6="${DISABLE_IPV6:-true}"
BIND_TO_VPN_INTERFACE="${BIND_TO_VPN_INTERFACE:-true}"
MAX_RECONNECT_ATTEMPTS="${MAX_RECONNECT_ATTEMPTS:-3}"

# Create necessary directories
mkdir -p "$BACKUP_DIR" "$PID_DIR" "$LOG_DIR"

#
# Parse command line arguments
#
NON_INTERACTIVE=false
CUSTOM_CONFIG=""
CUSTOM_OVPN_URL=""
SKIP_KILLSWITCH=false

while [[ $# -gt 0 ]]; do
    case $1 in
        --non-interactive)
            NON_INTERACTIVE=true
            shift
            ;;
        --config)
            CUSTOM_CONFIG="$2"
            shift 2
            ;;
        --ovpn-url)
            CUSTOM_OVPN_URL="$2"
            shift 2
            ;;
        --no-killswitch)
            SKIP_KILLSWITCH=true
            SETUP_KILLSWITCH=false
            shift
            ;;
        --help)
            echo "Usage: $0 [OPTIONS]"
            echo "Options:"
            echo "  --non-interactive    Run without prompts"
            echo "  --config FILE        Use specific config file"
            echo "  --ovpn-url URL       Download OVPN from URL"
            echo "  --no-killswitch      Skip killswitch setup"
            echo "  --help               Show this help"
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            echo "Use --help for usage information"
            exit 1
            ;;
    esac
done

##
# Logging functions
##
log_message() {
  local level=$1
  local message=$2
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] [$level] $message" | tee -a "$LOG_DIR/vpn.log"
}

rotate_logs() {
    if [ -f "$LOG_DIR/vpn.log" ]; then
        local size=$(stat -c%s "$LOG_DIR/vpn.log" 2>/dev/null || echo 0)
        if [ $size -gt 10485760 ]; then  # 10MB
            mv "$LOG_DIR/vpn.log" "$LOG_DIR/vpn.log.1"
            [ -f "$LOG_DIR/vpn.log.1.gz" ] && rm "$LOG_DIR/vpn.log.1.gz"
            gzip "$LOG_DIR/vpn.log.1" 2>/dev/null || true
        fi
    fi
}

#
# Input validation functions
#
validate_url() {
    local url=$1
    if [[ ! "$url" =~ ^https?:// ]]; then
        log_message "ERROR" "Invalid URL format: $url"
        return 1
    fi
    return 0
}

validate_directory() {
    local dir=$1
    if [ ! -d "$dir" ]; then
        log_message "ERROR" "Directory does not exist: $dir"
        return 1
    fi
    if [ ! -r "$dir" ]; then
        log_message "ERROR" "Directory not readable: $dir"
        return 1
    fi
    return 0
}

validate_ip() {
    local ip=$1
    if [[ ! "$ip" =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$ ]]; then
        log_message "ERROR" "Invalid IP address format: $ip"
        return 1
    fi
    return 0
}

#
# Security functions
#
setup_killswitch() {
    log_message "INFO" "Setting up network kill switch..."
    
    # Save existing rules for restoration
    sudo iptables-save > "$BACKUP_DIR/iptables.backup"
    
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
    # Use the XCONFIGFILE if available, otherwise search for OVPN file
    local OVPN_FILE="${1:-}"
    if [ -z "$OVPN_FILE" ]; then
        OVPN_FILE=$(find /etc/openvpn/client -name "*.ovpn" -type f 2>/dev/null | head -1)
    fi
    
    if [ -n "$OVPN_FILE" ] && [ -f "$OVPN_FILE" ]; then
        local VPN_SERVER=$(grep "^remote " "$OVPN_FILE" 2>/dev/null | head -1 | awk '{print $2}')
        local VPN_PORT=$(grep "^remote " "$OVPN_FILE" 2>/dev/null | head -1 | awk '{print $3}')
        if [ -n "$VPN_SERVER" ] && [ -n "$VPN_PORT" ]; then
            sudo iptables -A OUTPUT -d "$VPN_SERVER" -p udp --dport "$VPN_PORT" -j ACCEPT
            log_message "INFO" "Kill switch configured for VPN server: $VPN_SERVER:$VPN_PORT"
        fi
    fi
    
    # Drop everything else
    sudo iptables -A OUTPUT -j DROP
    
    log_message "INFO" "Kill switch activated - only VPN and local network traffic allowed"
}

setup_dns() {
    log_message "INFO" "Configuring DNS leak prevention..."
    
    # Backup original resolv.conf
    sudo cp /etc/resolv.conf "$BACKUP_DIR/resolv.conf.backup"
    
    # Set VPN-safe DNS servers (Cloudflare)
    echo "nameserver 1.1.1.1" | sudo tee /etc/resolv.conf > /dev/null
    echo "nameserver 1.0.0.1" | sudo tee -a /etc/resolv.conf > /dev/null
    
    # Prevent changes
    sudo chattr +i /etc/resolv.conf 2>/dev/null || true
    
    log_message "INFO" "DNS configured to use secure resolvers"
}

disable_ipv6() {
    log_message "INFO" "Disabling IPv6 to prevent leaks..."
    
    # Save current state for both settings
    local CURRENT_IPV6_ALL=$(sysctl net.ipv6.conf.all.disable_ipv6 2>/dev/null | awk '{print $3}')
    local CURRENT_IPV6_DEFAULT=$(sysctl net.ipv6.conf.default.disable_ipv6 2>/dev/null | awk '{print $3}')
    echo "$CURRENT_IPV6_ALL" > "$BACKUP_DIR/ipv6_all.backup"
    echo "$CURRENT_IPV6_DEFAULT" > "$BACKUP_DIR/ipv6_default.backup"
    
    # Disable IPv6
    sudo sysctl -w net.ipv6.conf.all.disable_ipv6=1 > /dev/null
    sudo sysctl -w net.ipv6.conf.default.disable_ipv6=1 > /dev/null
    
    log_message "INFO" "IPv6 disabled"
}

configure_qbittorrent_binding() {
    log_message "INFO" "Configuring qBittorrent to bind to VPN interface..."
    
    local CONFIG_FILE="$HOME/.config/qBittorrent/qBittorrent.conf"
    
    # Ensure config directory exists
    mkdir -p "$(dirname "$CONFIG_FILE")"
    
    # Backup existing config
    if [ -f "$CONFIG_FILE" ]; then
        cp "$CONFIG_FILE" "$BACKUP_DIR/qBittorrent.conf.backup"
    fi
    
    # Note: Actual binding configuration will be done after VPN is up
    log_message "INFO" "qBittorrent will be configured to bind to tun0"
}

#
# Cleanup trap handler
#
# Track whether we've already handled an error to avoid duplicate messages
ERROR_HANDLED=false

cleanup_on_error() {
    local exit_code=$?
  if [ $exit_code -ne 0 ] && [ "$ERROR_HANDLED" != true ]; then
    log_message "ERROR" "Script exited with error code $exit_code"
    echo ""
    echo "An error occurred. Check logs at: $LOG_DIR/vpn.log"
    echo ""
    fi
}

trap cleanup_on_error EXIT
trap 'log_message "WARN" "Script interrupted by user"; exit 130' INT TERM

# Rotate logs at startup
rotate_logs

clear
echo ""
echo "VPN Start Script"
echo ""

#
# VARIABLES
#
XHOME=$PWD"/"
YLOGFILE=$XHOME"checkvpn.log"
XVPNHOME="/etc/openvpn/"
XVPNCHOME="/etc/openvpn/client/"
XVPNLOGFILE="/var/log/openvpn.log"
XPYFILE=$XHOME"vpn_active.py"
XSUCCESS="TRUE"
VPNSERVICE=1

#
# Retrieve and display the current external IP address for later VPN verification
#
YHOMEIP=$(curl -s https://ipinfo.io/ip)
if ! validate_ip "$YHOMEIP"; then
    log_message "WARN" "Could not retrieve valid external IP address"
    YHOMEIP=""
fi
log_message "INFO" "Current external IP: $YHOMEIP"
echo ""

#
# Check and install required software and Python packages
# Prompts user to optionally install OpenVPN, qbittorrent-nox, 
# screen, ufw, python3, pip3, and Python dependencies.
#
if [ "$NON_INTERACTIVE" = true ]; then
  SWCHECK="n"
else
  while true; do
    read -p "Do you want to check if all software is installed? [y/n]: " SWCHECK
    case "${SWCHECK,,}" in
      y|n) break;;
      *) echo "Please enter 'y' or 'n'.";;
    esac
  done
fi

if [[ "${SWCHECK,,}" == "y" ]]; then
  log_message "INFO" "Checking and updating required software..."
  echo ""
  
  # Update package lists
  sudo apt-get -qq update 2>/dev/null || true
  
  # Array of packages to check and manage
  declare -a PACKAGES=("qbittorrent-nox" "openvpn" "screen" "ufw" "python3" "python3-pip")
  declare -a TO_INSTALL=()
  declare -a TO_UPDATE=()
  
  # Check each package
  for pkg in "${PACKAGES[@]}"; do
    if dpkg -l | grep -q "^ii  $pkg"; then
      echo "✓ $pkg is installed"
      TO_UPDATE+=("$pkg")
    else
      echo "✗ $pkg not found - will install"
      TO_INSTALL+=("$pkg")
    fi
  done
  
  # Update existing packages
  if [ ${#TO_UPDATE[@]} -gt 0 ]; then
    echo ""
    echo "Updating existing packages..."
    sudo apt-get install --only-upgrade -y -qq "${TO_UPDATE[@]}" 2>/dev/null || true
    log_message "INFO" "Updated ${#TO_UPDATE[@]} existing package(s)"
  fi
  
  # Install missing packages
  if [ ${#TO_INSTALL[@]} -gt 0 ]; then
    echo ""
    echo "Installing missing packages: ${TO_INSTALL[*]}"
    echo "Note: qbittorrent-nox requires manual first run to accept disclaimer"
    sudo apt-get install -y -qq "${TO_INSTALL[@]}"
    log_message "INFO" "Installed ${#TO_INSTALL[@]} missing package(s)"
  fi
  
  # Check Python requests library (special case - may be installed via pip or apt)
  echo ""
  if python3 -c "import requests" >/dev/null 2>&1; then
    echo "✓ python3-requests is available"
  else
    echo "Installing python3-requests..."
    sudo apt-get install -y -qq python3-requests
    log_message "INFO" "Installed python3-requests"
  fi
  
  echo ""
  log_message "INFO" "Software check complete"
fi

#
# Optional: Configure UFW firewall rules for VPN traffic
# Prompts user to allow a specific port and protocol through 
# UFW before starting VPN
#
if [ "$NON_INTERACTIVE" = true ]; then
  UFWCONFIRM="n"
else
  while true; do
    read -p "Do you want to allow a port through UFW? [y/n]: " UFWCONFIRM
    case "${UFWCONFIRM,,}" in
      y|n) break;;
      *) echo "Please enter 'y' or 'n'.";;
    esac
  done
fi

if [[ "${UFWCONFIRM,,}" == "y" ]]; then
    read -p "Enter the port number you want to allow: " UFWPORT
    read -p "Enter the protocol (tcp/udp): " UFWPROTO
    
    # Validate port number (1-65535)
    if ! [[ "$UFWPORT" =~ ^[0-9]+$ ]] || [ "$UFWPORT" -lt 1 ] || [ "$UFWPORT" -gt 65535 ]; then
        log_message "ERROR" "Invalid port number: $UFWPORT (must be 1-65535)"
    # Validate protocol (tcp or udp only)
    elif ! [[ "${UFWPROTO,,}" =~ ^(tcp|udp)$ ]]; then
        log_message "ERROR" "Invalid protocol: $UFWPROTO (must be tcp or udp)"
    else
        log_message "INFO" "Configuring UFW rule for port $UFWPORT/$UFWPROTO"
        
        # Save UFW rule for later removal
        echo "$UFWPORT/$UFWPROTO" > "$BACKUP_DIR/ufw_rule.backup"
        
        sudo ufw allow "$UFWPORT/$UFWPROTO" > /dev/null
        log_message "INFO" "UFW rule applied for $UFWPORT/$UFWPROTO"
        echo ""
    fi
fi

#
# OVPN Configuration: Download or select VPN config file
# Prompts user to download a new .ovpn file or use an existing one,
# then copies it to the OpenVPN client directory for use.
#
if [ -n "$CUSTOM_OVPN_URL" ]; then
  GETOVPN="y"
  OVPNURL="$CUSTOM_OVPN_URL"
elif [ "$NON_INTERACTIVE" = true ]; then
  GETOVPN="n"
else
  while true; do
    read -p "Do you want to download a new OVPN file? [y/n]: " GETOVPN
    case "${GETOVPN,,}" in
      y|n) break;;
      *) echo "Please enter 'y' or 'n'.";;
    esac
  done
fi

if [[ "${GETOVPN,,}" == "y" ]]; then
  rm -f *.ovpn
  # Clean out the OpenVPN client directory when downloading a new config
  log_message "INFO" "Cleaning $XVPNCHOME directory for new OVPN file..."
  sudo rm -f "$XVPNCHOME"*.ovpn
  
  if [ -z "$OVPNURL" ]; then
    read -p "Paste in a URL to download OVPN file: " OVPNURL
  fi
  
  if ! validate_url "$OVPNURL"; then
    log_message "ERROR" "Invalid OVPN URL provided"
    ERROR_HANDLED=true
    exit 1
  fi
  
  log_message "INFO" "Downloading OVPN file from: $OVPNURL"
  
  # Extract filename from URL and ensure .ovpn extension
  OVPN_FILENAME=$(basename "$OVPNURL" | sed 's/[?&].*//')
  # If filename doesn't end with .ovpn or has aspx, generate a proper name
  if [[ ! "$OVPN_FILENAME" =~ \.ovpn$ ]] || [[ "$OVPN_FILENAME" =~ \.aspx ]]; then
    # Try to extract meaningful name from URL path
    OVPN_FILENAME=$(echo "$OVPNURL" | grep -oP '/[^/]*\.ovpn' | tail -1 | sed 's|^/||')
    # If still no valid name, use a timestamp-based name
    if [ -z "$OVPN_FILENAME" ] || [[ ! "$OVPN_FILENAME" =~ \.ovpn$ ]]; then
      OVPN_FILENAME="downloaded_config_$(date +%Y%m%d_%H%M%S).ovpn"
    fi
  fi
  
  # Download directly with specified filename
  curl -s -L -o "$OVPN_FILENAME" "$OVPNURL"
  CURL_EXIT=$?
  if [ $CURL_EXIT -ne 0 ]; then
    log_message "ERROR" "Failed to download OVPN file"
    rm -f "$OVPN_FILENAME"
    ERROR_HANDLED=true
    exit 1
  fi
  
  # Verify the downloaded file is valid
  if [ ! -s "$OVPN_FILENAME" ]; then
    log_message "ERROR" "Downloaded file is empty"
    rm -f "$OVPN_FILENAME"
    ERROR_HANDLED=true
    exit 1
  fi
  
  OVPN_COUNT=$(ls *.ovpn 2>/dev/null | wc -l)
  if [ "$OVPN_COUNT" -eq 0 ]; then
    log_message "ERROR" "No .ovpn file found after download"
    ERROR_HANDLED=true
    exit 1
  fi
  log_message "INFO" "Downloaded $OVPN_COUNT .ovpn file(s)"
  LAST_OVPN=""
  for XFILE in *.ovpn; do
    log_message "INFO" "Moving $XFILE to $XVPNCHOME"
    sudo mv "$XFILE" "$XVPNCHOME"
    # Set proper permissions: readable/writable by root only (600) for security
    sudo chmod 600 "$XVPNCHOME$XFILE"
    sudo chown root:root "$XVPNCHOME$XFILE"
    LAST_OVPN="$XFILE"
  done
  XCONFIGFILE="$XVPNCHOME$LAST_OVPN"
else
  # If not downloading, check if any .ovpn file exists in /etc/openvpn/client/
  if ! sudo ls "$XVPNCHOME"*.ovpn 1> /dev/null 2>&1; then
    log_message "ERROR" "No .ovpn file found in $XVPNCHOME"
    log_message "INFO" "Checking for .ovpn files in current directory..."
    if ls *.ovpn 1> /dev/null 2>&1; then
      log_message "INFO" "Found .ovpn file(s) in current directory, moving to $XVPNCHOME"
      for XFILE in *.ovpn; do
        sudo mv "$XFILE" "$XVPNCHOME"
        # Set proper permissions: readable/writable by root only (600) for security
        sudo chmod 600 "$XVPNCHOME$XFILE"
        sudo chown root:root "$XVPNCHOME$XFILE"
      done
    else
      ERROR_HANDLED=true
      exit 1
    fi
  fi
  # Get the newest OVPN file from the directory (sorted by modification time)
  XCONFIGFILE=$(sudo ls -t "$XVPNCHOME"*.ovpn 2>/dev/null | head -1)
  if [ -z "$XCONFIGFILE" ]; then
    log_message "ERROR" "Failed to locate OVPN file in $XVPNCHOME"
    ERROR_HANDLED=true
    exit 1
  fi
  # Count available files and log if multiple exist
  OVPN_COUNT=$(sudo find "$XVPNCHOME" -maxdepth 1 -name "*.ovpn" -type f 2>/dev/null | wc -l)
  if [ "$OVPN_COUNT" -gt 1 ]; then
    log_message "INFO" "Multiple OVPN files found in $XVPNCHOME. Using the newest: $(basename "$XCONFIGFILE")"
  fi
fi

#
# Start and monitor OpenVPN service
# Uses the selected config file, starts OpenVPN in daemon mode,
# and monitors log output and connection status interactively.
#
while [ $VPNSERVICE != "q" ]; do
  if [ "$VPNSERVICE" == "1" ]; then
    log_message "INFO" "Preparing to start OpenVPN"
    cd $XVPNHOME
    sudo rm -rf $XVPNLOGFILE
    log_message "INFO" "Current external IP: $(curl -s https://ipinfo.io/ip)"
    log_message "INFO" "Starting VPN with config: $XCONFIGFILE"
    sudo openvpn --config $XCONFIGFILE --log $XVPNLOGFILE --daemon --ping 10 --ping-exit 60 --auth-nocache --mute-replay-warnings --data-ciphers AES-256-GCM:AES-128-GCM:CHACHA20-POLY1305:AES-128-CBC --data-ciphers-fallback AES-128-CBC --verb 3
    sleep 7
    echo "... Viewing OpenVPN log"
    echo ""
    sudo tail $XVPNLOGFILE
    echo ""
    
    if [ "$NON_INTERACTIVE" = true ]; then
      # In non-interactive mode, wait and auto-detect VPN connection
      log_message "INFO" "Waiting for VPN connection (non-interactive mode)..."
      sleep 10
      # Check if tun0 interface exists
      if ip link show tun0 &>/dev/null 2>&1; then
        iStart="y"
        log_message "INFO" "VPN interface detected (tun0)"
        log_message "INFO" "Current external IP: $(curl -s https://ipinfo.io/ip)"
        VPNSERVICE="q"
      else
        iStart="f"
        log_message "ERROR" "VPN interface not detected - startup failed"
        VPNSERVICE="q"
      fi
    else
      while true; do
        read -p "Has it started? [Y/N/F - f is failed] " iStart
        case "${iStart,,}" in
          y)
            # Log current external IP before exiting loop
            log_message "INFO" "Current external IP: $(curl -s https://ipinfo.io/ip)"
            VPNSERVICE="q"
            break
            ;;
          f)
            log_message "ERROR" "VPN startup failed"
            VPNSERVICE="q"
            break
            ;;
          n)
            echo ""
            for load in $(seq 10 -1 0); do
              echo -ne "... Check again in $load seconds\r"
              sleep 1
            done
            echo ""
            echo "... Viewing log"
            echo ""
            sudo tail $XVPNLOGFILE
            echo ""
            ;;
          *)
            read -p "Type 'q' to quit: " VPNSERVICE
            if [ "$VPNSERVICE" == "q" ]; then
              break
            fi
            ;;
        esac
      done
    fi
  fi
done

#
# Apply security measures after VPN connection is established
#
if [[ "${iStart,,}" == "y" ]]; then
  log_message "INFO" "VPN connected successfully, applying security measures..."
  
  # Setup kill switch
  if [ "$SETUP_KILLSWITCH" = true ]; then
    setup_killswitch "$XCONFIGFILE"
  fi
  
  # Setup DNS leak prevention
  if [ "$PREVENT_DNS_LEAK" = true ]; then
    setup_dns
  fi
  
  # Disable IPv6
  if [ "$DISABLE_IPV6" = true ]; then
    disable_ipv6
  fi
  
  # Configure qBittorrent binding
  if [ "$BIND_TO_VPN_INTERFACE" = true ]; then
    configure_qbittorrent_binding
  fi
  
  log_message "INFO" "Security measures applied successfully"
fi

#
# Test VPN connection and start torrent server if secure
#
if [[ "${iStart,,}" == "y" && $VPNSERVICE == "q" ]]; then
  echo ""
  log_message "INFO" "Testing VPN connection"
  # Run Python script to verify VPN is active and IP is changed
  active=$(python3 $XPYFILE $YHOMEIP)
  log_message "INFO" "VPN test result: $active"
  
  # If VPN is secure, start torrent server
  if [ "$active" == "secure" ]; then
    log_message "INFO" "Starting qbittorrent-nox"
    
    # Start qbittorrent and save PID
    nohup qbittorrent-nox > "$XHOME/qbit.log" 2>&1 &
    QBIT_PID=$!
    echo $QBIT_PID > "$PID_DIR/qbittorrent.pid"
    log_message "INFO" "qbittorrent-nox started (PID: $QBIT_PID)"
    
    sleep 2
    
    # Verify it's still running
    if kill -0 $QBIT_PID 2>/dev/null; then
      log_message "INFO" "Torrent Server started successfully (PID: $QBIT_PID)"
    else
      log_message "WARN" "Torrent Server may have failed to start"
    fi
  else
    echo ""
    log_message "WARN" "Torrent Server not started - VPN not secure"
    echo ""
  fi
fi

#
# Run checkip script to monitor external IP address if VPN is secure
#
if [ "$active" == "secure" ]; then
  YCHECKFILE=$XHOME"checkip.sh "$YHOMEIP
  log_message "INFO" "Starting VPN monitoring script"
  cd $XHOME
  ./checkip.sh $YHOMEIP &
  CHECKIP_PID=$!
  echo $CHECKIP_PID > "$PID_DIR/checkip.pid"
  log_message "INFO" "VPN monitoring started (PID: $CHECKIP_PID)"
  echo "... See progress: tail -f ${YLOGFILE}"
  echo ""
  echo "VPN and services started successfully!"
  echo "Use './vpn_status.sh' to check status"
  log_message "INFO" "VPN startup complete"
  echo ""
fi
