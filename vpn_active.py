#! /usr/bin/python3

import sys
import requests
import subprocess
import time

def check_openvpn_running():
    """Check if OpenVPN process is still running"""
    try:
        result = subprocess.run(['pgrep', '-f', 'openvpn'], capture_output=True, timeout=2)
        return result.returncode == 0
    except:
        return False

def check_vpn_interface():
    """Check if tun0 interface exists and is up"""
    try:
        result = subprocess.run(['ip', 'link', 'show', 'tun0'], capture_output=True, timeout=2)
        return result.returncode == 0
    except:
        return False

def get_external_ip():
    """Try multiple IP services for reliability"""
    services = [
        "https://api.ipify.org?format=json",
        "https://httpbin.org/ip", 
        "https://api64.ipify.org?format=json"
    ]
    
    for service in services:
        try:
            response = requests.get(service, timeout=3)
            if service == "https://httpbin.org/ip":
                return response.json().get("origin").split(',')[0].strip()
            else:
                return response.json().get("ip")
        except:
            continue
    return None

def main_ipcheck(home_ip):
    # First check if OpenVPN is even running
    if not check_openvpn_running():
        print("OpenVPN process not running!")
        return 0
    
    # Check if VPN interface exists
    if not check_vpn_interface():
        print("VPN interface down!")
        return 0
    
    # Check external IP
    current_ip = get_external_ip()
    if current_ip is None:
        print("Cannot determine external IP - network issue")
        return 0
    
    if home_ip.strip() == current_ip.strip():
        print(f"IP LEAK DETECTED: {current_ip}")
        return 0
    else:
        return 1

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: vpn_active.py <home_ip>")
        sys.exit(1)
        
    result = main_ipcheck(sys.argv[1])
    if result == 1:
        print("secure")
    else:
        print("notsecure")
    sys.exit(result)