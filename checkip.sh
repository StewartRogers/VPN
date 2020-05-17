#!/bin/bash

clear
echo ""
echo "Checking IP address for VPN..."
echo ""

xHOME="/home/pi/MyPiFiles/vpn/"
xPyFILE=$xHOME"vpn_active.py"
xLOGFILE=$xHOME"checkvpn.log"
xSTOPFILE=$xHOME"stopvpn.sh"

# redirect stdout/stderr to a file
rm -rf $xLOGFILE
exec &> $xLOGFILE

echo $xHOME
echo $xPyFILE 
echo $xLOGFILE

active="secure"
firstrun="y"

echo ""
while [[ "$active" == "secure" ]];
  do
    if [[ $firstrun == "n" ]];
      then 
           echo ""
           for load in $(seq 30 -1 0); do
              echo -ne "Wait $load seconds ...\r"
              sleep 1
           done
           echo ""
    fi
    echo ""
    echo "Testing VPN..."
    sleep 1
    active=$(python3 $xPyFILE)
    sleep 1
    echo "... VPN test complete. Result:" $active
    firstrun="n"
    sleep 1
  done
echo ""
if [ "$active" != "secure" ] ;
then
  echo "Stopping Deluge and VPN..."
  $xSTOPFILE
fi
echo ""
echo "FINISHED"
echo ""
