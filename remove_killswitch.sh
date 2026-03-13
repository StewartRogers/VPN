#!/bin/bash
#
# remove_killswitch.sh — Emergency kill switch removal
#
# Run this from the terminal if the kill switch has locked out your internet
# and the web app is not accessible.
#
# Usage:
#   ./remove_killswitch.sh           # restore from backup if available, else reset
#   ./remove_killswitch.sh --reset   # always flush and reset to ACCEPT (ignore backup)
#

BACKUP="$HOME/.vpn_backups/iptables.backup"
IP6BACKUP="$HOME/.vpn_backups/ip6tables.backup"
FORCE_RESET=false

if [[ "${1:-}" == "--reset" ]]; then
    FORCE_RESET=true
fi

echo ""
echo "VPN Kill Switch Removal"
echo ""

# ---- IPv4 ----
if [ "$FORCE_RESET" = false ] && [ -f "$BACKUP" ]; then
    echo "Found iptables backup: $BACKUP"
    echo "Restoring original IPv4 rules..."
    if sudo iptables-restore < "$BACKUP"; then
        rm -f "$BACKUP"
        echo "IPv4 restored. Backup deleted."
    else
        echo "Restore failed — falling back to manual reset"
        FORCE_RESET=true
    fi
fi

if [ "$FORCE_RESET" = true ] || [ ! -f "$BACKUP" ]; then
    if [ ! -f "$BACKUP" ]; then
        echo "No IPv4 backup found at $BACKUP"
    fi
    echo "Flushing IPv4 OUTPUT chain and setting policy to ACCEPT..."
    sudo iptables -F OUTPUT
    sudo iptables -P OUTPUT ACCEPT
    echo "Done."
fi

# ---- IPv6 ----
if [ "$FORCE_RESET" = false ] && [ -f "$IP6BACKUP" ]; then
    echo "Found ip6tables backup: $IP6BACKUP"
    echo "Restoring original IPv6 rules..."
    if sudo ip6tables-restore < "$IP6BACKUP"; then
        rm -f "$IP6BACKUP"
        echo "IPv6 restored. Backup deleted."
    else
        echo "ip6tables restore failed — falling back to manual reset"
        sudo ip6tables -F OUTPUT
        sudo ip6tables -P OUTPUT ACCEPT
    fi
elif [ "$FORCE_RESET" = true ] || [ ! -f "$IP6BACKUP" ]; then
    echo "Flushing IPv6 OUTPUT chain and setting policy to ACCEPT..."
    sudo ip6tables -F OUTPUT
    sudo ip6tables -P OUTPUT ACCEPT
    echo "Done."
fi

# Re-enable IPv6 in case it was disabled via sysctl
echo "Re-enabling IPv6..."
sudo sysctl -w net.ipv6.conf.all.disable_ipv6=0 > /dev/null 2>&1 || true
sudo sysctl -w net.ipv6.conf.default.disable_ipv6=0 > /dev/null 2>&1 || true

# Restore resolv.conf if it was locked
DNS_BACKUP="$HOME/.vpn_backups/resolv.conf.backup"
if [ -f "$DNS_BACKUP" ]; then
    echo "Restoring DNS configuration..."
    sudo chattr -i /etc/resolv.conf 2>/dev/null || true
    sudo mv "$DNS_BACKUP" /etc/resolv.conf
    echo "DNS restored."
else
    # Just unlock it in case chattr +i is still set
    sudo chattr -i /etc/resolv.conf 2>/dev/null || true
fi

echo ""
echo "Kill switch removed. Internet access should be restored."
echo "You can verify with: curl -s https://ipinfo.io/ip"
echo ""
