#!/bin/bash
# Copyright (c) 2022-2025 Stewart Rogers
# SPDX-License-Identifier: MIT

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

# Load config
if [ -f "$HOME/.vpn_config.conf" ]; then
    source "$HOME/.vpn_config.conf"
elif [ -f "$SCRIPT_DIR/vpn_config.conf" ]; then
    source "$SCRIPT_DIR/vpn_config.conf"
fi

# Settings
FAST_CHECK_INTERVAL="${FAST_CHECK_INTERVAL:-2}"
IP_CHECK_INTERVAL="${IP_CHECK_INTERVAL:-10}"
PID_DIR="${PID_DIR:-/tmp/vpn_pids}"
LOG_DIR="${LOG_DIR:-$SCRIPT_DIR/vpn_logs}"
MAX_SESSIONS="${MAX_SESSIONS:-20}"

YIP_HOMEIP="$1"

if [ -z "$YIP_HOMEIP" ]; then
    echo "Usage: $0 <home_ip>"
    exit 1
fi

# --- Pre-flight checks (run before log redirect so errors appear in terminal) ---
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

check_killswitch_active() {
    if ! sudo ufw status verbose 2>/dev/null | grep -q "deny (outgoing)"; then
        echo ""
        echo "ERROR: UFW kill switch is not active - outgoing traffic is unrestricted."
        echo ""
        echo "  Apply the kill switch first:"
        echo "    sudo bash \"$SCRIPT_DIR/ufw_killswitch.sh\""
        echo ""
        return 1
    fi
    return 0
}

if ! check_killswitch_active; then
    exit 1
fi

# --- Session logging setup ---
mkdir -p "$LOG_DIR"
SESSION_LOG="$LOG_DIR/session_$(date '+%Y%m%d_%H%M%S').log"
exec > "$SESSION_LOG" 2>&1
ln -sf "$SESSION_LOG" "$LOG_DIR/latest.log"

# Prune old sessions, keep last MAX_SESSIONS
ls -t "$LOG_DIR"/session_*.log 2>/dev/null | tail -n +"$((MAX_SESSIONS + 1))" | xargs rm -f 2>/dev/null || true

# --- Logging ---
log() {
    local level="$1"
    local msg="$2"
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [$level] $msg"
}

# --- Cleanup on exit (Ctrl+C, kill, or natural exit) ---
# UFW kill switch is left active intentionally — stopvpn.sh removes it.
# This ensures outgoing stays blocked if checkip exits unexpectedly.
_exit_handler() {
    log "INFO" "Stopping qBittorrent before exit..."
    stop_qbittorrent
    log "INFO" "Kill switch remains active - run stopvpn.sh to restore base state"
    log "INFO" "=== Session ended ==="
}
trap _exit_handler EXIT

# --- VPN checks ---
check_openvpn_process() {
    if ! pgrep -x openvpn > /dev/null; then
        log "CRITICAL" "OpenVPN process not running"
        return 1
    fi
    return 0
}

check_vpn_interface() {
    if ! ip link show tun0 &>/dev/null; then
        log "CRITICAL" "VPN interface (tun0) is down"
        return 1
    fi
    return 0
}

check_routing() {
    local route
    route=$(ip route get 8.8.8.8 2>/dev/null)
    if ! echo "$route" | grep -q "dev tun0"; then
        log "CRITICAL" "Traffic is not routing through tun0 - possible leak"
        return 1
    fi
    return 0
}

# Returns 0=secure, 1=confirmed leak, 2=could not determine
perform_ip_check() {
    local result
    result=$(python3 "$SCRIPT_DIR/vpn_active.py" "$YIP_HOMEIP" 2>/dev/null)
    case "$result" in
        secure)
            log "INFO" "IP check: secure"
            return 0
            ;;
        leak)
            log "CRITICAL" "IP check: HOME IP DETECTED - confirmed leak"
            return 1
            ;;
        error)
            log "WARN" "IP check: could not reach IP services"
            return 2
            ;;
        *)
            log "ERROR" "IP check: unexpected response: '$result'"
            return 2
            ;;
    esac
}

