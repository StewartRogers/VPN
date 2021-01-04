#!/bin/bash

clear
echo ""
echo "VPN Start Script"
echo ""
sleep 2

xHOME="/home/pi/MyPiFiles/vpn/"
xSTOPFILE=$xHOME"stopvpn.sh"
xTEMPHOME=$xHOME"temp/"
xLOGFILE=$xTEMPHOME"openvpn.log"
xVPNHOME="/etc/openvpn/client/"
xUSERPASS=$xTEMPHOME"openvpncode.txt"
xPyFILE=$xHOME"vpn_active.py"
xSUCCESS="FALSE"

read -p "Which VPN Service (1 = NL, 2 = CA, 3 = DE, q = quit): " VPNSERVICE

while [ $VPNSERVICE != "q" ]
do
  echo ""
  echo "Stopping Deluge and VPN..."
  $xSTOPFILE
  rm -rf $xLOGFILE
  if [ $VPNSERVICE == "1" ];
     then 
          echo ""
          echo "VPN NL Service"
          echo ""
          echo "Downloading OVPN files..."
          rm -rf ${xTEMPHOME}*.zip
          iDATE1=$(date +"%B-%Y")
          iDATE2=$(date '+%B-%Y' --date '1 month ago')
          xURL1="https://freevpnme.b-cdn.net/FreeVPN.me-OpenVPN-Bundle-July-2020.zip"
          xURL2="https://freevpnme.b-cdn.net/FreeVPN.me-OpenVPN-Bundle-July-2020.zip"
          wget -q $xURL1 -P $xTEMPHOME
          RC1="$?"
          if [ "$RC1" -ne 0 ];
            then echo "... WGET failed to download. Trying for old ZIP file..."
                 sleep 2
                 wget -q $xURL2 -P $xTEMPHOME
                 RC2="$?"
                 if [ "$RC2" -ne 1 ]; 
                    then echo "... WGET second attempt succeeded."
                         echo ""
                         xSUCCESS="TRUE"
                         xFILE="FreeVPN.me-OpenVPN-Bundle-July-2020.zip"
                    else echo "... WGET second attempt failed."
                         echo ""
                         xSUCCESS="FALSE"
                 fi
            else 
                 xSUCCESS="TRUE"
                 xFILE="FreeVPN.me-OpenVPN-Bundle-July-2020.zip"
          fi
          if [ $xSUCCESS == "TRUE" ];
             then echo "OVPN files downloaded."
                  sleep 2
                  cd $xTEMPHOME
                  rm -f *.ovpn
                  rm -f *.txt
                  rm -f *.crt
                  rm -f *.key
                  sleep 2
                  unzip -j -q $xFILE
                  echo "Files unzipped."
                  sleep 2
                  xCONFIGFILE="${xHOME}Server1-TCP443.ovpn"
                  [[ -e ${xTEMPHOME}Server1-TCP443.ovpn ]] && sudo cp ${xTEMPHOME}Server1-TCP443.ovpn $xVPNHOME
                  xCONFIGFILE="${xVPNHOME}Server1-TCP443.ovpn"
                  echo "Files copied to openvpn folder."
                  echo ""
                  sleep 2
                  echo "Building OVPN login file"
                  echo ""
                  cd $xTEMPHOME
                  [[ -e $xUSERPASS ]] && mv $xUSERPASS ${xUSERPASS}.bak
                  echo 'freevpn.me' >> $xUSERPASS
                  curl -s https://freevpn.me/accounts/ | html2text > ${xTEMPHOME}tmp_html.txt
                  cat ${xTEMPHOME}tmp_html.txt | grep Password > ${xTEMPHOME}ipass.txt
                  cat ${xTEMPHOME}ipass.txt | head -3 | tail -1 > ${xTEMPHOME}ipass2.txt
                  sed 's/[^,:]*: //g' ${xTEMPHOME}ipass2.txt >> $xUSERPASS 
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
                  cd $xTEMPHOME
                  [[ -e $xUSERPASS ]] && mv $xUSERPASS ${xUSERPASS}.bak
#                  xURL="https://www.vpngate.net/common/openvpn_download.aspx?sid=1603037986009&udp=1&host=public-vpn-234.opengw.net&port=1195&hid=15134981&/vpngate_public-vpn-234.opengw.net_udp_1195.ovpn"
#                  wget -q $xURL -P $xVPNHOME
                  RC2="$?"
                  if [ "$RC2" -ne 0 ];
                     then echo "... WGET failed to download OVPN file."
                          xSUCCESS="TRUE"
                     else
                          echo "... WGET succeeded."
#                          echo 'freevpn4you' >> $xUSERPASS
#                          read -p "Enter pasword from - https://freevpn4you.net/locations/ukraine.php password: " xFREEPASS 
#                          echo $xFREEPASS >> $xUSERPASS
                          xCONFIGFILE="/etc/openvpn/client/vpngate_public-vpn-234.opengw.net_udp_1195.ovpn"
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
                  cd $xTEMPHOME
                  rm -rf *.zip
                  wget -q $xURL -P $xTEMPHOME
                  RC3="$?"
                  if [ "$RC3" -ne 0 ];
                     then echo "... WGET failed to download OVPN file."
                          xSUCCESS="FALSE"
                     else
                          echo "... WGET succeeded."
                          cd  $xTEMPHOME
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
          echo "Taking down IPV6"
          sudo sysctl -w net.ipv6.conf.all.disable_ipv6=1
          sleep 2
          sudo sysctl -w net.ipv6.conf.default.disable_ipv6=1
          sleep 2
          echo ""
          echo "Taking down WIFI..."
          sudo ifconfig wlan0 down
          sleep 2
          echo "Reloading UFW..."
          sudo ufw reload
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
  read -p "Which VPN Service (1 = NL, 2 = CA, 3 = DE, q = quit): " VPNSERVICE
done

if [[ $iStart == "y" && $VPNSERVICE == "q" ]];
  then
     sleep 2
     echo ""
     echo "Testing VPN..."
     active=$(python3 $xPyFILE)
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
