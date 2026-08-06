#!/bin/bash
#
# remove_killswitch.sh — Emergency kill switch removal
#
# Run this from the terminal if the kill switch has locked out your internet
# and the web app is not accessible.
#
# The kill switch is UFW-based, so this drives UFW. Earlier versions flushed
# the raw iptables OUTPUT chain instead, which removed UFW's jump rules so
# nothing filtered at all — while 'ufw status' still reported "deny (outgoing)"
# because that comes from UFW's own config files. Every kill-switch check in
# this project reads that string, so the machine reported itself protected
# while it was wide open. Never flush the OUTPUT chain to disable the switch.
#
# Usage:
#   ./remove_killswitch.sh
#

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
BACKUP_DIR="$HOME/.vpn_backups"

echo ""
echo "VPN Kill Switch Removal"
echo ""

# ---- Firewall ----
if [ -f "$SCRIPT_DIR/ufw_base.sh" ]; then
    echo "Restoring UFW base state (outgoing unrestricted)..."
    if sudo bash "$SCRIPT_DIR/ufw_base.sh"; then
        echo "UFW restored."
    else
        echo "ufw_base.sh failed — disabling UFW outright as a last resort."
        sudo ufw --force disable
    fi
else
    echo "ufw_base.sh not found — disabling UFW outright."
    sudo ufw --force disable
fi

# ---- IPv6 ----
# Re-enable IPv6 in case it was disabled via sysctl during VPN start
echo "Re-enabling IPv6..."
sudo sysctl -w net.ipv6.conf.all.disable_ipv6=0 > /dev/null 2>&1 || true
sudo sysctl -w net.ipv6.conf.default.disable_ipv6=0 > /dev/null 2>&1 || true

# ---- DNS ----
# The web app pins /etc/resolv.conf to Cloudflare and sets chattr +i on it.
DNS_BACKUP="$BACKUP_DIR/resolv.conf.backup"
sudo chattr -i /etc/resolv.conf 2>/dev/null || true
if [ -f "$DNS_BACKUP" ]; then
    echo "Restoring DNS configuration..."
    sudo mv "$DNS_BACKUP" /etc/resolv.conf
    echo "DNS restored."
else
    echo "No DNS backup found — left /etc/resolv.conf as-is (now unlocked)."
fi

# ---- Verify ----
echo ""
if sudo ufw status verbose 2>/dev/null | grep -q "deny (outgoing)"; then
    echo "WARNING: UFW still reports 'deny (outgoing)' — the kill switch is NOT removed."
    echo "         Try: sudo ufw --force disable"
else
    echo "Kill switch removed. Internet access should be restored."
fi
echo "Verify with: curl -s https://ipinfo.io/ip"
echo ""
