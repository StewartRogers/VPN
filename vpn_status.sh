#!/bin/bash
# Copyright (c) 2022-2025 Stewart Rogers
# SPDX-License-Identifier: MIT
#
# VPN and BitTorrent Status Check Script
# This script displays the current status of VPN, BitTorrent, and monitoring services

echo "==================================="
echo "VPN & BitTorrent Status"
echo "==================================="
echo ""

# Check OpenVPN
if pgrep -f openvpn > /dev/null 2>&1; then
    echo "✓ OpenVPN: Running"
    VPN_IP=$(curl -s --max-time 3 https://api.ipify.org 2>/dev/null)
    if [ -n "$VPN_IP" ]; then
        echo "  External IP: $VPN_IP"
    else
        echo "  External IP: Unable to retrieve"
    fi
else
    echo "✗ OpenVPN: Not running"
fi

# Check VPN interface
if ip link show tun0 &>/dev/null 2>&1; then
    echo "✓ VPN Interface: Up (tun0)"
    # Get interface IP
    TUN_IP=$(ip addr show tun0 2>/dev/null | grep "inet " | awk '{print $2}')
    if [ -n "$TUN_IP" ]; then
        echo "  Interface IP: $TUN_IP"
    fi
else
    echo "✗ VPN Interface: Down"
fi

# Check qbittorrent
if pgrep -f qbittorrent-nox > /dev/null 2>&1; then
    echo "✓ qBittorrent: Running"
    # Check if it's listening
    if command -v ss > /dev/null 2>&1; then
        LISTENING=$(ss -tulpn 2>/dev/null | grep qbittorrent | head -1 | awk '{print $5}')
        if [ -n "$LISTENING" ]; then
            echo "  Listening on: $LISTENING"
        fi
    fi
else
    echo "✗ qBittorrent: Not running"
fi

# Check deluge (alternative torrent client)
if pgrep -f deluged > /dev/null 2>&1; then
    echo "✓ Deluge: Running"
else
    echo "✗ Deluge: Not running"
fi

# Check monitoring script
if pgrep -f checkip.sh > /dev/null 2>&1; then
    echo "✓ VPN Monitor: Running"
    SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
    LOG_FILE="${SCRIPT_DIR}/checkvpn.log"
    if [ -f "$LOG_FILE" ]; then
        LAST_LINE=$(tail -1 "$LOG_FILE" 2>/dev/null)
        if [ -n "$LAST_LINE" ]; then
            echo "  Last check: $LAST_LINE"
        fi
    fi
else
    echo "✗ VPN Monitor: Not running"
fi

# Check for kill switch rules
if sudo iptables -L OUTPUT -n 2>/dev/null | grep -q "tun"; then
    echo "✓ Kill Switch: Active"
else
    echo "✗ Kill Switch: Not active"
fi

echo ""
echo "==================================="
