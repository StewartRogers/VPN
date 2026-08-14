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
    if ! stop_qbittorrent; then
        log "CRITICAL" "qBittorrent could not be stopped - do NOT run stopvpn.sh until"
        log "CRITICAL" "it is dead; opening UFW now would hand it the ISP link."
        log "INFO" "=== Session ended ==="
        return
    fi
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

check_no_ipv6_leak() {
    if ip -6 addr show 2>/dev/null | grep "inet6" | grep -q "scope global"; then
        log "CRITICAL" "Global IPv6 address detected - leak risk (bypasses the IPv4-only firewall rules)"
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

# Grace period for a clean qBittorrent exit. Mirrors QBT_STOP_GRACE in
# webapp/monitor.py — keep the two in step.
#
# qBittorrent rewrites qBittorrent.conf and flushes resume data on exit, and
# that takes seconds, not milliseconds. The old 1s window here (and a 5s one on
# the web side) killed it mid-write. Waiting is safe: the kill switch is up for
# the whole of this function, so UFW is denying all outgoing traffic.
QBT_STOP_GRACE="${QBT_STOP_GRACE:-30}"

# Returns 0 only when qBittorrent is *confirmed* gone, so callers can gate the
# next teardown step on it rather than assuming the signal worked.
stop_qbittorrent() {
    local pid_file="$PID_DIR/qbittorrent.pid"
    local pid="" waited=0

    if ! is_qbittorrent_running; then
        rm -f "$pid_file"
        return 0
    fi

    [ -f "$pid_file" ] && pid=$(cat "$pid_file" 2>/dev/null)
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        log "INFO" "Stopping qBittorrent (PID: $pid)"
        kill "$pid" 2>/dev/null || true
    else
        log "INFO" "Stopping qBittorrent (pkill fallback)"
        sudo pkill -f "qbittorrent-nox" 2>/dev/null || true
    fi

    while [ "$waited" -lt "$QBT_STOP_GRACE" ]; do
        if ! is_qbittorrent_running; then
            rm -f "$pid_file"
            log "INFO" "qBittorrent stopped"
            return 0
        fi
        sleep 1
        waited=$((waited + 1))
        if [ $((waited % 5)) -eq 0 ]; then
            log "INFO" "Still waiting for qBittorrent to shut down (${waited}s/${QBT_STOP_GRACE}s)"
        fi
    done

    log "WARN" "qBittorrent has not exited after ${QBT_STOP_GRACE}s - sending SIGKILL"
    sudo pkill -9 -f "qbittorrent-nox" 2>/dev/null || true
    sleep 1
    if is_qbittorrent_running; then
        log "CRITICAL" "qBittorrent is STILL RUNNING after SIGKILL - kill switch stays active"
        return 1
    fi
    rm -f "$pid_file"
    log "INFO" "qBittorrent stopped"
    return 0
}

apply_qbittorrent_config() {
    # Shared with the web path: qbt_config.py merges the settings this project
    # owns (tun0 bind, save path, concurrent-download limit) into the live
    # qBittorrent config without clobbering anything qBittorrent wrote itself.
    local out
    if ! out=$(python3 "$SCRIPT_DIR/qbt_config.py" 2>&1); then
        log "WARN" "Could not apply qBittorrent config"
    fi
    while IFS= read -r line; do
        [ -n "$line" ] && log "INFO" "qBittorrent config - $line"
    done <<< "$out"
}

start_qbittorrent() {
    if is_qbittorrent_running; then
        log "INFO" "qBittorrent already running"
        return 0
    fi
    apply_qbittorrent_config
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
if [ $ip_rc -eq 2 ]; then
    log "WARN" "Could not verify IP at startup - retrying once..."
    sleep 3
    perform_ip_check
    ip_rc=$?
fi
if [ $ip_rc -eq 1 ]; then
    log "CRITICAL" "IP leak detected at startup - aborting"
    exit 1
elif [ $ip_rc -eq 2 ]; then
    log "CRITICAL" "Could not verify external IP at startup - aborting (refusing to start qBittorrent without positive verification)"
    exit 1
fi

start_qbittorrent

LAST_IP_CHECK=$(date +%s)
consecutive_ip_errors=0
log "INFO" "Monitoring active"

while true; do
    sleep "$FAST_CHECK_INTERVAL"
    current_time=$(date +%s)

    # Fast check: process + interface + routing + no newly-appeared global IPv6
    if ! check_openvpn_process || ! check_vpn_interface || ! check_routing || ! check_no_ipv6_leak; then
        log "CRITICAL" "VPN failure detected - shutting down"
        # trap handles stop_qbittorrent and UFW reset
        exit 1
    fi

    # Periodic full IP check
    if [ $((current_time - LAST_IP_CHECK)) -ge "$IP_CHECK_INTERVAL" ]; then
        perform_ip_check
        ip_rc=$?
        post_check_time=$(date +%s)

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
            # Retry sooner than normal (stamp from post-check time)
            LAST_IP_CHECK=$((post_check_time - IP_CHECK_INTERVAL + 5))
        else
            consecutive_ip_errors=0
            LAST_IP_CHECK=$post_check_time
        fi
    fi
done
