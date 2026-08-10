#!/bin/bash
# Copyright (c) 2022-2025 Stewart Rogers
# SPDX-License-Identifier: MIT
#
# stop_web.sh — stop the VPN Monitor web app and everything it started.
#
# Order matters, and mirrors stopvpn.sh: the torrent client and OpenVPN are
# stopped BEFORE the firewall is relaxed.
#
# The previous version did only two things — pkill the Flask process, then reset
# UFW. That removed the kill switch while qBittorrent was still running, and
# because qBittorrent is started as a child of Flask, killing Flask orphaned it
# rather than stopping it. The reachable end state was: kill switch off, torrents
# running, no monitor. Killing Flask also skipped the app's own restore_dns() and
# restore_ipv6(), leaving /etc/resolv.conf pinned and flagged immutable.
#
# Usage: bash stop_web.sh
#

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

# The web app writes its backups under the invoking user's home, so resolve that
# rather than $HOME, which is /root when this is run under sudo.
if [ -n "${SUDO_USER:-}" ]; then
    REAL_HOME=$(getent passwd "$SUDO_USER" | cut -d: -f6)
else
    REAL_HOME="$HOME"
fi
BACKUP_DIR="${REAL_HOME}/.vpn_backups"

divider() { echo "------------------------------------------------------------"; }

divider
echo "  Shutting down VPN Monitor web app..."
echo ""

# --- 1. Torrent client, before anything relaxes the firewall ---
echo "  [ qbittorrent-nox ]"
if pgrep -f "qbittorrent-nox" > /dev/null; then
    echo "  Stopping qbittorrent-nox"
    sudo pkill -f "qbittorrent-nox"
    for _ in $(seq 1 10); do
        pgrep -f "qbittorrent-nox" > /dev/null || break
        sleep 0.5
    done
    if pgrep -f "qbittorrent-nox" > /dev/null; then
        echo "  Still running - sending SIGKILL"
        sudo pkill -9 -f "qbittorrent-nox"
    fi
    echo "  Stopped."
else
    echo "  Not running."
fi
echo ""

# --- 2. OpenVPN ---
echo "  [ OpenVPN ]"
if pgrep -x openvpn > /dev/null; then
    echo "  Stopping OpenVPN"
    sudo pkill -x openvpn
    sleep 2
    echo "  Stopped."
else
    echo "  Not running."
fi
echo ""

# --- 3. The web app itself ---
echo "  [ Web app ]"
if pgrep -f "webapp/app.py" > /dev/null; then
    echo "  Stopping VPN Monitor web app"
    pkill -f "webapp/app.py"
    sleep 1
    echo "  Stopped."
else
    echo "  Not running."
fi
echo ""

# --- 4. Undo the host changes the web app makes ---
# The app does these in restore_dns() / restore_ipv6() on a clean Stop VPN, but
# those never run when the process is killed, so repeat them here. Both are
# safe to run when nothing was changed.
echo "  [ DNS / IPv6 ]"
DNS_BACKUP="$BACKUP_DIR/resolv.conf.backup"
if [ -f "$DNS_BACKUP" ]; then
    echo "  Restoring /etc/resolv.conf from backup"
    sudo chattr -i /etc/resolv.conf 2>/dev/null
    if sudo mv "$DNS_BACKUP" /etc/resolv.conf; then
        echo "  DNS restored."
    else
        echo "  WARNING: could not restore DNS - check $DNS_BACKUP"
    fi
else
    # Clear the immutable bit even with no backup, so resolv.conf is not left
    # locked against every other tool on the system.
    sudo chattr -i /etc/resolv.conf 2>/dev/null
    echo "  No DNS backup found (nothing to restore)."
fi

sudo sysctl -w net.ipv6.conf.all.disable_ipv6=0     > /dev/null 2>&1
sudo sysctl -w net.ipv6.conf.default.disable_ipv6=0 > /dev/null 2>&1
echo "  IPv6 re-enabled."
echo ""

# --- 5. Firewall LAST, once nothing is left that could leak ---
echo "  [ Firewall ]"
echo "  Resetting UFW to base state..."
if sudo bash "$SCRIPT_DIR/ufw_base.sh"; then
    echo "  UFW base state restored - outgoing unrestricted."
else
    echo "  WARNING: UFW reset failed - run manually: sudo bash $SCRIPT_DIR/ufw_base.sh"
fi

echo ""
divider
echo "  Done."
divider
