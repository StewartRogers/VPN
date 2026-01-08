#!/bin/bash
# Copyright (c) 2022-2025 Stewart Rogers
# SPDX-License-Identifier: MIT
#
# Original Author: Stewart Rogers
# Enhanced for faster VPN disconnect detection and auto-reconnect
# This licensed under the MIT License
# A short and simple permissive license with conditions only requiring
# preservation of copyright and license notices. Licensed works, modifications,
# and larger works may be distributed under different terms and without source code.
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

#
# VARIABLES
#
XIP_HOME=$PWD"/"
XIP_PYFILE=$XIP_HOME"vpn_active.py"
XIP_LOGFILE=$XIP_HOME"checkvpn.log"
XIP_STOPFILE=$XIP_HOME"stopvpn.sh --shutdown-only"
PID_DIR="${PID_DIR:-/tmp/vpn_pids}"
YIP_HOMEIP=$1
#
# New variables for enhanced monitoring
#
FAST_CHECK_INTERVAL=${FAST_CHECK_INTERVAL:-2}
IP_CHECK_INTERVAL=${IP_CHECK_INTERVAL:-10}
MAX_RECONNECT_ATTEMPTS=${MAX_RECONNECT_ATTEMPTS:-3}
LAST_IP_CHECK=0
reconnect_count=0
#
# redirect stdout/stderr to a file
#
rm -rf $XIP_LOGFILE
exec >$XIP_LOGFILE 2>&1

#
# Enhanced monitoring functions
#
check_openvpn_process() {
    if ! pgrep -f openvpn > /dev/null; then
        echo "$(date): CRITICAL - OpenVPN process not running!"
        return 1
    fi
    return 0
}

check_vpn_interface() {
    if ! ip link show tun0 &>/dev/null 2>&1; then
        echo "$(date): CRITICAL - VPN interface (tun0) is down!"
        return 1
    fi
    return 0
}

# qBittorrent control helpers
is_qbittorrent_running() {
    # Prefer PID file if present
    local pid_file="$PID_DIR/qbittorrent.pid"
    if [ -f "$pid_file" ]; then
        local pid=$(cat "$pid_file" 2>/dev/null)
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            echo "$(date): qBittorrent running (PID: $pid)"
            return 0
        fi
    fi
    # Fallback to process check
    if pgrep -f "qbittorrent-nox" >/dev/null; then
        echo "$(date): qBittorrent running (detected by process)"
        return 0
    fi
    return 1
}

stop_qbittorrent() {
    local pid_file="$PID_DIR/qbittorrent.pid"
    if [ -f "$pid_file" ]; then
        local pid=$(cat "$pid_file" 2>/dev/null)
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            echo "$(date): Stopping qBittorrent (PID: $pid)"
            kill "$pid" 2>/dev/null || true
            sleep 1
            if kill -0 "$pid" 2>/dev/null; then
                kill -9 "$pid" 2>/dev/null || true
            fi
        fi
        rm -f "$pid_file"
    fi
    # Ensure stopped even without PID file
    if pgrep -f "qbittorrent-nox" >/dev/null; then
        echo "$(date): Stopping qBittorrent (pkill fallback)"
        sudo pkill -f "qbittorrent-nox" 2>/dev/null || true
    fi
}

start_qbittorrent() {
    # Avoid duplicate starts
    if is_qbittorrent_running; then
        echo "$(date): qBittorrent already running; skip start"
        return 0
    fi
    echo "$(date): Starting qBittorrent"
    nohup qbittorrent-nox > "$XIP_HOME/qbit.log" 2>&1 &
    local qpid=$!
    mkdir -p "$PID_DIR"
    echo "$qpid" > "$PID_DIR/qbittorrent.pid"
    # Verify
    sleep 1
    if kill -0 "$qpid" 2>/dev/null; then
        echo "$(date): qBittorrent started (PID: $qpid)"
        return 0
    else
        echo "$(date): WARNING - qBittorrent may have failed to start"
        return 1
    fi
}

perform_ip_check() {
    echo "$(date): Performing full IP check..."
    local result=$(python3 $XIP_PYFILE $YIP_HOMEIP)
    echo "$(date): IP test result: $result"
    if [ "$result" != "secure" ]; then
        echo "$(date): CRITICAL - IP leak detected!"
        return 1
    fi
    return 0
}

