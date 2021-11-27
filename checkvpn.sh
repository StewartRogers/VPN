#!/bin/bash

YHOME="/home/pi/MyPiFiles/vpn/"
YCHECKFILE=$YHOME"checkip.sh"
YLOGFILE=$YHOME"checkvpn.log"

rm -rf $YLOGFILE

clear
echo ""
echo "Checking IP address..."
screen -d -m -S checkip $YCHECKFILE
echo "See progress... tail -f ${YLOGFILE}"
echo "FINISHED"
echo ""
