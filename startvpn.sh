#!/bin/bash

clear
echo ""
echo "VPN Start Script"
echo ""
sleep 2

xHOME="/home/pi/MyPiFiles/vpn/"
xTEMPHOME=$xHOME"temp/"
xLOGFILE=$xTEMPHOME"openvpn.log"
xVPNHOME="/etc/openvpn/client/"
xUSERPASS=$xTEMPHOME"openvpncode.txt"
xSUCCESS="FALSE"

read -p "Which VPN Service (1 = NL, 2 = CA, 3 = DE, q = quit): " VPNSERVICE

while [ $VPNSERVICE != "q" ]
do 
  echo ""
  echo "Stopping Deluge and VPN..."
  {$XHOME}stopvpn.sh
  rm -rf $xLOGFILE
  if [ $VPNSERVICE == "1" ];
     then 
          echo ""
          echo "VPN NL Service"
          echo ""
          echo "Downloading OVPN files..."
          rm -rf ${xHOME}*.zip
          iDATE1=$(date +"%B-%Y")
          iDATE2=$(date '+%B-%Y' --date '1 month ago')
          xURL1="https://freevpnme.b-cdn.net/FreeVPN.me-OpenVPN-Bundle-$iDATE1.zip"
          xURL2="https://freevpnme.b-cdn.net/FreeVPN.me-OpenVPN-Bundle-$iDATE2.zip"
          wget -q $xURL1 -P $xHOME
          RC1="$?"
          if [ "$RC1" -ne 0 ];
            then echo "... WGET failed to download. Trying for old ZIP file..."
                 sleep 2
                 wget -q $xURL2 -P $xHOME
                 RC2="$?"
                 if [ "$RC2" -ne 1 ]; 
                    then echo "... WGET second attempt succeeded."
                         echo ""
                         xSUCCESS="TRUE"
                         xFILE="FreeVPN.me-OpenVPN-Bundle-$iDATE2.zip" 
                    else echo "... WGET second attempt failed."
                         echo ""
                         xSUCCESS="FALSE"
                 fi
            else 
                 xSUCCESS="TRUE"
                 xFILE="FreeVPN.me-OpenVPN-Bundle-$iDATE1.zip" 
          fi
          if [ $xSUCCESS == "TRUE" ];
             then echo "OVPN files downloaded."
                  sleep 2
                  cd $xHOME
                  rm -f *.ovpn
                  rm -f *.txt
                  sleep 2
                  unzip -j -q $xFILE
                  echo "Files unzipped."
                  sleep 2
                  xCONFIGFILE="${xHOME}Server1-UDP53.ovpn"
                  [[ -e ${xHOME}Server1-UDP53.ovpn ]] && cp ${xHOME}Server1-UDP53.ovpn $xVPNHOME
                  xCONFIGFILE="${xVPNHOME}Server1-UDP53.ovpn"
                  echo "Files copied to openvpn folder."
                  echo ""
                  sleep 2
                  echo "Building OVPN login file"
                  echo ""
                  cd $xHOME
                  [[ -e $xUSERPASS ]] && mv $xUSERPASS ${xUSERPASS}.bak
                  echo 'freevpn.me' >> $xUSERPASS
                  curl -s https://freevpn.me/accounts/ | html2text > ${xHOME}tmp_html.txt
                  cat ${xHOME}tmp_html.txt | grep Password > ${xHOME}ipass.txt
                  cat ${xHOME}ipass.txt | head -3 | tail -1 > ${xHOME}ipass2.txt
                  grep -o '[^ ]\+$' ${xHOME}ipass2.txt >> $xUSERPASS 
                  chown root:root $xUSERPASS
                  chmod 600 $xUSERPASS
             else echo "... OVPN failed to download."
          fi
     else 
          if [ $VPNSERVICE == "2" ];
             then 
                  echo ""
                  echo "VPN CA Service"
                  echo ""
                  echo "Downloading OVPN files..."
                  cd $xHOME
                  [[ -e $xUSERPASS ]] && mv $xUSERPASS ${xUSERPASS}.bak
                  xURL="https://www.freevpn4you.net/files/Canada-udp.ovpn"
                  wget -q $xURL -P $xVPNHOME
                  RC2="$?"
                  if [ "$RC2" -ne 0 ];
                     then echo "... WGET failed to download OVPN file."
                          xSUCCESS="FALSE"
                     else
                          echo "... WGET succeeded."
                          echo 'freevpn4you' >> $xUSERPASS
                          read -p "Enter pasword from - https://freevpn4you.net/locations/canada.php password: " xFREEPASS 
                          echo $xFREEPASS >> $xUSERPASS
                          xCONFIGFILE="${xVPNHOME}Canada-udp.ovpn"
                          xSUCCESS="TRUE"
                  fi
             else 
                  echo ""
                  echo "VPN DE Service"
                  echo ""
                  echo "Downloading OVPN files..."
                  xURL="https://www.vpnbook.com/free-openvpn-account/VPNBook.com-OpenVPN-DE4.zip"
                  xFILE="VPNBook.com-OpenVPN-DE4.zip"
                  xVPNFILE="vpnbook-de4-udp53.ovpn"
                  cd $xHOME
                  rm -rf *.zip
                  wget -q $xURL -P $xHOME
                  RC3="$?"
                  if [ "$RC3" -ne 0 ];
                     then echo "... WGET failed to download OVPN file."
                          xSUCCESS="FALSE"
                     else
                          echo "... WGET succeeded."
                          cd  $xHOME
                          rm -f *.ovpn
                          rm -f *.txt
                          sleep 2
                          unzip -j -q $xFILE
                          echo "Files unzipped."
                          sleep 2
                          xCONFIGFILE="${xHOME}${xVPNFILE}"
                          [[ -e $xCONFIGFILE ]] && mv $xCONFIGFILE $xVPNHOME
                          xCONFIGFILE="${xVPNHOME}${xVPNFILE}"
                          echo "Files copied to openvpn folder."
                          [[ -e $xUSERPASS ]] && mv $xUSERPASS ${xUSERPASS}.bak
                          echo 'vpnbook' >> $xUSERPASS
                          echo ""
                          read -p "Enter pasword from - https://www.vpnbook.com/freevpn  password: " xFREEPASS 
                          echo $xFREEPASS >> $xUSERPASS
                          xSUCCESS="TRUE"
                  fi
          fi
  fi
  if [[ $xSUCCESS == "TRUE" ]];
     then
          echo "Taking down WIFI..."
          ifconfig wlan0 down
          sleep 2
          echo "Reloading UFW..."
          ufw reload
          sleep 2
          echo "Flush IP Route Cache"
          sudo ip route flush cache
          sleep 2
          echo "Changing directory..."
          cd /etc/openvpn
          sleep 2
          echo ""
          echo "Start VPN"
          echo ""
          sleep 1
          sudo openvpn --config $xCONFIGFILE --auth-user-pass $xUSERPASS --log $xLOGFILE --daemon
          echo ""
          echo "Starting VPN..."
          sleep 7
          echo ""
          echo "View log"
          echo ""
          tail $xLOGFILE
          echo ""
          read -p "Has it started? [ y = yes, n = no, f = failed ] " iStart
          while [ $iStart == "n" ]
          do
            echo ""
#            echo "Wait 10 seconds, check again"
            for load in $(seq 10 -1 0); do
               echo -ne "Check again in $load seconds...\r"
               sleep 1
            done
            echo ""
            echo "Showing tail of log..."
            echo ""
            tail $xLOGFILE
            echo ""
            read -p "Has it started? [ y = yes, n = no, f = failed ] " iStart
          done
  fi
  read -p "Which VPN Service (1 = NL, 2 = CA, 3 = DE, q = quit): " VPNSERVICE
done

if [[ $iStart == "y" && $VPNSERVICE == "q" ]];
  then
     sleep 2
     echo ""
     echo "Testing VPN..."
     active=$(python3 /home/pi/MyPiFiles/vpn_active.py)
     echo "VPN test complete. Result: " $active
     if [ "$active" == "secure" ];
       then echo ""
            echo "Starting Deluge Server"
            deluged
            sleep 2
            echo ""
            echo "Starting Deluge Web Server"
            echo ""
            deluge-web &
            sleep 2
       else echo ""
            echo "Deluge not started."
            echo "" 
     fi
fi
echo ""
echo "FINISHED"
echo ""
