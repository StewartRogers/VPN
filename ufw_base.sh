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

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

# Read PORT from webapp/.env (sudo doesn't inherit the caller's shell env),
# without clobbering an already-exported PORT.
if [ -z "${PORT+x}" ] && [ -f "$SCRIPT_DIR/webapp/.env" ]; then
    while IFS='=' read -r key value; do
        case "$key" in ''|'#'*) continue ;; esac
        if [[ "$value" == \"*\" && "$value" == *\" ]] || [[ "$value" == \'*\' && "$value" == *\' ]]; then
            value="${value:1:-1}"
        fi
        if [ "$key" = "PORT" ]; then
            PORT="$value"
        fi
    done < <(grep -v '^\s*#' "$SCRIPT_DIR/webapp/.env" | grep -v '^\s*$')
fi
PORT="${PORT:-5000}"

# Outgoing policy, applied BEFORE ufw is enabled.
#
# ufw_killswitch.sh sets this to 'deny'. It used to let this script enable ufw
# with allow-outgoing and then flip the policy afterwards, which left a window
# of a second or more where ufw was live and outgoing was unrestricted. Setting
# the policy first means the worst case is a brief total outage (fail-closed)
# instead of a brief unprotected one (fail-open).
UFW_OUT_POLICY="${UFW_OUT_POLICY:-allow}"
case "$UFW_OUT_POLICY" in
    allow|deny) ;;
    *) echo "ERROR: UFW_OUT_POLICY must be 'allow' or 'deny' (got '$UFW_OUT_POLICY')"; exit 1 ;;
esac

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Applying UFW base state (outgoing: $UFW_OUT_POLICY)..."

ufw --force reset                   > /dev/null 2>&1

ufw default deny  incoming              > /dev/null 2>&1
ufw default "$UFW_OUT_POLICY" outgoing  > /dev/null 2>&1

ufw allow 22/tcp    comment 'SSH'              > /dev/null 2>&1
ufw allow 443/tcp   comment 'HTTPS'            > /dev/null 2>&1
ufw allow 32400/tcp comment 'Plex'             > /dev/null 2>&1
ufw allow 8080/tcp  comment 'Web UI'           > /dev/null 2>&1
ufw allow "$PORT/tcp" comment 'VPN Web UI'     > /dev/null 2>&1
ufw allow 19806/tcp comment 'qBittorrent peer' > /dev/null 2>&1
ufw allow 19806/udp comment 'qBittorrent peer' > /dev/null 2>&1
ufw allow in on tun0 comment 'VPN interface'   > /dev/null 2>&1

ufw --force enable                  > /dev/null 2>&1

if [ "$UFW_OUT_POLICY" = "deny" ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Base rules applied with outgoing DENIED - caller must add its allow-out rules."
else
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Base state applied - outgoing unrestricted, web UI port $PORT/tcp open."
fi
