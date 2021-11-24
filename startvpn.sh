#!/bin/bash

clear
echo ""
echo "VPN Start Script"
echo ""

xHOME="/home/pi/MyPiFiles/vpn/"
xSTOPFILE=$xHOME"stopvpn.sh"
xTEMPHOME=$xHOME"temp/"
xLOGFILE=$xTEMPHOME"openvpn.log"
xVPNHOME="/etc/openvpn/client/"
xUSERPASS=$xTEMPHOME"openvpncode.txt"
xPyFILE=$xHOME"vpn_active.py"
xCONFIGFILE="/etc/openvpn/client/vpngate_public-vpn-45.opengw.net_udp_1195.ovpn"
xSUCCESS="TRUE"

read -p "Which VPN Service (2 = CA, q = quit): " VPNSERVICE

while [ $VPNSERVICE != "q" ]
do
  echo ""
  echo "Stopping Deluge and VPN..."
  $xSTOPFILE
  rm -rf $xLOGFILE
  if [ $VPNSERVICE == "2" ];
    then
        echo ""
        echo "VPN CA Service"
        echo ""
        echo "Downloading OVPN files..."
        cd $xTEMPHOME
        echo "Changing directory..."
        cd /etc/openvpn
        echo ""
        echo "Starting VPN..."
        echo ""
        echo $xCONFIGFILE
        echo $xUSERPASS
        echo $xLOGFILE
        sleep 1
        sudo openvpn --config $xCONFIGFILE --log $xLOGFILE --daemon
        sleep 7
        echo ""
        echo "View log"
        echo ""
        sudo tail $xLOGFILE
        echo ""
        read -p "Has it started? [ y = yes, n = no, f = failed ] " iStart
        while [ $iStart == "n" ]
        do
          echo ""
          for load in $(seq 10 -1 0); do
             echo -ne "Check again in $load seconds...\r"
             sleep 1
          done
          echo ""
          echo "Showing tail of log..."
          echo ""
          sudo tail $xLOGFILE
          echo ""
          read -p "Has it started? [ y = yes, n = no, f = failed ] " iStart
        done
  fi
  read -p "Which VPN Service (2 = CA, q = quit): " VPNSERVICE
done

if [[ $iStart == "y" && $VPNSERVICE == "q" ]];
  then
     echo ""
     echo "Testing VPN..."
     active=$(python3 $xPyFILE)
     echo "VPN test complete. Result: " $active
     if [ "$active" == "secure" ];
       then echo ""
            echo "Starting Deluge Server"
            # deluged
            qbittorrent-nox &
            sleep 2
            echo ""
            echo "Starting Deluge Web Server"
            echo ""
            # deluge-web &
            # sleep 2
       else echo ""
            echo "Deluge not started."
            echo ""
     fi
fi
echo ""
echo "FINISHED"
echo ""
