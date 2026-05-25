#!/bin/bash
# Copyright (c) 2022-2025 Stewart Rogers
# SPDX-License-Identifier: MIT
#
# Applies UFW base state: no kill switch, outgoing unrestricted.
# Run this to recover from any UFW issue, or when the VPN is not in use.
#
# Usage: sudo bash ufw_base.sh

if [ "$EUID" -ne 0 ]; then
    echo "Must be run as root: sudo bash $0"
    exit 1
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Applying UFW base state..."

ufw --force reset                   > /dev/null 2>&1

ufw default deny  incoming          > /dev/null 2>&1
ufw default allow outgoing          > /dev/null 2>&1

ufw allow 22/tcp    comment 'SSH'              > /dev/null 2>&1
ufw allow 443/tcp   comment 'HTTPS'            > /dev/null 2>&1
ufw allow 32400/tcp comment 'Plex'             > /dev/null 2>&1
ufw allow 8080/tcp  comment 'Web UI'           > /dev/null 2>&1
ufw allow 19806/tcp comment 'qBittorrent peer' > /dev/null 2>&1
ufw allow 19806/udp comment 'qBittorrent peer' > /dev/null 2>&1
ufw allow in on tun0 comment 'VPN interface'   > /dev/null 2>&1

ufw --force enable                  > /dev/null 2>&1

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Base state applied - outgoing unrestricted."
