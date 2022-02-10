#!/bin/bash

clear
echo ""
echo "VPN Start Script"
xHOME=$PWD"/"
xSTOPFILE=$xHOME"stopvpn.sh"
xTEMPHOME=$xHOME"temp/"
if [ -d "$xTEMPHOME" ]
then
    echo ""
else
    mkdir $xTEMPHOME
    echo ""
fi
xLOGFILE=$xTEMPHOME"openvpn.log"
xVPNHOME="/etc/openvpn/"
xVPNCHOME="/etc/openvpn/client/"
xPyFILE=$xHOME"vpn_active.py"
xCONFIGFILE=$xVPNCHOME"vpngate_public-vpn-234.opengw.net_udp_1195.ovpn"
xSUCCESS="TRUE"

YHOMEIP=$(curl -s https://ipinfo.io/ip)
echo "External IP: "$YHOMEIP
echo ""
rm -rf $xLOGFILE

read -p "Which VPN Service (1 = CA, q = quit): " VPNSERVICE

while [ $VPNSERVICE != "q" ]
do
  rm -rf $xLOGFILE
  if [ $VPNSERVICE == "1" ];
    then
        echo ""
        echo "... Changing directory"
        cd $xVPNHOME
        echo "... Starting VPN"
        sudo openvpn --config $xCONFIGFILE --log $xLOGFILE --daemon
        sleep 7
        echo "... Viewing log"
        echo ""
        sudo tail $xLOGFILE
        echo ""
        read -p "Has it started? [ y = yes, n = no, f = failed ] " iStart
        while [ $iStart == "n" ]
        do
          echo ""
          for load in $(seq 10 -1 0); do
             echo -ne "... Check again in $load seconds\r"
             sleep 1
          done
          echo ""
          echo "... Viewing log"
          echo ""
          sudo tail $xLOGFILE
          echo ""
          read -p "Has it started? [ y = yes, n = no, f = failed ] " iStart
        done
  fi
  if [[ $iStart == "y" ]];
     then
       VPNSERVICE="q"
     else
       read -p "Which VPN Service (1 = CA, q = quit): " VPNSERVICE
  fi
done

if [[ $iStart == "y" && $VPNSERVICE == "q" ]];
  then
     echo ""
     echo "... Testing VPN"
     active=$(python3 $xPyFILE $YHOMEIP)
     echo "... VPN test result:" $active
     if [ "$active" == "secure" ];
       then echo "... Starting Torrent Server"
            # deluged
            qbittorrent-nox &>$xHOME/qbit.log &
            sleep 2
            # echo ""
            # echo "Starting Deluge Web Server"
            # echo ""
            # deluge-web &
            # sleep 2
       else echo ""
            echo "Torrent Server not started."
            echo ""
     fi
fi

if [ "$active" == "secure" ];
  then
    cd ~/MyPiFiles/vpn
    YHOME=$PWD"/"
    YCHECKFILE=$YHOME"checkip.sh "$YHOMEIP
    YLOGFILE=$YHOME"checkvpn.log"
    rm -rf $YLOGFILE
    echo "... Checking IP address"
    screen -d -m -S checkip $YCHECKFILE
    echo "... See progress: tail -f ${YLOGFILE}"
    echo ""
fi
