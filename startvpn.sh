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

# Resolve the Python interpreter (./.venv if present, else system python3)
source "$SCRIPT_DIR/py_env.sh"

# Load config
CONFIG_FILE=""
if [ -f "$HOME/.vpn_config.conf" ]; then
    CONFIG_FILE="$HOME/.vpn_config.conf"
    source "$CONFIG_FILE"
elif [ -f "$SCRIPT_DIR/vpn_config.conf" ]; then
    CONFIG_FILE="$SCRIPT_DIR/vpn_config.conf"
    source "$CONFIG_FILE"
fi

# Defaults
PID_DIR="${PID_DIR:-/tmp/vpn_pids}"
LOG_DIR="${LOG_DIR:-$SCRIPT_DIR/vpn_logs}"
XVPNCHOME="${VPN_CLIENT_HOME:-/etc/openvpn/client/}"
XVPNLOGFILE="${VPN_LOG_FILE:-/var/log/openvpn.log}"
MAX_STARTUP_ATTEMPTS="${MAX_STARTUP_ATTEMPTS:-3}"

mkdir -p "$PID_DIR" "$LOG_DIR"

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
            # $2 must be checked before `shift 2`. With no argument following,
            # `shift 2` has nothing to shift, fails, and leaves $# unchanged —
            # so `while [[ $# -gt 0 ]]` spins forever on the same argument.
            if [ -z "${2:-}" ]; then
                echo "ERROR: --ovpn-url requires a URL argument"
                echo "Use --help for usage information"
                exit 1
            fi
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
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [$level] $message" >> "$LOG_DIR/vpn.log"
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
# Config persistence - update or append a KEY="value" line in CONFIG_FILE.
# Falls back to the repo's vpn_config.conf if no config file was loaded yet.
##
persist_config_value() {
    local key=$1
    local value=$2
    local file="${CONFIG_FILE:-$SCRIPT_DIR/vpn_config.conf}"
    local quoted

    if [ ! -f "$file" ]; then
        echo "# VPN Configuration File" > "$file"
    fi

    # Single-quote the value, escaping any embedded single quotes as '\''.
    # This file is `source`d on the next run, so a double-quoted value would
    # let $(...), `...` and $VAR in a path the user typed at a prompt execute
    # as shell code. Single quotes suppress all of it.
    quoted="'${value//\'/\'\\\'\'}'"

    if grep -q "^${key}=" "$file"; then
        # Rewritten in bash rather than `sed -i "s|...|${key}=\"${value}\"|"`.
        # sed expands & in the replacement to the whole match, so a path like
        # /mnt/a&b wrote a mangled line back into a file that is then sourced;
        # a | in the value broke it too, since | was the delimiter.
        local tmp line replaced=false
        tmp=$(mktemp "${file}.XXXXXX") || return 1
        while IFS= read -r line || [ -n "$line" ]; do
            if [ "$replaced" = false ] && [[ "$line" == "${key}="* ]]; then
                printf '%s=%s\n' "$key" "$quoted"
                replaced=true
            else
                printf '%s\n' "$line"
            fi
        done < "$file" > "$tmp"
        # Copy contents rather than mv, so the original file's ownership and
        # permissions survive (mktemp creates 0600).
        if cat "$tmp" > "$file"; then
            rm -f "$tmp"
        else
            rm -f "$tmp"
            return 1
        fi
    else
        printf '%s=%s\n' "$key" "$quoted" >> "$file"
    fi
    CONFIG_FILE="$file"
}

##
# Pre-flight: IPv6 must be disabled before proceeding
##
disable_ipv6() {
    sudo sysctl -w net.ipv6.conf.all.disable_ipv6=1     > /dev/null 2>&1
    sudo sysctl -w net.ipv6.conf.default.disable_ipv6=1 > /dev/null 2>&1
}

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

disable_ipv6
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

