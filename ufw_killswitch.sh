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

# Load config (for LAN_CIDRS overrides). Mirrors checkip.sh's search order.
if [ -f "$HOME/.vpn_config.conf" ]; then
    source "$HOME/.vpn_config.conf"
elif [ -f "$SCRIPT_DIR/vpn_config.conf" ]; then
    source "$SCRIPT_DIR/vpn_config.conf"
fi

if [ "$EUID" -ne 0 ]; then
    echo "Must be run as root: sudo bash $0"
    exit 1
fi

# --- Find and parse .ovpn config ---
# Newest by mtime, matching startvpn.sh (ls -t) and monitor.py's
# _openvpn_start(). This used to be a plain `ls | head -1` — alphabetical — so
# with more than one config present the firewall whitelisted one server's
# endpoint while OpenVPN dialled another. The tunnel then could not come up,
# and the wrong endpoint sat allowed out for the duration.
OVPN=$(ls -t /etc/openvpn/client/*.ovpn 2>/dev/null | head -1)
if [ -z "$OVPN" ]; then
    echo "ERROR: No .ovpn file found in /etc/openvpn/client/"
    exit 1
fi

# Strip carriage returns - .ovpn files are often created on Windows
REMOTE_LINE=$(grep "^remote " "$OVPN" | head -1 | tr -d '\r')

VPN_HOST=$(echo "$REMOTE_LINE" | awk '{print $2}' | tr -d '\r')
VPN_PORT=$(echo "$REMOTE_LINE" | awk '{print $3}' | tr -d '\r')

# Protocol: check remote line first, fall back to proto directive
VPN_PROTO=$(echo "$REMOTE_LINE" | awk '{print $4}' | tr -d '\r')
if [ -z "$VPN_PROTO" ]; then
    VPN_PROTO=$(grep "^proto " "$OVPN" | head -1 | awk '{print $2}' | tr -d '\r')
fi
VPN_PROTO="${VPN_PROTO:-udp}"

if [ -z "$VPN_HOST" ] || [ -z "$VPN_PORT" ]; then
    echo "ERROR: Could not parse 'remote' line from $OVPN"
    echo "       Expected format: remote <host> <port> [proto]"
    exit 1
fi

if ! [[ "$VPN_PORT" =~ ^[0-9]+$ ]]; then
    echo "ERROR: Parsed port is not numeric: '$VPN_PORT'"
    echo "       The .ovpn file may have unexpected formatting."
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
fi

echo "  Config:     $(basename "$OVPN")"
echo "  VPN server: $VPN_IP ($VPN_HOST)"
echo "  Port/Proto: $VPN_PORT/$VPN_PROTO"

# --- Apply base state first (clean slate) ---
#
# UFW_OUT_POLICY=deny makes ufw_base.sh set the deny-outgoing default BEFORE it
# enables ufw. Previously the base script enabled ufw with allow-outgoing and
# this script flipped the policy on the next line, leaving ufw live and
# outgoing unrestricted in between — a fail-open window on every single kill
# switch application, including every reconnect.
UFW_OUT_POLICY=deny bash "$SCRIPT_DIR/ufw_base.sh" > /dev/null 2>&1

# --- Add kill switch rules on top ---
# Outgoing is already denied by default at this point, so the window between
# here and the last rule is a brief outage, not a brief exposure.
ufw allow out to "$VPN_IP" port "$VPN_PORT" proto "$VPN_PROTO" comment 'VPN server' > /dev/null 2>&1
ufw allow out on tun0 comment 'VPN tunnel'                                         > /dev/null 2>&1
ufw allow out on eth0  to any port 53 comment 'DNS'                                > /dev/null 2>&1
ufw allow out on wlan0 to any port 53 comment 'DNS'                                > /dev/null 2>&1

# LAN ranges to allow on the physical interfaces. Was hardcoded to 10.0.0.0/24,
# which is narrower than what INSTALL.md documents ("192.168.x.x, 10.x.x.x,
# 172.16.x.x") and doesn't cover every RFC1918 network. Override via
# LAN_CIDRS="10.1.2.0/24 192.168.1.0/24" (space-separated) in vpn_config.conf
# if you want something narrower than the full private-address space.
LAN_CIDRS="${LAN_CIDRS:-10.0.0.0/8 172.16.0.0/12 192.168.0.0/16}"
for cidr in $LAN_CIDRS; do
    ufw allow out on eth0  to "$cidr" comment 'LAN' > /dev/null 2>&1
    ufw allow out on wlan0 to "$cidr" comment 'LAN' > /dev/null 2>&1
done

echo "  Status:     ACTIVE - all outgoing blocked except VPN tunnel and LAN"
