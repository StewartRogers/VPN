#!/bin/bash
# Copyright (c) 2022-2026 Stewart Rogers
# SPDX-License-Identifier: MIT
#
# remove_killswitch.sh — Emergency kill switch removal
#
# Run this from the terminal if the kill switch has locked out your internet
# and the web app is not reachable.
#
# Usage:
#   ./remove_killswitch.sh             restore UFW base state (normal recovery)
#   ./remove_killswitch.sh --disable   last resort: disable UFW entirely
#
# This script used to be entirely iptables-based: it restored
# ~/.vpn_backups/iptables.backup, a file nothing has created since this project
# moved to UFW, and otherwise flushed the iptables OUTPUT chain. That happened
# to restore connectivity as a side effect — flushing OUTPUT drops UFW's jump
# rules — but UFW itself stayed enabled and reasserted every rule on the next
# reload or reboot. It never called ufw_base.sh, so the documented recovery path
# did not actually recover anything.
#

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

FORCE_DISABLE=false
[ "${1:-}" = "--disable" ] && FORCE_DISABLE=true

# Backups are written under the invoking user's home, so resolve that rather
# than $HOME, which is /root under sudo.
if [ -n "${SUDO_USER:-}" ]; then
    REAL_HOME=$(getent passwd "$SUDO_USER" | cut -d: -f6)
else
    REAL_HOME="$HOME"
fi
DNS_BACKUP="$REAL_HOME/.vpn_backups/resolv.conf.backup"

echo ""
echo "VPN Kill Switch Removal"
echo ""

# --- 1. Stop the torrent client first ---
# This script exists to reopen unrestricted outbound traffic. Anything still
# torrenting when that happens would egress on the ISP link.
if pgrep -f "qbittorrent-nox" > /dev/null; then
    echo "Stopping qbittorrent-nox before reopening outbound traffic..."
    sudo pkill -f "qbittorrent-nox"
    for _ in $(seq 1 10); do
        pgrep -f "qbittorrent-nox" > /dev/null || break
        sleep 0.5
    done
    pgrep -f "qbittorrent-nox" > /dev/null && sudo pkill -9 -f "qbittorrent-nox"
    echo "Stopped."
else
    echo "qbittorrent-nox is not running."
fi
echo ""

# --- 2. Firewall ---
if [ "$FORCE_DISABLE" = true ]; then
    echo "Disabling UFW entirely (--disable)..."
    if sudo ufw --force disable; then
        echo "UFW disabled. The host has no firewall until you re-enable it:"
        echo "  sudo bash $SCRIPT_DIR/ufw_base.sh"
    else
        echo "WARNING: 'ufw --force disable' failed."
    fi
else
    echo "Restoring UFW base state..."
    if sudo bash "$SCRIPT_DIR/ufw_base.sh"; then
        echo "UFW base state restored - outgoing unrestricted."
    else
        echo "WARNING: ufw_base.sh failed. Falling back to disabling UFW..."
        if sudo ufw --force disable; then
            echo "UFW disabled - internet should work, but the host is unfirewalled."
            echo "Re-apply the base state when you can:"
            echo "  sudo bash $SCRIPT_DIR/ufw_base.sh"
        else
            echo "ERROR: could not disable UFW either. Check 'sudo ufw status'."
        fi
    fi
fi
echo ""

# --- 3. DNS ---
if [ -f "$DNS_BACKUP" ]; then
    echo "Restoring DNS configuration..."
    sudo chattr -i /etc/resolv.conf 2>/dev/null
    if sudo mv "$DNS_BACKUP" /etc/resolv.conf; then
        echo "DNS restored."
    else
        echo "WARNING: could not restore DNS - backup is still at $DNS_BACKUP"
    fi
else
    # Clear the immutable bit even with no backup, so resolv.conf is not left
    # locked against every other tool on the system.
    sudo chattr -i /etc/resolv.conf 2>/dev/null
    echo "No DNS backup found - cleared the immutable bit on /etc/resolv.conf."
fi
echo ""

# --- 4. IPv6 ---
echo "Re-enabling IPv6..."
sudo sysctl -w net.ipv6.conf.all.disable_ipv6=0     > /dev/null 2>&1
sudo sysctl -w net.ipv6.conf.default.disable_ipv6=0 > /dev/null 2>&1

echo ""
echo "Kill switch removed. Internet access should be restored."
echo "Verify with:  curl -s https://api.ipify.org"
echo ""
