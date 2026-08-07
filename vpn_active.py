#!/usr/bin/python3
# Copyright (c) 2022-2025 Stewart Rogers
# SPDX-License-Identifier: MIT

import ipaddress
import sys
import time
import requests
import subprocess

def diag(message):
    """Write a diagnostic line to stderr.

    stdout carries the machine-readable verdict (secure/leak/error) that
    checkip.sh parses, so anything explanatory must go to stderr. Without this
    every failure -- DNS, TLS, timeout, a service changing its JSON shape --
    collapsed into one indistinguishable "could not reach IP services".
    """
    print(message, file=sys.stderr)

def check_openvpn_running():
    try:
        result = subprocess.run(['pgrep', '-x', 'openvpn'], capture_output=True, timeout=2)
        return result.returncode == 0
    except Exception:
        return False

def check_vpn_interface():
    """True only if tun0 exists AND is UP.

    'ip link show tun0' exits 0 whenever the device exists, including state
    DOWN — a tun0 left behind by --persist-tun or a SIGKILLed openvpn would
    otherwise pass while carrying no traffic.
    """
    try:
        result = subprocess.run(['ip', '-o', 'link', 'show', 'tun0'],
                                capture_output=True, text=True, timeout=2)
        if result.returncode != 0:
            return False
        return 'state UP' in result.stdout or ',UP' in result.stdout
    except Exception:
        return False

def check_routing():
    """True if traffic to the internet actually egresses via tun0.

    Uses 'ip route get' rather than the default route because redirect-gateway
    def1 installs two /1 routes and leaves the default pointing at eth0.
    """
    try:
        result = subprocess.run(['ip', 'route', 'get', '8.8.8.8'],
                                capture_output=True, text=True, timeout=2)
        return 'tun0' in result.stdout
    except Exception:
        return False

def get_external_ip():
    """Return the external IPv4 address as a string, or None.

    IPv4-only endpoints: api64.ipify.org is deliberately excluded because it is
    dual-stack and returns an IPv6 address when one is available, which can
    never equal the (IPv4) home IP — making every leak check read "secure".
    """
    services = [
        ("https://api.ipify.org?format=json", "ip"),
        ("https://httpbin.org/ip", "origin"),
    ]
    for url, key in services:
        started = time.monotonic()
        try:
            response = requests.get(url, timeout=3)
            response.raise_for_status()
            payload = response.json()
            raw = payload.get(key, "")
            ip = str(raw).split(",")[0].strip()
            if not ip:
                diag(f"{url} -> HTTP {response.status_code} but no '{key}' field "
                     f"in {payload!r} ({time.monotonic() - started:.1f}s)")
                continue
            # Reject anything that is not a well-formed IPv4 literal — an
            # error page or an IPv6 answer must not be treated as our IP.
            validated = str(ipaddress.IPv4Address(ip))
            diag(f"{url} -> {validated} ({time.monotonic() - started:.1f}s)")
            return validated
        except Exception as exc:
            # Name the failure: a timeout, a DNS error, a TLS error and a
            # malformed response are all different problems.
            diag(f"{url} -> {type(exc).__name__}: {exc} "
                 f"({time.monotonic() - started:.1f}s)")
            continue
    diag("All IP services failed")
    return None

def main(home_ip):
    """Return 0 = secure, 1 = confirmed leak, 2 = could not determine.

    Note the contract: 0 is "secure", which is also shell success. A caller
    writing `if vpn_active.py "$ip"; then start_torrenting; fi` is correct.
    """
    try:
        home_ip = str(ipaddress.IPv4Address(home_ip.strip()))
    except ValueError:
        print("error")
        print(f"Invalid home IP: {home_ip!r}", file=sys.stderr)
        return 2

    if not check_openvpn_running():
        print("leak")
        return 1

    if not check_vpn_interface():
        print("leak")
        return 1

    if not check_routing():
        print("leak")
        return 1

    current_ip = get_external_ip()
    if current_ip is None:
        print("error")
        return 2

    try:
        current_ip = str(ipaddress.IPv4Address(str(current_ip).strip()))
    except ValueError:
        print("error")
        return 2

    if home_ip == current_ip:
        print("leak")
        return 1

    print("secure")
    return 0

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: vpn_active.py <home_ip>", file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
