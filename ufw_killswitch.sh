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

# .ovpn files routinely use udp4/udp6/tcp4/tcp-client/tcp4-client, none of which
# ufw accepts. An unnormalised value makes 'ufw allow out ... proto' fail, and
# the failure used to be discarded — leaving the VPN server unreachable through
# a kill switch that still reported success.
case "$VPN_PROTO" in
    udp*) VPN_PROTO="udp" ;;
    tcp*) VPN_PROTO="tcp" ;;
    *)
        echo "ERROR: Unrecognised protocol in $OVPN: '$VPN_PROTO'"
        exit 1
        ;;
esac

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
    # ahostsv4, not hosts: 'getent hosts' can return the AAAA record first, and
    # an IPv6 literal here produces a rule that never matches the tunnel.
    VPN_IP=$(getent ahostsv4 "$VPN_HOST" | awk '{print $1}' | head -1)
    if [ -z "$VPN_IP" ]; then
        echo "ERROR: Could not resolve VPN hostname: $VPN_HOST"
        echo "       Check DNS is working before applying the kill switch."
        exit 1
    fi
fi

if ! [[ "$VPN_IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "ERROR: Resolved VPN address is not IPv4: '$VPN_IP'"
    exit 1
fi

echo "  Config:     $(basename "$OVPN")"
echo "  VPN server: $VPN_IP ($VPN_HOST)"
echo "  Port/Proto: $VPN_PORT/$VPN_PROTO"

# LAN subnets, auto-detected from the physical interfaces' link routes.
# This used to be a hardcoded 10.0.0.0/24, which severed local access — SSH and
# the web UI included — on any 192.168.x or 172.16.x network.
# Override by exporting LAN_SUBNETS="192.168.1.0/24 10.8.0.0/24".
if [ -z "$LAN_SUBNETS" ]; then
    LAN_SUBNETS=$(ip -o -f inet route show scope link 2>/dev/null \
        | awk '$3 !~ /^(lo|tun)/ {print $1}' | sort -u)
fi
if [ -z "$LAN_SUBNETS" ]; then
    echo "WARNING: Could not detect a LAN subnet - local access may be blocked."
fi

# DNS permitted on the physical interfaces. OpenVPN must resolve its 'remote'
# hostname before tun0 exists, so some DNS has to be allowed pre-tunnel.
#
# Default is unrestricted port 53, because narrowing it to the resolvers listed
# in /etc/resolv.conf breaks systemd-resolved: resolv.conf holds the 127.0.0.53
# stub, not the upstream the stub actually forwards to, so every lookup is
# dropped and the monitor reports "could not reach IP services" forever.
#
# To narrow it anyway, name the real upstream resolvers explicitly:
#   sudo DNS_SERVERS="192.168.1.1 1.1.1.1" bash ufw_killswitch.sh
# Note this only affects queries leaving on eth0/wlan0 -- once tun0 is up, DNS
# goes through the tunnel under the 'VPN tunnel' rule regardless.

FAILED=0
run_ufw() {
    if ! ufw "$@" > /dev/null 2>&1; then
        echo "  ERROR: 'ufw $*' failed"
        FAILED=1
    fi
}

# --- Clean slate, enabled with outgoing ALREADY denied ---
# Passing OUTGOING_POLICY=deny matters: ufw_base.sh's default enables the
# firewall with 'allow outgoing', which left a window of unrestricted egress
# between 'ufw enable' and the deny rule below.
if ! OUTGOING_POLICY=deny bash "$SCRIPT_DIR/ufw_base.sh" > /dev/null 2>&1; then
    echo "ERROR: ufw_base.sh failed - firewall is in an unknown state."
    exit 1
fi

# --- Add kill switch rules on top ---
run_ufw default deny outgoing
run_ufw allow out to "$VPN_IP" port "$VPN_PORT" proto "$VPN_PROTO" comment 'VPN server'
run_ufw allow out on tun0 comment 'VPN tunnel'
for iface in eth0 wlan0; do
    for subnet in $LAN_SUBNETS; do
        run_ufw allow out on "$iface" to "$subnet" comment 'LAN'
    done
    # Pre-tunnel DNS (see the DNS_SERVERS note above).
    if [ -n "$DNS_SERVERS" ]; then
        for resolver in $DNS_SERVERS; do
            run_ufw allow out on "$iface" to "$resolver" port 53 comment 'DNS'
        done
    else
        run_ufw allow out on "$iface" to any port 53 comment 'DNS'
    fi
done

# --- Verify the switch is genuinely in place before reporting success ---
if [ "$FAILED" -ne 0 ]; then
    echo "  Status:     FAILED - one or more rules were rejected. Run 'sudo ufw status verbose'."
    exit 1
fi
if ! ufw status verbose 2>/dev/null | grep -q "deny (outgoing)"; then
    echo "  Status:     FAILED - UFW does not report 'deny (outgoing)'."
    exit 1
fi

echo "  LAN:        ${LAN_SUBNETS:-none detected}"
echo "  DNS:        ${DNS_SERVERS:-any (port 53)}"
echo "  Status:     ACTIVE - all outgoing blocked except VPN tunnel and LAN"