# --- qBittorrent control ---
is_qbittorrent_running() {
    local pid_file="$PID_DIR/qbittorrent.pid"
    if [ -f "$pid_file" ]; then
        local pid
        pid=$(cat "$pid_file" 2>/dev/null)
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            return 0
        fi
    fi
    pgrep -f "qbittorrent-nox" >/dev/null
}

stop_qbittorrent() {
    local pid_file="$PID_DIR/qbittorrent.pid"
    if [ -f "$pid_file" ]; then
        local pid
        pid=$(cat "$pid_file" 2>/dev/null)
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            log "INFO" "Stopping qBittorrent (PID: $pid)"
            kill "$pid" 2>/dev/null || true
            sleep 1
            kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null || true
        fi
        rm -f "$pid_file"
    fi
    if pgrep -f "qbittorrent-nox" >/dev/null; then
        log "INFO" "Stopping qBittorrent (pkill fallback)"
        sudo pkill -f "qbittorrent-nox" 2>/dev/null || true
    fi
}

start_qbittorrent() {
    if is_qbittorrent_running; then
        log "INFO" "qBittorrent already running"
        return 0
    fi
    log "INFO" "Starting qBittorrent"
    nohup qbittorrent-nox > "$SCRIPT_DIR/qbit.log" 2>&1 &
    local qpid=$!
    mkdir -p "$PID_DIR"
    echo "$qpid" > "$PID_DIR/qbittorrent.pid"
    sleep 1
    if kill -0 "$qpid" 2>/dev/null; then
        log "INFO" "qBittorrent started (PID: $qpid)"
    else
        log "WARN" "qBittorrent may have failed to start"
    fi
}

# --- Main ---
log "INFO" "=== VPN monitoring session started ==="
log "INFO" "Monitoring home IP: $YIP_HOMEIP"
log "INFO" "Fast check: ${FAST_CHECK_INTERVAL}s  |  IP check: ${IP_CHECK_INTERVAL}s"
log "INFO" "Session log: $SESSION_LOG"

# Verify VPN is up before starting qBittorrent
log "INFO" "Initial VPN verification..."
if ! check_openvpn_process || ! check_vpn_interface || ! check_routing; then
    log "CRITICAL" "VPN not ready at startup - aborting"
    exit 1
fi

perform_ip_check
ip_rc=$?
if [ $ip_rc -eq 1 ]; then
    log "CRITICAL" "IP leak detected at startup - aborting"
    exit 1
elif [ $ip_rc -eq 2 ]; then
    log "WARN" "Could not verify IP at startup - continuing, will recheck"
fi

start_qbittorrent

LAST_IP_CHECK=$(date +%s)
consecutive_ip_errors=0
log "INFO" "Monitoring active"

while true; do
    sleep "$FAST_CHECK_INTERVAL"
    current_time=$(date +%s)

    # Fast check: process + interface + routing
    if ! check_openvpn_process || ! check_vpn_interface || ! check_routing; then
        log "CRITICAL" "VPN failure detected - shutting down"
        # trap handles stop_qbittorrent and UFW reset
        exit 1
    fi

    # Periodic full IP check
    if [ $((current_time - LAST_IP_CHECK)) -ge "$IP_CHECK_INTERVAL" ]; then
        perform_ip_check
        ip_rc=$?

        if [ $ip_rc -eq 1 ]; then
            log "CRITICAL" "IP leak confirmed - shutting down"
            exit 1
        elif [ $ip_rc -eq 2 ]; then
            consecutive_ip_errors=$((consecutive_ip_errors + 1))
            log "WARN" "IP check error ($consecutive_ip_errors consecutive)"
            if [ $consecutive_ip_errors -ge 3 ]; then
                log "CRITICAL" "3 consecutive IP check failures - shutting down as precaution"
                exit 1
            fi
            # Retry sooner than normal
            LAST_IP_CHECK=$((current_time - IP_CHECK_INTERVAL + 5))
        else
            consecutive_ip_errors=0
            LAST_IP_CHECK=$current_time
        fi
    fi
done
