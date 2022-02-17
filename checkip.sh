#!/bin/bash
#
# Original Author: Stewart Rogers
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
XIP_STOPFILE=$XIP_HOME"stopvpn.sh"
YIP_HOMEIP=$1

#
# redirect stdout/stderr to a file
#
rm -rf $XIP_LOGFILE
exec >$XIP_LOGFILE 2>&1

#
# Main
#
echo ""
echo "Checking IP address for VPN..."
echo ""

active="secure"
firstrun="y"

echo ""
while [[ "$active" == "secure" ]];
  do
    echo ""
    if [[ "$firstrun" == "n" ]];
      then
           echo ""
           for load in $(seq 5 -1 0); do
              echo -ne "Wait $load seconds ...\r"
              sleep 1
           done
           echo ""
    fi
    echo "Testing VPN..."
    XNOW=$(date)
    active=$(python3 $XIP_PYFILE $YIP_HOMEIP)
    echo "... VPN test complete. Result:" $active
    firstrun="n"
  done
echo ""
if [ "$active" != "secure" ] ;
  then
    echo "Stopping Torrent Server and VPN..."
    $XIP_STOPFILE
fi
echo ""
echo "FINISHED"
echo ""
