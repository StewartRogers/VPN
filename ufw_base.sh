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

# Outgoing policy. Defaults to "allow" (base state). ufw_killswitch.sh calls
# this with OUTGOING_POLICY=deny so the firewall is *enabled already denying*
# outgoing — otherwise there is a window between 'ufw enable' and the kill
# switch setting its policy in which egress is unrestricted.
OUTGOING_POLICY="${OUTGOING_POLICY:-allow}"

# Every ufw call used to be fire-and-forget, so this script always exited 0 and
# callers' return-code checks were decorative. Track failures instead: a failed
# 'ufw allow 22/tcp' after a successful reset means no SSH rule and a remote
# lockout, which must not be reported as success.
FAILED=0
run_ufw() {
    if ! ufw "$@" > /dev/null 2>&1; then
        echo "  ERROR: 'ufw $*' failed"
        FAILED=1
    fi
}

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Applying UFW base state (outgoing: $OUTGOING_POLICY)..."

run_ufw --force reset

run_ufw default deny  incoming
run_ufw default "$OUTGOING_POLICY" outgoing

run_ufw allow 22/tcp    comment 'SSH'
run_ufw allow 443/tcp   comment 'HTTPS'
run_ufw allow 32400/tcp comment 'Plex'
run_ufw allow 8080/tcp  comment 'Web UI'
run_ufw allow "$PORT/tcp" comment 'VPN Web UI'
run_ufw allow 19806/tcp comment 'qBittorrent peer'
run_ufw allow 19806/udp comment 'qBittorrent peer'
run_ufw allow in on tun0 comment 'VPN interface'

run_ufw --force enable

if [ "$FAILED" -ne 0 ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] FAILED - UFW is not in the expected state. Check 'sudo ufw status verbose'."
    exit 1
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Base state applied - outgoing $OUTGOING_POLICY, web UI port $PORT/tcp open."
