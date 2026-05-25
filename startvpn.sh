#!/bin/bash
#
# Copyright (c) 2022-2025 Stewart Rogers
# SPDX-License-Identifier: MIT
#
# Usage: ./startvpn.sh [OPTIONS]
# Options:
#   --non-interactive    Run without prompts (requires config file)
#   --ovpn-url URL       Download OVPN from URL
#   --no-killswitch      Skip UFW kill switch (monitoring will not start)
#   --help               Show help
#

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

# Load config
if [ -f "$HOME/.vpn_config.conf" ]; then
    source "$HOME/.vpn_config.conf"
elif [ -f "$SCRIPT_DIR/vpn_config.conf" ]; then
    source "$SCRIPT_DIR/vpn_config.conf"
fi

# Defaults
BACKUP_DIR="${BACKUP_DIR:-/tmp/vpn_backups}"
PID_DIR="${PID_DIR:-/tmp/vpn_pids}"
LOG_DIR="${LOG_DIR:-$SCRIPT_DIR/vpn_logs}"
XVPNHOME="${VPN_HOME:-/etc/openvpn/}"
XVPNCHOME="${VPN_CLIENT_HOME:-/etc/openvpn/client/}"
XVPNLOGFILE="${VPN_LOG_FILE:-/var/log/openvpn.log}"

mkdir -p "$BACKUP_DIR" "$PID_DIR" "$LOG_DIR"

#
# Parse arguments
#
NON_INTERACTIVE=false
CUSTOM_OVPN_URL=""
SKIP_KILLSWITCH=false

while [[ $# -gt 0 ]]; do
    case $1 in
        --non-interactive)
            NON_INTERACTIVE=true
            shift
            ;;
        --ovpn-url)
            CUSTOM_OVPN_URL="$2"
            shift 2
            ;;
        --no-killswitch)
            SKIP_KILLSWITCH=true
            shift
            ;;
        --help)
            echo "Usage: $0 [OPTIONS]"
            echo "Options:"
            echo "  --non-interactive    Run without prompts"
            echo "  --ovpn-url URL       Download OVPN from URL"
            echo "  --no-killswitch      Skip UFW kill switch"
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
# Logging
##
log_message() {
    local level=$1
    local message=$2
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [$level] $message" | tee -a "$LOG_DIR/vpn.log"
}

rotate_logs() {
    if [ -f "$LOG_DIR/vpn.log" ]; then
        local size
        size=$(stat -c%s "$LOG_DIR/vpn.log" 2>/dev/null || echo 0)
        if [ "$size" -gt 10485760 ]; then
            mv "$LOG_DIR/vpn.log" "$LOG_DIR/vpn.log.1"
            [ -f "$LOG_DIR/vpn.log.1.gz" ] && rm "$LOG_DIR/vpn.log.1.gz"
            gzip "$LOG_DIR/vpn.log.1" 2>/dev/null || true
        fi
    fi
}

