#!/usr/bin/python3
# Copyright (c) 2022-2025 Stewart Rogers
# SPDX-License-Identifier: MIT

import sys
import requests
import subprocess

def check_openvpn_running():
    try:
        result = subprocess.run(['pgrep', '-x', 'openvpn'], capture_output=True, timeout=2)
        return result.returncode == 0
    except Exception:
        return False

def check_vpn_interface():
    try:
        result = subprocess.run(['ip', 'link', 'show', 'tun0'], capture_output=True, timeout=2)
        return result.returncode == 0
    except Exception:
        return False

def get_external_ip():
    services = [
        ("https://api.ipify.org?format=json", "ip"),
        ("https://api64.ipify.org?format=json", "ip"),
        ("https://httpbin.org/ip", "origin"),
    ]
    for url, key in services:
        try:
            response = requests.get(url, timeout=5)
            response.raise_for_status()
            ip = response.json().get(key, "")
            # httpbin returns comma-separated IPs when behind a proxy
            return ip.split(",")[0].strip()
        except Exception:
            continue
    return None

def main(home_ip):
    if not check_openvpn_running():
        print("leak")
        return 1

    if not check_vpn_interface():
        print("leak")
        return 1

    current_ip = get_external_ip()
    if current_ip is None:
        print("error")
        return 2

    if home_ip.strip() == current_ip.strip():
        print("leak")
        return 1

    print("secure")
    return 0

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: vpn_active.py <home_ip>", file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
