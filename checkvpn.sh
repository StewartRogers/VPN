#!/bin/bash

xHOME="/home/pi/MyPiFiles/vpn/"
xCHECKFILE=$xHOME"checkip.sh"
xLOGFILE=$xHOME"checkvpn.log"

rm -rf $xLOGFILE

clear
echo ""
echo ""
echo "Checking IP address..."
echo ""
sleep 2
screen -d -m -S checkip $xCHECKFILE
sleep 2
echo "See progress... tail ${xLOGFILE}"
echo ""
echo "FINISHED"
echo ""