##
# Validation
##
validate_url() {
    local url=$1
    if [[ ! "$url" =~ ^https?:// ]]; then
        log_message "ERROR" "Invalid URL format: $url"
        return 1
    fi
    return 0
}

validate_ip() {
    local ip=$1
    if [[ ! "$ip" =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$ ]]; then
        return 1
    fi
    return 0
}

##
# Pre-flight: IPv6 must be disabled before proceeding
##
check_ipv6_disabled() {
    # Check for global-scope IPv6 addresses - these can route to the internet and cause leaks.
    # Handles all disable methods: kernel param (ipv6.disable=1), sysctl, or simply no addresses.
    # Link-local (fe80::) addresses are not a concern as they cannot route beyond the LAN.
    if ip -6 addr show 2>/dev/null | grep "inet6" | grep -q "scope global"; then
        echo ""
        echo "ERROR: Active global IPv6 addresses detected - this is a leak risk."
        echo ""
        echo "  IPv6 traffic can bypass the VPN tunnel."
        echo "  Disable IPv6 using one of:"
        echo "    /boot/cmdline.txt  - add: ipv6.disable=1  (requires reboot)"
        echo "    /etc/sysctl.conf   - add: net.ipv6.conf.all.disable_ipv6 = 1"
        echo "                             net.ipv6.conf.default.disable_ipv6 = 1"
        echo "                        then: sudo sysctl -p"
        echo ""
        return 1
    fi
    return 0
}

if ! check_ipv6_disabled; then
    exit 1
fi

##
# Error trap
##
KILLSWITCH_APPLIED=false
ERROR_HANDLED=false

cleanup_on_error() {
    local exit_code=$?
    if [ $exit_code -ne 0 ] && [ "$ERROR_HANDLED" != true ]; then
        log_message "ERROR" "Script exited with error (code $exit_code)"
        if [ "$KILLSWITCH_APPLIED" = true ]; then
            log_message "INFO" "Resetting UFW to base state..."
            sudo bash "$SCRIPT_DIR/ufw_base.sh" >> "$LOG_DIR/vpn.log" 2>&1 || true
        fi
    fi
}
trap cleanup_on_error EXIT
trap 'log_message "WARN" "Script interrupted by user"; exit 130' INT TERM

rotate_logs

divider() { echo "------------------------------------------------------------"; }

clear
echo ""
echo "  VPN Start Script"
echo ""
divider

#
# Capture home IP before VPN starts
#
YHOMEIP=$(curl -s --max-time 10 https://api.ipify.org 2>/dev/null)
if ! validate_ip "$YHOMEIP"; then
    log_message "WARN" "Could not retrieve valid external IP address"
    YHOMEIP=""
fi
echo "  Home IP (pre-VPN): ${YHOMEIP:-unknown}"
log_message "INFO" "Home IP (pre-VPN): ${YHOMEIP:-unknown}"
echo ""

#
# Optional: check and install required software
#
if [ "$NON_INTERACTIVE" = true ]; then
    SWCHECK="n"
else
    read -p "  Check software installation? [y/n]: " SWCHECK
    SWCHECK=$(echo "$SWCHECK" | tr '[:upper:]' '[:lower:]' | tr -d '\r')
fi

if [ "$SWCHECK" = "y" ]; then
    echo ""
    log_message "INFO" "Checking required software..."

    sudo apt-get -qq update 2>/dev/null || true

    declare -a PACKAGES=("qbittorrent-nox" "openvpn" "ufw" "python3" "python3-pip")
    declare -a TO_INSTALL=()
    declare -a TO_UPDATE=()

    for pkg in "${PACKAGES[@]}"; do
        if dpkg -l | grep -q "^ii  $pkg"; then
            echo "  installed: $pkg"
            TO_UPDATE+=("$pkg")
        else
            echo "  missing:   $pkg"
            TO_INSTALL+=("$pkg")
        fi
    done

    if [ ${#TO_UPDATE[@]} -gt 0 ]; then
        echo ""
        echo "Updating existing packages..."
        sudo apt-get install --only-upgrade -y -qq "${TO_UPDATE[@]}" 2>/dev/null || true
    fi

    if [ ${#TO_INSTALL[@]} -gt 0 ]; then
        echo ""
        echo "Installing: ${TO_INSTALL[*]}"
        sudo apt-get install -y -qq "${TO_INSTALL[@]}"
    fi

    echo ""
    if python3 -c "import requests" >/dev/null 2>&1; then
        echo "  installed: python3-requests"
    else
        echo "  installing python3-requests..."
        sudo apt-get install -y -qq python3-requests
    fi

    echo ""
    log_message "INFO" "Software check complete"
fi

#
# OVPN Configuration: download or select existing
#
divider
echo ""
if [ -n "$CUSTOM_OVPN_URL" ]; then
    GETOVPN="y"
    OVPNURL="$CUSTOM_OVPN_URL"
elif [ "$NON_INTERACTIVE" = true ]; then
    GETOVPN="n"
else
    read -p "  Download a new OVPN file? [y/n]: " GETOVPN
    GETOVPN=$(echo "$GETOVPN" | tr '[:upper:]' '[:lower:]' | tr -d '\r')
fi

if [ "$GETOVPN" = "y" ]; then
    # Clean out old configs
    log_message "INFO" "Cleaning $XVPNCHOME for new OVPN file..."
    sudo rm -f "$XVPNCHOME"*.ovpn
    rm -f "$SCRIPT_DIR"/*.ovpn

    if [ -z "$OVPNURL" ]; then
        read -p "Paste URL to download OVPN file: " OVPNURL
    fi

    if ! validate_url "$OVPNURL"; then
        log_message "ERROR" "Invalid OVPN URL"
        ERROR_HANDLED=true
        exit 1
    fi

    log_message "INFO" "Downloading OVPN from: $OVPNURL"

    OVPN_FILENAME=$(basename "$OVPNURL" | sed 's/[?&].*//')
    if [[ ! "$OVPN_FILENAME" =~ \.ovpn$ ]] || [[ "$OVPN_FILENAME" =~ \.aspx ]]; then
        OVPN_FILENAME=$(echo "$OVPNURL" | grep -oP '/[^/]*\.ovpn' | tail -1 | sed 's|^/||')
        if [ -z "$OVPN_FILENAME" ] || [[ ! "$OVPN_FILENAME" =~ \.ovpn$ ]]; then
            OVPN_FILENAME="config_$(date +%Y%m%d_%H%M%S).ovpn"
        fi
    fi

    curl -s -L -o "$SCRIPT_DIR/$OVPN_FILENAME" "$OVPNURL"
    if [ $? -ne 0 ] || [ ! -s "$SCRIPT_DIR/$OVPN_FILENAME" ]; then
        log_message "ERROR" "Failed to download OVPN file"
        rm -f "$SCRIPT_DIR/$OVPN_FILENAME"
        ERROR_HANDLED=true
        exit 1
    fi

    for XFILE in "$SCRIPT_DIR"/*.ovpn; do
        log_message "INFO" "Moving $(basename "$XFILE") to $XVPNCHOME"
        sudo mv "$XFILE" "$XVPNCHOME"
        sudo chmod 600 "$XVPNCHOME$(basename "$XFILE")"
        sudo chown root:root "$XVPNCHOME$(basename "$XFILE")"
    done

    XCONFIGFILE=$(sudo ls -t "$XVPNCHOME"*.ovpn 2>/dev/null | head -1)
else
    XCONFIGFILE=$(sudo ls -t "$XVPNCHOME"*.ovpn 2>/dev/null | head -1)
    if [ -z "$XCONFIGFILE" ]; then
        # Check current directory as fallback
        if ls "$SCRIPT_DIR"/*.ovpn 1>/dev/null 2>&1; then
            log_message "INFO" "Moving .ovpn file(s) from script directory to $XVPNCHOME"
            for XFILE in "$SCRIPT_DIR"/*.ovpn; do
                sudo mv "$XFILE" "$XVPNCHOME"
                sudo chmod 600 "$XVPNCHOME$(basename "$XFILE")"
                sudo chown root:root "$XVPNCHOME$(basename "$XFILE")"
            done
            XCONFIGFILE=$(sudo ls -t "$XVPNCHOME"*.ovpn 2>/dev/null | head -1)
        fi
    fi
    if [ -z "$XCONFIGFILE" ]; then
        log_message "ERROR" "No .ovpn file found in $XVPNCHOME"
        ERROR_HANDLED=true
        exit 1
    fi
fi

OVPN_COUNT=$(sudo find "$XVPNCHOME" -maxdepth 1 -name "*.ovpn" -type f 2>/dev/null | wc -l)
if [ "$OVPN_COUNT" -gt 1 ]; then
    echo "  Config (newest of $OVPN_COUNT): $(basename "$XCONFIGFILE")"
    log_message "INFO" "Multiple OVPN files - using newest: $(basename "$XCONFIGFILE")"
else
    echo "  Config: $(basename "$XCONFIGFILE")"
    log_message "INFO" "OVPN config: $(basename "$XCONFIGFILE")"
fi
echo ""

#
# Apply UFW kill switch before starting OpenVPN
#
divider
echo ""
if [ "$SKIP_KILLSWITCH" != true ]; then
    echo "  Applying UFW kill switch..."
    echo ""
    sudo bash "$SCRIPT_DIR/ufw_killswitch.sh"
    KS_RC=$?
    echo ""
    if [ $KS_RC -eq 0 ]; then
        KILLSWITCH_APPLIED=true
        log_message "INFO" "UFW kill switch active"
    else
        echo "  ERROR: Failed to apply UFW kill switch"
        log_message "ERROR" "Failed to apply UFW kill switch - aborting"
        ERROR_HANDLED=true
        exit 1
    fi
else
    echo "  Kill switch skipped (--no-killswitch)"
    log_message "WARN" "Kill switch skipped"
fi
echo ""

#
# Kill any existing OpenVPN (ensures tun0 is used, not tun1/tun2)
#
if pgrep -x openvpn > /dev/null; then
    echo "  Stopping existing OpenVPN process..."
    sudo pkill -x openvpn
    sleep 2
fi

#
# Start OpenVPN
#
divider
echo ""
echo "  Starting OpenVPN..."
log_message "INFO" "Starting OpenVPN: $(basename "$XCONFIGFILE")"
sudo rm -f "$XVPNLOGFILE"
sudo openvpn \
    --config "$XCONFIGFILE" \
    --log "$XVPNLOGFILE" \
    --daemon \
    --ping 10 \
    --ping-exit 60 \
    --auth-nocache \
    --mute-replay-warnings \
    --data-ciphers AES-256-GCM:AES-128-GCM:CHACHA20-POLY1305:AES-128-CBC \
    --data-ciphers-fallback AES-128-CBC \
    --verb 3

sleep 7
echo ""

# Check for success indicator in log
if sudo grep -q "Initialization Sequence Completed" "$XVPNLOGFILE" 2>/dev/null; then
    echo "  ** VPN connected - Initialization Sequence Completed **"
    echo ""
else
    echo "  OpenVPN log (last 5 lines):"
    echo ""
    sudo tail -5 "$XVPNLOGFILE" 2>/dev/null | sed 's/^/    /'
    echo ""
fi

#
# Wait for VPN confirmation
#
divider
echo ""
iStart=""
if [ "$NON_INTERACTIVE" = true ]; then
    log_message "INFO" "Waiting for VPN connection (non-interactive)..."
    sleep 10
    if ip link show tun0 &>/dev/null; then
        iStart="y"
        log_message "INFO" "VPN interface detected (tun0)"
    else
        iStart="f"
        log_message "ERROR" "VPN interface not detected"
    fi
else
    while true; do
        read -p "  Has VPN started? [Y/N/F - F=failed]: " iStart
        iStart=$(echo "$iStart" | tr '[:upper:]' '[:lower:]' | tr -d '\r')
        case "$iStart" in
            y) break ;;
            f) break ;;
            n)
                for i in $(seq 10 -1 0); do
                    echo -ne "  Rechecking in $i seconds...\r"
                    sleep 1
                done
                echo ""
                if sudo grep -q "Initialization Sequence Completed" "$XVPNLOGFILE" 2>/dev/null; then
                    echo "  ** VPN connected - Initialization Sequence Completed **"
                else
                    sudo tail -5 "$XVPNLOGFILE" 2>/dev/null | sed 's/^/    /'
                fi
                echo ""
                ;;
            *) echo "  Please enter Y, N, or F" ;;
        esac
    done
fi

#
# Launch monitoring if VPN confirmed
#
echo ""
divider
echo ""
if [ "$iStart" = "y" ]; then
    if [ -z "$YHOMEIP" ]; then
        echo "  ERROR: Home IP not captured - cannot start monitor"
        echo "  Run manually: ./checkip.sh <your_home_ip>"
        log_message "ERROR" "Home IP not captured - monitor not started"
    elif [ "$SKIP_KILLSWITCH" = true ]; then
        echo "  WARNING: Kill switch was skipped - monitor not started"
        echo "  Run: sudo bash $SCRIPT_DIR/ufw_killswitch.sh  then  ./checkip.sh $YHOMEIP"
        log_message "WARN" "Kill switch skipped - monitor not started"
    else
        "$SCRIPT_DIR/checkip.sh" "$YHOMEIP" &
        CHECKIP_PID=$!
        echo "$CHECKIP_PID" > "$PID_DIR/checkip.pid"
        log_message "INFO" "VPN monitor started (PID: $CHECKIP_PID)"
        echo "  VPN is running.  Monitor and qBittorrent starting..."
        echo ""
        echo "  Monitor log:  tail -f $LOG_DIR/latest.log"
        echo "  To stop:      ./stopvpn.sh"
    fi
elif [ "$iStart" = "f" ]; then
    log_message "ERROR" "VPN startup failed"
    echo "  Resetting UFW..."
    if [ "$KILLSWITCH_APPLIED" = true ]; then
        sudo bash "$SCRIPT_DIR/ufw_base.sh" > /dev/null 2>&1 || true
        KILLSWITCH_APPLIED=false
    fi
    echo "  UFW reset to base state."
fi
echo ""
