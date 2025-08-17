#!/bin/bash
# Copyright (c) 2022-2025 Stewart Rogers
# SPDX-License-Identifier: MIT
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
while true; do
  read -p "Do you want to check if all software is installed? [y/n]: " SWCHECK
  case "${SWCHECK,,}" in
    y|n) break;;
    *) echo "Please enter 'y' or 'n'.";;
  esac
done

if [[ "${SWCHECK,,}" == "y" ]]; then
  echo "Installing required software..."
  echo "This install qbittorrent-nox and if you have never run that before"
  echo "you must run it manually first to accept the disclaimer"
  sudo apt-get -qq update
  sudo apt-get install -y -qq qbittorrent-nox openvpn screen ufw

  # Ensure python3, pip3, and required Python packages are installed
  if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 not found, installing..."
    sudo apt-get update && sudo apt-get install -y -qq python3
  fi

  if ! command -v pip3 >/dev/null 2>&1; then
    echo "pip3 not found, installing..."
    sudo apt-get install -y -qq python3-pip
  fi

  # Ensure required Python packages are installed
  pip3 install --user --upgrade requests
fi

# ...rest of script continues here, outside the software check loop...

#
# Get OVPN File
#
while true; do
  read -p "Do you want to download a new OVPN file? [y/n]: " GETOVPN
  # Ask user if they want to make a UFW call for a specific port
  while true; do
    read -p "Do you want to allow a port through UFW? [y/n]: " UFWCONFIRM
    case "${UFWCONFIRM,,}" in
      y|n) break;;
      *) echo "Please enter 'y' or 'n'.";;
    esac
  done
  if [[ "${UFWCONFIRM,,}" == "y" ]]; then
      read -p "Enter the port number you want to allow: " UFWPORT
      read -p "Enter the protocol (tcp/udp): " UFWPROTO
      echo ""
      echo "... Configuring UFW rule for port $UFWPORT/$UFWPROTO"
      sudo ufw allow $UFWPORT/$UFWPROTO > /dev/null
      echo "... UFW rule applied for $UFWPORT/$UFWPROTO"
      echo ""
  fi

  # Get OVPN File
  while true; do
    read -p "Do you want to download a new OVPN file? [y/n]: " GETOVPN
    case "${GETOVPN,,}" in
      y|n) break;;
      *) echo "Please enter 'y' or 'n'.";;
    esac
  done
  if [[ "${GETOVPN,,}" == "y" ]]; then
    rm -f *.ovpn
    read -p "Paste in a URL to download OVPN file: " OVPNURL
    curl -s -O "$OVPNURL"
    # Check if any .ovpn file exists after download
    if ! ls *.ovpn 1> /dev/null 2>&1; then
      echo -e "Error: OVPN download failed or no .ovpn file found. Aborting script.\n\n"
      exit 1
    fi
    for XFILE in *.ovpn; do
      sudo cp "$XFILE" "$XVPNCHOME"
    done
    XCONFIGFILE="$XVPNCHOME$XFILE"
  else
    # If not downloading, check if any .ovpn file exists in current dir
    if ! ls *.ovpn 1> /dev/null 2>&1; then
      echo -e "Error: No .ovpn file found. Aborting script.\n\n"
      exit 1
    fi
    for XFILE in *.ovpn; do
      sudo cp "$XFILE" "$XVPNCHOME"
    done
    XCONFIGFILE="$XVPNCHOME$XFILE"
  fi
while [ $VPNSERVICE != "q" ]
do
  if [ "$VPNSERVICE" == "1" ]; then
    echo ""
    echo "... Getting organized"
    cd $XVPNHOME
    sudo rm -rf $XVPNLOGFILE
    # Echo current external IP after starting VPN
    echo "... Current external IP: $(curl -s https://ipinfo.io/ip)"
    echo "... Starting VPN"
    echo "... CONFIGFILE: " $XCONFIGFILE
    sudo openvpn --config $XCONFIGFILE --log $XVPNLOGFILE --daemon --ping 10 --ping-exit 60 --auth-nocache --mute-replay-warnings --verb 3
    sleep 7
    echo "... Viewing log"
    echo ""
    sudo tail $XVPNLOGFILE
    echo ""
    while true; do
      read -p "Has it started? [Y/N/F - f is failed] " iStart
      case "${iStart,,}" in
        y)
          # Echo current external IP before exiting loop
          echo "... Current external IP: $(curl -s https://ipinfo.io/ip)"
          VPNSERVICE="q"
          break
          ;;
        f)
          echo "VPN startup failed"
          VPNSERVICE="q"
          break
          ;;
        n)
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
          ;;
        *)
          read -p "Type 'q' to quit: " VPNSERVICE
          if [ "$VPNSERVICE" == "q" ]; then
            break
          fi
          ;;
      esac
    done
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
            nohup qbittorrent-nox > "$XHOME/qbit.log" 2>&1 &
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
