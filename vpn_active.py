#!/usr/bin/env python3
# Copyright (c) 2022-2025 Stewart Rogers
# SPDX-License-Identifier: MIT

import ipaddress
import socket
import sys
import threading
import requests
import subprocess

RESOLV_CONF = "/etc/resolv.conf"

# Anycast resolvers that answer from any source address. An ISP's resolver
# usually will not: once redirect-gateway moves the default route onto tun0,
# queries reach it from the VPN's exit IP and are refused or dropped. That is
# a silent failure - the query leaves, nothing comes back, and getaddrinfo
# blocks for its full retry budget rather than returning an error.
KNOWN_PUBLIC_RESOLVERS = {
    "8.8.8.8", "8.8.4.4",              # Google
    "1.1.1.1", "1.0.0.1",              # Cloudflare
    "9.9.9.9", "149.112.112.112",      # Quad9
    "208.67.222.222", "208.67.220.220" # OpenDNS
}


def resolver_addresses(path=RESOLV_CONF):
    servers = []
    try:
        with open(path) as fh:
            for line in fh:
                parts = line.split()
                if len(parts) >= 2 and parts[0] == "nameserver":
                    servers.append(parts[1])
    except OSError:
        pass
    return servers


def check_resolvers(lan_cidrs):
    """
    Nameservers that will be routed through the tunnel and may stop answering
    there. Advisory only — a public resolver like 8.8.8.8 is off-LAN and
    perfectly fine, so this can never be a gate, only a warning.

    A nameserver inside LAN_CIDRS is safe by construction: ufw_killswitch.sh
    allows those out on the physical interface, so they never enter the tunnel
    and behave identically with the VPN up or down.
    """
    nets = []
    for cidr in lan_cidrs:
        try:
            nets.append(ipaddress.ip_network(cidr, strict=False))
        except ValueError:
            continue

    suspect = []
    for ns in resolver_addresses():
        try:
            addr = ipaddress.ip_address(ns)
        except ValueError:
            continue
        if any(addr in net for net in nets):
            continue
        if ns in KNOWN_PUBLIC_RESOLVERS:
            continue
        suspect.append(ns)
    return suspect


def _resolves(host, timeout):
    # getaddrinfo ignores socket timeouts and blocks on its own retry schedule
    # (~20s against a resolver that never answers), so bound it out of band.
    # The thread is a daemon and this process is short-lived, so a hung lookup
    # does not keep anything alive.
    done = []

    def _lookup():
        try:
            socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)
            done.append(True)
        except Exception:
            done.append(False)

    worker = threading.Thread(target=_lookup, daemon=True)
    worker.start()
    worker.join(timeout)
    return bool(done) and done[0]


def _reachable(addr, port, timeout):
    try:
        with socket.create_connection((addr, port), timeout=timeout):
            return True
    except Exception:
        return False


def diagnose_ip_failure():
    """
    Say *which layer* failed after get_external_ip() returned None.

    "could not reach IP services" covers two very different faults, and on
    2026-08-14 the ambiguity cost an hour: DNS was pointed at an ISP resolver
    that stops answering once queries arrive from the VPN's exit IP, while the
    tunnel itself was carrying traffic perfectly. Resolve a name, then connect
    to a literal address, and report the one that actually broke.
    """
    resolved = _resolves("api.ipify.org", 5.0)
    reachable = _reachable("1.1.1.1", 443, 5.0)

    if not resolved and reachable:
        servers = ", ".join(resolver_addresses()) or "none configured"
        return ("DNS resolution failed but the tunnel carries traffic - "
                "resolvers (%s) are not answering through the VPN" % servers)
    if not reachable and resolved:
        return "no connectivity to IP services (DNS resolved normally)"
    if not reachable:
        return "no DNS and no connectivity - nothing is leaving the tunnel"
    return "IP services did not answer (DNS and connectivity both OK)"

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
    # api64.ipify.org is dual-stack and answers with IPv6 when it can. An IPv6
    # address can never equal the (IPv4) home IP, so before the IPv4Address
    # check below it would always read as "secure". The validation rejects such
    # a reply and falls through to the next service.
    services = [
        ("https://api.ipify.org?format=json", "ip"),
        ("https://api64.ipify.org?format=json", "ip"),
        ("https://httpbin.org/ip", "origin"),
    ]
    for url, key in services:
        try:
            response = requests.get(url, timeout=3)
            response.raise_for_status()
            ip = response.json().get(key, "").split(",")[0].strip()
            if not ip:
                continue
            ipaddress.IPv4Address(ip)
            return ip
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
    if len(sys.argv) >= 2 and sys.argv[1] == "--diagnose":
        print(diagnose_ip_failure())
        sys.exit(0)

    if len(sys.argv) >= 2 and sys.argv[1] == "--check-resolvers":
        for ns in check_resolvers(sys.argv[2:]):
            print(ns)
        sys.exit(0)

    if len(sys.argv) != 2:
        print("Usage: vpn_active.py <home_ip>", file=sys.stderr)
        print("       vpn_active.py --diagnose", file=sys.stderr)
        print("       vpn_active.py --check-resolvers [cidr...]", file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
