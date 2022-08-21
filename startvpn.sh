#!/bin/bash
#
# Original Author: Stewart Rogers
# This licensed under the MIT License
# A short and simple permissive license with conditions only requiring
# preservation of copyright and license notices. Licensed works, modifications,
# and larger works may be distributed under different terms and without source code.
#

clear
echo ""
echo "VPN Start Script"
echo ""

#
# VARIABLES
#
XHOME=$PWD"/"
YLOGFILE=$XHOME"checkvpn.log"
XVPNHOME="/etc/openvpn/"
XVPNCHOME="/etc/openvpn/client/"
XVPNLOGFILE="/var/log/openvpn.log"
XPYFILE=$XHOME"vpn_active.py"
XSUCCESS="TRUE"
VPNSERVICE=1

#
# Get current external IP address for future test
#
YHOMEIP=$(curl -s https://ipinfo.io/ip)
echo "External IP: "$YHOMEIP
echo ""

#
# Installing required software
#
echo "Installing required software..."
echo "This install qbittorrent-nox and if you have never run that before"
echo "you must run it manually first to accept the disclaimer"
sudo apt-get -qq update
sudo apt-get install -y -qq qbittorrent-nox openvpn
echo ""
echo ""

#
# Get OVPN File
#
read -p "Do you want to download a new OVPN file? (Y/N) " GETOVPN

if [ $GETOVPN == "y" ] || [ $GETOVPN == "Y" ]
then
  read -p "Paste in a URL to download OVPN file: " OVPNURL
  curl -s -O $OVPNURL
else
  echo ""
fi

for XFILE in `eval ls *.ovpn`
  do
    sudo cp $XFILE $XVPNCHOME
  done
XCONFIGFILE=$XVPNCHOME$XFILE

#
# Start VPN service
#
while [ $VPNSERVICE != "q" ]
do
  if [ $VPNSERVICE == "1" ];
    then
        echo ""
        echo "... Getting organized"
        sudo ufw allow out 1195/udp > /dev/null
        sudo ufw allow 8080/tcp > /dev/null
        sudo ufw allow out on tun0 from any to any > /dev/null
        cd $XVPNHOME
        sudo rm -rf $XVPNLOGFILE
        echo "... Starting VPN"
        sudo openvpn --auth-nocache --config $XCONFIGFILE --log $XVPNLOGFILE --daemon --data-ciphers-fallback 'AES-128-CBC' --data-ciphers 'AES-128-CBC' --verb 6
        sleep 7
        echo "... Viewing log"
        echo ""
        sudo tail $XVPNLOGFILE
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
          sudo tail $XVPNLOGFILE
          echo ""
          read -p "Has it started? [ y = yes, n = no, f = failed ] " iStart
        done
  fi
  if [[ $iStart == "y" ]];
     then
       VPNSERVICE="q"
     else
       read -p "Type 'q' to quit: " VPNSERVICE
  fi
done

if [[ $iStart == "y" && $VPNSERVICE == "q" ]];
  then
     echo ""
     echo "... Testing VPN"
     active=$(python3 $XPYFILE $YHOMEIP)
     echo "... VPN test result:" $active
     if [ "$active" == "secure" ];
       then echo "... Starting Torrent Server"
            qbittorrent-nox &>$XHOME/qbit.log &
            sleep 2
       else echo ""
            echo "Torrent Server not started."
            echo ""
     fi
fi

if [ "$active" == "secure" ];
  then
    YCHECKFILE=$XHOME"checkip.sh "$YHOMEIP
    echo "... Checking IP address"
    cd $XHOME
    ./checkip.sh $YHOMEIP &
    echo "... See progress: tail -f ${YLOGFILE}"
    echo ""
fi
