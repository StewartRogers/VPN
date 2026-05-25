#!/bin/bash
# Copyright (c) 2022-2025 Stewart Rogers
# SPDX-License-Identifier: MIT
#
# Applies UFW kill switch: blocks all outgoing except through the VPN tunnel.
# Reads the current .ovpn file to determine VPN server IP/port dynamically.
#
# Run this BEFORE starting OpenVPN, then start OpenVPN, then run checkip.sh.
# To reverse: sudo bash ufw_base.sh
#
# Usage: sudo bash ufw_killswitch.sh

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

if [ "$EUID" -ne 0 ]; then
    echo "Must be run as root: sudo bash $0"
    exit 1
fi

# --- Find and parse .ovpn config ---
OVPN=$(ls /etc/openvpn/client/*.ovpn 2>/dev/null | head -1)
if [ -z "$OVPN" ]; then
    echo "ERROR: No .ovpn file found in /etc/openvpn/client/"
    exit 1
fi
echo "Using config: $OVPN"

REMOTE_LINE=$(grep "^remote " "$OVPN" | head -1)
VPN_HOST=$(echo "$REMOTE_LINE" | awk '{print $2}')
VPN_PORT=$(echo "$REMOTE_LINE" | awk '{print $3}')

# Protocol: check remote line first, fall back to proto directive
VPN_PROTO=$(echo "$REMOTE_LINE" | awk '{print $4}')
if [ -z "$VPN_PROTO" ]; then
    VPN_PROTO=$(grep "^proto " "$OVPN" | head -1 | awk '{print $2}')
fi
VPN_PROTO="${VPN_PROTO:-udp}"

if [ -z "$VPN_HOST" ] || [ -z "$VPN_PORT" ]; then
    echo "ERROR: Could not parse 'remote' line from $OVPN"
    echo "       Expected format: remote <host> <port> [proto]"
    exit 1
fi

# Resolve hostname to IP if needed
if [[ "$VPN_HOST" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    VPN_IP="$VPN_HOST"
else
    VPN_IP=$(getent hosts "$VPN_HOST" | awk '{print $1}' | head -1)
    if [ -z "$VPN_IP" ]; then
        echo "ERROR: Could not resolve VPN hostname: $VPN_HOST"
        echo "       Check DNS is working before applying the kill switch."
        exit 1
    fi
    echo "Resolved $VPN_HOST -> $VPN_IP"
fi

echo "VPN server: $VPN_IP  port: $VPN_PORT  proto: $VPN_PROTO"

# --- Apply base state first (clean slate) ---
bash "$SCRIPT_DIR/ufw_base.sh"

# --- Add kill switch rules on top ---

# Block all outgoing by default
ufw default deny outgoing

# Allow OpenVPN to reach the VPN server
ufw allow out to "$VPN_IP" port "$VPN_PORT" proto "$VPN_PROTO" comment 'VPN server'

# Allow all traffic through the VPN tunnel
ufw allow out on tun0 comment 'VPN tunnel'

# Allow outgoing to LAN on both interfaces (Plex, SSH responses, web UI)
ufw allow out on eth0  to 10.0.0.0/24 comment 'LAN'
ufw allow out on wlan0 to 10.0.0.0/24 comment 'LAN'

echo ""
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Kill switch active."
echo "  Allowed outgoing: tun0 (all), $VPN_IP:$VPN_PORT/$VPN_PROTO (VPN server), 10.0.0.0/24 (LAN)"
echo "  Everything else: BLOCKED"
echo ""
echo "  Next steps:"
echo "    1. Start OpenVPN:   sudo openvpn --config $OVPN --daemon"
echo "    2. Start monitor:   ./checkip.sh <your_home_ip>"
echo "    3. To recover:      sudo bash $SCRIPT_DIR/ufw_base.sh"
