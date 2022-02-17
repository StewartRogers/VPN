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
SHOME="/home/pi/MyPiFiles/vpn/"
SSERVICE="q"

#
# Stopping torrent server. Change SSERVICE variable from q to use Deluge
#
echo ""
if [[ "$SSERVICE" == "q" ]];
  then
     echo "... Stopping qbittorrent"
     SERVICE="qbittorrent-nox"
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

#
# Stopping Deluge console server. Change SSERVICE variable from q to use Deluge
#
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

#
# Stopping OpenVPN server.
#
echo ""
echo "... Stopping OpenVPN Server"
SERVICE="openvpn"
if pgrep -x "$SERVICE" >/dev/null
then
    echo "... $SERVICE is running"
    sudo killall $SERVICE
    echo "... $SERVICE has been stopped."
else
    echo "... $SERVICE is not running"
fi
sleep 1

#
# Stopping checkip script server.
#
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

#
# END
#
