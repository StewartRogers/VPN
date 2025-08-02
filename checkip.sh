#!/bin/bash
# Copyright (c) 2022-2025 Stewart Rogers
# SPDX-License-Identifier: MIT
#
# Original Author: Stewart Rogers
# Enhanced for faster VPN disconnect detection
# This licensed under the MIT License
# A short and simple permissive license with conditions only requiring
# preservation of copyright and license notices. Licensed works, modifications,
# and larger works may be distributed under different terms and without source code.
#
#
# VARIABLES
#
XIP_HOME=$PWD"/"
XIP_PYFILE=$XIP_HOME"vpn_active.py"
XIP_LOGFILE=$XIP_HOME"checkvpn.log"
XIP_STOPFILE=$XIP_HOME"stopvpn.sh --shutdown-only"
YIP_HOMEIP=$1
#
# New variables for enhanced monitoring
#
FAST_CHECK_INTERVAL=1    # Quick process/interface checks every 2 seconds
IP_CHECK_INTERVAL=5     # Full IP check every 10 seconds
LAST_IP_CHECK=0
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
# Main
#
echo ""
echo "$(date): Starting enhanced VPN monitoring..."
echo "$(date): Fast checks every $FAST_CHECK_INTERVAL seconds"
echo "$(date): Full IP checks every $IP_CHECK_INTERVAL seconds"
echo ""

active="secure"
firstrun="y"
echo ""

while [[ "$active" == "secure" ]]; do
    current_time=$(date +%s)
    
    # Always do fast checks (process and interface)
    if ! check_openvpn_process; then
        active="notsecure"
        break
    fi
    
    if ! check_vpn_interface; then
        active="notsecure"
        break
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
            active="notsecure"
            break
        fi
        
        LAST_IP_CHECK=$current_time
        firstrun="n"
        echo "$(date): VPN status confirmed secure"
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
    echo "$(date): VPN COMPROMISED - Initiating emergency shutdown..."
    echo "$(date): Stopping Torrent Server and VPN..."
    $XIP_STOPFILE
    echo "$(date): Emergency shutdown complete"
else
    echo "$(date): Monitoring stopped normally"
fi
echo ""
echo "$(date): FINISHED"
echo ""