#!/bin/bash

xHOME="/home/pi/MyPiFiles/vpn/"

echo ""
echo "... Stopping Deluge Web Server"

SERVICE="deluge-web"
if pgrep -x "$SERVICE" >/dev/null
then
    echo "... $SERVICE is running"
    killall $SERVICE
    echo "... $SERVICE has been stopped."
    sleep 2
else
    echo "... $SERVICE is not running"
fi
sleep 3

echo ""
echo "... Stopping Deluge Server"
SERVICE="deluged"
if pgrep -x "$SERVICE" >/dev/null
then
    echo "... $SERVICE is running"
    echo ""
    xDELUGE="$(deluge-console "connect 127.0.0.1:58846 ; pause * ; halt ; quit")"
    echo "... ${xDELUGE}"
    sleep 2
else
    echo "... $SERVICE is not running"
fi
sleep 3

echo ""
echo "... Stopping OpenVPN Server"
SERVICE="openvpn"
if pgrep -x "$SERVICE" >/dev/null
then
    echo "... $SERVICE is running"
    killall $SERVICE
    echo "... $SERVICE has been stopped."
    sleep 2
else
    echo "... $SERVICE is not running"
fi
sleep 5

echo ""
echo "... Stopping checkip script"
SERVICE="checkip.sh"
if pgrep -x "$SERVICE" >/dev/null
then
    echo "... $SERVICE is running"
    killall $SERVICE
    echo "... $SERVICE has been stopped."
    sleep 2
else
    echo "... $SERVICE is not running"
fi
sleep 5
screen -S "checkip" -p 0 -X quit > /dev/null
echo ""
echo ""
sleep 2