#
# Auto-reconnect function
#
attempt_reconnect() {
    echo "$(date): Attempting VPN reconnection (attempt $((reconnect_count + 1))/$MAX_RECONNECT_ATTEMPTS)..."
    
    # Stop existing VPN
    sudo pkill -f openvpn
    sleep 2
    
    # Find the most recent .ovpn file
    XCONFIGFILE=$(ls /etc/openvpn/client/*.ovpn 2>/dev/null | head -1)
    
    if [ -z "$XCONFIGFILE" ]; then
        echo "$(date): ERROR - No .ovpn config file found"
        return 1
    fi
    
    echo "$(date): Restarting VPN with config: $XCONFIGFILE"
    
    # Restart VPN
    sudo openvpn --config $XCONFIGFILE --log /var/log/openvpn.log --daemon --ping 10 --ping-exit 60 --auth-nocache --mute-replay-warnings --data-ciphers AES-256-GCM:AES-128-GCM:CHACHA20-POLY1305:AES-128-CBC --data-ciphers-fallback AES-128-CBC --verb 3
    
    # Wait for connection
    sleep 10
    
    # Check if successful
    if check_openvpn_process && check_vpn_interface; then
        # Give it a moment more to stabilize
        sleep 5
        if perform_ip_check; then
            echo "$(date): Reconnection successful!"
            # Safe to restart torrent
            start_qbittorrent
            return 0
        fi
    fi
    
    echo "$(date): Reconnection failed"
    return 1
}

#
# Main
#
echo ""
echo "$(date): Starting enhanced VPN monitoring with auto-reconnect..."
echo "$(date): Fast checks every $FAST_CHECK_INTERVAL seconds"
echo "$(date): Full IP checks every $IP_CHECK_INTERVAL seconds"
echo "$(date): Max reconnect attempts: $MAX_RECONNECT_ATTEMPTS"
echo ""

active="secure"
firstrun="y"
echo ""

while [[ "$active" == "secure" ]]; do
    current_time=$(date +%s)
    
    # Always do fast checks (process and interface)
    if ! check_openvpn_process || ! check_vpn_interface; then
        # VPN failure detected - attempt reconnection
        # Immediately stop torrent to prevent leaks
        if is_qbittorrent_running; then
            echo "$(date): Immediate action: Stopping qBittorrent due to VPN failure"
            stop_qbittorrent
        fi
        if [ $reconnect_count -lt $MAX_RECONNECT_ATTEMPTS ]; then
            reconnect_count=$((reconnect_count + 1))
            echo "$(date): VPN failure detected - initiating reconnect attempt $reconnect_count/$MAX_RECONNECT_ATTEMPTS"
            
            if attempt_reconnect; then
                reconnect_count=0  # Reset counter on success
                LAST_IP_CHECK=$current_time  # Reset IP check timer
                echo "$(date): VPN reconnected successfully, resuming monitoring"
                continue
            fi
        else
            echo "$(date): Max reconnection attempts ($MAX_RECONNECT_ATTEMPTS) reached"
            active="notsecure"
            break
        fi
    fi
    
    # Do full IP check based on interval
    if [ $((current_time - LAST_IP_CHECK)) -ge $IP_CHECK_INTERVAL ]; then
        if [[ "$firstrun" == "n" ]]; then
            echo ""
            echo "$(date): Running scheduled IP verification..."
        else
            echo "$(date): Initial VPN verification..."
        fi
        
        if ! perform_ip_check; then
            # IP leak detected - attempt reconnection
            # Immediately stop torrent to prevent leaks
            if is_qbittorrent_running; then
                echo "$(date): Immediate action: Stopping qBittorrent due to IP leak"
                stop_qbittorrent
            fi
            if [ $reconnect_count -lt $MAX_RECONNECT_ATTEMPTS ]; then
                reconnect_count=$((reconnect_count + 1))
                echo "$(date): IP leak detected - initiating reconnect attempt $reconnect_count/$MAX_RECONNECT_ATTEMPTS"
                
                if attempt_reconnect; then
                    reconnect_count=0  # Reset counter on success
                    LAST_IP_CHECK=$current_time  # Reset IP check timer
                    echo "$(date): VPN reconnected successfully, resuming monitoring"
                    firstrun="n"
                    continue
                fi
            else
                echo "$(date): Max reconnection attempts ($MAX_RECONNECT_ATTEMPTS) reached"
                active="notsecure"
                break
            fi
        fi
        
        LAST_IP_CHECK=$current_time
        firstrun="n"
        echo "$(date): VPN status confirmed secure"
        reconnect_count=0  # Reset counter after successful check
    else
        # Just show we're monitoring
        if [ $((current_time % 10)) -eq 0 ]; then
            echo "$(date): VPN monitoring active (process: OK, interface: OK)"
        fi
    fi
    
    sleep $FAST_CHECK_INTERVAL
done

echo ""
if [ "$active" != "secure" ]; then
    echo "$(date): VPN COMPROMISED - All reconnection attempts failed"
    echo "$(date): Initiating emergency shutdown..."
    echo "$(date): Stopping Torrent Server and VPN..."
    $XIP_STOPFILE
    echo "$(date): Emergency shutdown complete"
else
    echo "$(date): Monitoring stopped normally"
fi
echo ""
echo "$(date): FINISHED"
echo ""