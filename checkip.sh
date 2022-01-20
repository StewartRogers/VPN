#!/bin/bash

clear
echo ""
echo "Checking IP address for VPN..."
echo ""

XHOME=$PWD"/"
XPYFILE=$XHOME"vpn_active.py"
XLOGFILE=$XHOME"checkvpn.log"
XSTOPFILE=$XHOME"stopvpn.sh"
YHOMEIP=$1

# redirect stdout/stderr to a file
rm -rf $XLOGFILE
exec &> $XLOGFILE

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
    echo ""
    echo "Testing VPN..."
    XNOW=$(date)
    echo $XNOW
    # echo $XPYFILE
    # echo $YHOMEIP
    active=$(python3 $XPYFILE $YHOMEIP)
    echo "... VPN test complete. Result:" $active
    firstrun="n"
  done
echo ""
if [ "$active" != "secure" ] ;
  then
    echo "Stopping Deluge and VPN..."
    $XSTOPFILE
fi
echo ""
echo "FINISHED"
echo ""
