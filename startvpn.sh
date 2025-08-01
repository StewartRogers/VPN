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
read -p "Do you want to check if all software is installed? (Y/N) " SWCHECK
if [[ "${SWCHECK,,}" == "y" ]]
then
  echo "Installing required software..."
  echo "This install qbittorrent-nox and if you have never run that before"
  echo "you must run it manually first to accept the disclaimer"
  sudo apt-get -qq update
  sudo apt-get install -y -qq qbittorrent-nox openvpn screen ufw
fi
#
# Get OVPN File
#
read -p "Do you want to download a new OVPN file? (Y/N) " GETOVPN

if [[ "${GETOVPN,,}" == "y" ]]
then
  rm *.ovpn
  read -p "Paste in a URL to download OVPN file: " OVPNURL
  curl -s -O $OVPNURL
else
  echo ""
fi

# Check if any .ovpn file exists
if ! ls *.ovpn 1> /dev/null 2>&1; then
  echo -e "Error: No .ovpn file found. Aborting script.\n\n"
  exit 1
fi

for XFILE in `eval ls *.ovpn`
  do
    sudo cp $XFILE $XVPNCHOME
  done
XCONFIGFILE=$XVPNCHOME$XFILE

#
# Start VPN service
#


# Ask user if they want to make a UFW call for a specific port
read -p "Do you want to allow a port through UFW? (Y/N) " UFWCONFIRM
if [[ "${UFWCONFIRM,,}" == "y" ]]; then
    read -p "Enter the port number you want to allow: " UFWPORT
    read -p "Enter the protocol (tcp/udp): " UFWPROTO
    echo ""
    echo "... Configuring UFW rule for port $UFWPORT/$UFWPROTO"
    sudo ufw allow $UFWPORT/$UFWPROTO > /dev/null
    echo "... UFW rule applied for $UFWPORT/$UFWPROTO"
    echo ""
fi

# Start VPN service
while [ $VPNSERVICE != "q" ]
do
  if [ $VPNSERVICE == "1" ];
    then
        echo ""
        echo "... Getting organized"
        cd $XVPNHOME
        sudo rm -rf $XVPNLOGFILE
        echo "... Starting VPN"
        echo "... CONFIGFILE: " $XCONFIGFILE
        sudo openvpn --config $XCONFIGFILE --log $XVPNLOGFILE --proto tcp --port 443 --auth SHA256 --data-ciphers-fallback 'AES-256-CBC' --data-ciphers 'AES-256-CBC' --tls-client --tls-version-min 1.2 --auth-nocache --mssfix 1300 --mute-replay-warnings --verb 5 --daemon
        sleep 7
        echo "... Viewing log"
        echo ""
        sudo tail $XVPNLOGFILE
        echo ""
        read -p "Has it started? [ y = yes, n = no, f = failed ] " iStart
        while [[ "${iStart,,}" == "n" ]]
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
  if [[ "${iStart,,}" == "y" ]];
     then
       VPNSERVICE="q"
     else
       read -p "Type 'q' to quit: " VPNSERVICE
  fi
done

if [[ "${iStart,,}" == "y" && $VPNSERVICE == "q" ]];
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