# Ask before burning another attempt out of MAX_STARTUP_ATTEMPTS. Only asks
# when interactive and an attempt is actually still available - non-interactive
# runs keep auto-retrying (nobody is there to answer), and there's nothing to
# ask on the last attempt since the loop ends either way.
confirm_retry() {
    if [ "$NON_INTERACTIVE" = true ] || [ "$ATTEMPT" -ge "$MAX_STARTUP_ATTEMPTS" ]; then
        return 0
    fi
    local reply
    read -p "  Retry? [y/N]: " reply
    reply=$(echo "$reply" | tr '[:upper:]' '[:lower:]' | tr -d '\r')
    [ "$reply" = "y" ]
}

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

    declare -a PACKAGES=("qbittorrent-nox" "openvpn" "ufw" "python3" "python3-venv")
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
    # Python dependencies live in ./.venv, installed from
    # webapp/requirements.txt — not in apt's python3-requests. One source of
    # truth beats a venv that half-inherits from the system packages.
    if "$VPN_PYTHON" -c "import requests" >/dev/null 2>&1; then
        echo "  installed: python dependencies ($VPN_PYTHON_KIND)"
    else
        echo "  missing:   python dependencies"
        echo "             run: $SCRIPT_DIR/setup_venv.sh"
    fi

    echo ""
    log_message "INFO" "Software check complete"
fi

#
# qBittorrent download location (asked once, then remembered in config).
# If already set, offer to confirm/edit it in place rather than only
# printing it - press Enter to keep, edit and Enter to change, or clear
# the line to fall back to qBittorrent's own default.
#
if [ "$NON_INTERACTIVE" != true ]; then
    divider
    echo ""
    if [ -n "$QBT_SAVE_PATH" ]; then
        echo "  Current qBittorrent save path: $QBT_SAVE_PATH"
        read -e -i "$QBT_SAVE_PATH" -p "  Press Enter to keep, or edit the path: " QBT_SAVE_PATH_NEW
    else
        read -e -p "  Where should qBittorrent save downloaded files? [qBittorrent default]: " QBT_SAVE_PATH_NEW
    fi
    QBT_SAVE_PATH_NEW="${QBT_SAVE_PATH_NEW/#\~/$HOME}"

    if [ "$QBT_SAVE_PATH_NEW" = "$QBT_SAVE_PATH" ]; then
        [ -n "$QBT_SAVE_PATH" ] && echo "  Keeping: $QBT_SAVE_PATH"
    elif [ -n "$QBT_SAVE_PATH_NEW" ]; then
        if mkdir -p "$QBT_SAVE_PATH_NEW" 2>/dev/null; then
            QBT_SAVE_PATH="$QBT_SAVE_PATH_NEW"
            persist_config_value "QBT_SAVE_PATH" "$QBT_SAVE_PATH"
            log_message "INFO" "qBittorrent save path set: $QBT_SAVE_PATH"
            echo "  Saved. qBittorrent will download to: $QBT_SAVE_PATH"
        else
            echo "  Could not create '$QBT_SAVE_PATH_NEW' - keeping previous setting."
            log_message "WARN" "Could not create QBT_SAVE_PATH: $QBT_SAVE_PATH_NEW"
        fi
    else
        QBT_SAVE_PATH=""
        persist_config_value "QBT_SAVE_PATH" ""
        log_message "INFO" "qBittorrent save path cleared - using qBittorrent's own default"
        echo "  Cleared. qBittorrent will use its own default save location."
    fi
    echo ""
elif [ -n "$QBT_SAVE_PATH" ]; then
    echo "  qBittorrent save path: $QBT_SAVE_PATH"
    echo ""
fi

