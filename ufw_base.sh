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

ufw --force reset

ufw default deny  incoming
ufw default allow outgoing

ufw allow 22/tcp    comment 'SSH'
ufw allow 443/tcp   comment 'HTTPS'
ufw allow 32400/tcp comment 'Plex'
ufw allow 8080/tcp  comment 'Web UI'
ufw allow 19806/tcp comment 'qBittorrent peer'
ufw allow 19806/udp comment 'qBittorrent peer'
ufw allow in on tun0 comment 'VPN interface'

ufw --force enable

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Base state applied - outgoing unrestricted."
