#!/bin/bash

# SHOME="/home/pi/MyPiFiles/vpn/"

echo ""

SSERVICE="q"

if [[ "$SSERVICE" == "q" ]];
  then
     echo "... Stopping qbittorrent"
     SERVICE="qbittorrent"
  else
     echo "... Stopping Deluge Web Server"
     SERVICE="deluge-web"
fi

if pgrep -x "$SERVICE" >/dev/null
then
    echo "... $SERVICE is running"
    sudo killall $SERVICE
    echo "... $SERVICE has been stopped."
    sleep 1
else
    echo "... $SERVICE is not running"
fi
sleep 1

if [[ "$SSERVICE" == "q" ]];
  then
     SERVICE="qbittorrent-nox"
  else
     echo ""
     echo "... Stopping Deluge Server"
     SERVICE="deluged"
     if pgrep -x "$SERVICE" >/dev/null
     then
         echo "... $SERVICE is running"
         echo ""
         xDELUGE="$(deluge-console "connect 127.0.0.1:58846 ; pause * ; halt ; quit")"
         echo "... ${xDELUGE}"
         sleep 1
     else
         echo "... $SERVICE is not running"
     fi
fi
echo ""
echo "... Stopping OpenVPN Server"
SERVICE="openvpn"
if pgrep -x "$SERVICE" >/dev/null
then
    echo "... $SERVICE is running"
    sudo killall $SERVICE
    echo "... $SERVICE has been stopped."
    sleep 1
else
    echo "... $SERVICE is not running"
fi
echo ""
echo "... Stopping checkip script"
SERVICE="checkip.sh"
if pgrep -x "$SERVICE" >/dev/null
then
    echo "... $SERVICE is running"
    sudo killall $SERVICE
    echo "... $SERVICE has been stopped."
else
    echo "... $SERVICE is not running"
fi
sleep 1
echo ""
screen -S "checkip" -p 0 -X quit > /dev/null
echo ""