#
# Connect to VPN, retrying with a fresh or re-selected .ovpn on failure.
# Nothing sensitive is running yet at any point in this loop - qBittorrent
# only ever starts later, inside checkip.sh, after its own independent
# verification. That's what makes it safe to relax the firewall between
# attempts here (a retry needs outgoing open again to download a new .ovpn).
#
STARTUP_OK=false
for ATTEMPT in $(seq 1 "$MAX_STARTUP_ATTEMPTS"); do
    if [ "$ATTEMPT" -gt 1 ]; then
        divider
        echo ""
        echo "  Retry attempt $ATTEMPT of $MAX_STARTUP_ATTEMPTS"
        echo ""
        if [ "$KILLSWITCH_APPLIED" = true ]; then
            sudo bash "$SCRIPT_DIR/ufw_base.sh" > /dev/null 2>&1 || true
            KILLSWITCH_APPLIED=false
        fi
    fi

    #
    # OVPN Configuration: download or select existing
    #
    divider
    echo ""
    OVPNURL=""
    if [ -n "$CUSTOM_OVPN_URL" ]; then
        GETOVPN="y"
        OVPNURL="$CUSTOM_OVPN_URL"
    elif [ "$NON_INTERACTIVE" = true ]; then
        GETOVPN="n"
    else
        read -p "  Download a new OVPN file? [y/n]: " GETOVPN
        GETOVPN=$(echo "$GETOVPN" | tr '[:upper:]' '[:lower:]' | tr -d '\r')
    fi

    ATTEMPT_FAILED=false
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
            echo "  ERROR: Invalid OVPN URL"
            ATTEMPT_FAILED=true
        else
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
                echo "  ERROR: Failed to download OVPN file"
                rm -f "$SCRIPT_DIR/$OVPN_FILENAME"
                ATTEMPT_FAILED=true
            else
                for XFILE in "$SCRIPT_DIR"/*.ovpn; do
                    log_message "INFO" "Moving $(basename "$XFILE") to $XVPNCHOME"
                    sudo mv "$XFILE" "$XVPNCHOME"
                    sudo chmod 600 "$XVPNCHOME$(basename "$XFILE")"
                    sudo chown root:root "$XVPNCHOME$(basename "$XFILE")"
                done
                XCONFIGFILE=$(sudo ls -t "$XVPNCHOME"*.ovpn 2>/dev/null | head -1)
            fi
        fi
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
            echo "  ERROR: No .ovpn file found in $XVPNCHOME"
            ATTEMPT_FAILED=true
        fi
    fi

    if [ "$ATTEMPT_FAILED" = true ]; then
        if [ "$ATTEMPT" -lt "$MAX_STARTUP_ATTEMPTS" ]; then
            echo "  ($((MAX_STARTUP_ATTEMPTS - ATTEMPT)) attempt(s) left.)"
            if ! confirm_retry; then
                log_message "INFO" "User declined to retry after attempt $ATTEMPT"
                break
            fi
            echo ""
        fi
        continue
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
            # A kill-switch failure is an environment/permissions problem, not
            # a bad .ovpn - retrying the same way would not help, so this is
            # the one failure in this loop that aborts immediately.
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

    if [ "$iStart" = "y" ]; then
        STARTUP_OK=true
        break
    fi

    log_message "WARN" "Connection attempt $ATTEMPT failed"
    if [ "$KILLSWITCH_APPLIED" = true ]; then
        sudo bash "$SCRIPT_DIR/ufw_base.sh" > /dev/null 2>&1 || true
        KILLSWITCH_APPLIED=false
    fi
    if [ "$ATTEMPT" -lt "$MAX_STARTUP_ATTEMPTS" ]; then
        echo "  Attempt $ATTEMPT failed. ($((MAX_STARTUP_ATTEMPTS - ATTEMPT)) attempt(s) left.)"
        if ! confirm_retry; then
            log_message "INFO" "User declined to retry after attempt $ATTEMPT"
            break
        fi
    fi
done

#
# Launch monitoring if VPN confirmed, otherwise report final failure
#
echo ""
divider
echo ""
if [ "$STARTUP_OK" = true ]; then
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
else
    log_message "ERROR" "VPN startup failed after $MAX_STARTUP_ATTEMPTS attempt(s)"
    echo "  VPN startup failed after $MAX_STARTUP_ATTEMPTS attempt(s). Giving up."
    echo "  Resetting UFW..."
    if [ "$KILLSWITCH_APPLIED" = true ]; then
        sudo bash "$SCRIPT_DIR/ufw_base.sh" > /dev/null 2>&1 || true
        KILLSWITCH_APPLIED=false
    fi
    echo "  UFW reset to base state."
fi
echo ""
