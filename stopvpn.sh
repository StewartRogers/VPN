#!/bin/bash
# Copyright (c) 2022-2025 Stewart Rogers
# SPDX-License-Identifier: MIT
#
# Original Author: Stewart Rogers
# This licensed under the MIT License
# A short and simple permissive license with conditions only requiring
# preservation of copyright and license notices. Licensed works, modifications,
# and larger works may be distributed under different terms and without source code.
#

#
# Load configuration file if exists
#
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

# Resolve the Python interpreter (./.venv if present, else system python3)
source "$SCRIPT_DIR/py_env.sh"

if [ -f "$HOME/.vpn_config.conf" ]; then
    source "$HOME/.vpn_config.conf"
elif [ -f "$SCRIPT_DIR/vpn_config.conf" ]; then
    source "$SCRIPT_DIR/vpn_config.conf"
fi

# Set default values if not in config
BACKUP_DIR="${BACKUP_DIR:-/tmp/vpn_backups}"
PID_DIR="${PID_DIR:-/tmp/vpn_pids}"
LOG_DIR="${LOG_DIR:-$SCRIPT_DIR/vpn_logs}"

# Ensure required directories exist
mkdir -p "$BACKUP_DIR" "$PID_DIR" "$LOG_DIR"

#
# Logging function
#
log_message() {
    local level=$1
    local message=$2
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [$level] $message" >> "$LOG_DIR/vpn.log" 2>/dev/null
}

divider() {
    echo "------------------------------------------------------------"
}

rotate_logs() {
    if [ -f "$LOG_DIR/vpn.log" ]; then
        local size=$(stat -c%s "$LOG_DIR/vpn.log" 2>/dev/null || echo 0)
        if [ $size -gt 10485760 ]; then  # 10MB
            mv "$LOG_DIR/vpn.log" "$LOG_DIR/vpn.log.1"
            [ -f "$LOG_DIR/vpn.log.1.gz" ] && rm "$LOG_DIR/vpn.log.1.gz"
            gzip "$LOG_DIR/vpn.log.1" 2>/dev/null || true
            log_message "INFO" "Log rotated (previous file > 10MB)"
        fi
    fi
}

#
# VARIABLES
#
TEMP_DEST=""  # Will be set to SOURCE_DIR during file processing

reset_ufw() {
    echo "  Resetting UFW to base state..."
    log_message "INFO" "Resetting UFW to base state..."
    if sudo bash "$SCRIPT_DIR/ufw_base.sh" >> "$LOG_DIR/vpn.log" 2>&1; then
        echo "  UFW base state restored - outgoing unrestricted"
        log_message "INFO" "UFW base state restored - outgoing unrestricted"
    else
        echo "  WARNING: UFW reset failed - run manually: sudo bash $SCRIPT_DIR/ufw_base.sh"
        log_message "WARN" "UFW reset failed - run manually: sudo bash $SCRIPT_DIR/ufw_base.sh"
    fi
}

#
# PID-based process stopping
#
stop_service_by_pid() {
    local service=$1
    local pid_file="$PID_DIR/${service}.pid"

    if [ -f "$pid_file" ]; then
        local pid=$(cat "$pid_file")
        if kill -0 $pid 2>/dev/null; then
            echo "  Stopping $service (PID: $pid)"
            log_message "INFO" "Stopping $service (PID: $pid)"
            kill $pid 2>/dev/null
            sleep 1
            if kill -0 $pid 2>/dev/null; then
                kill -9 $pid 2>/dev/null
            fi
            rm "$pid_file"
        else
            echo "  $service not running (stale PID file removed)"
            log_message "INFO" "$service not running (stale PID file removed)"
            rm "$pid_file"
        fi
    else
        if pgrep -f "$service" >/dev/null; then
            echo "  Stopping $service (pkill fallback)"
            log_message "WARN" "Stopping $service using pkill (no PID file found)"
            sudo pkill -f "$service"
        else
            echo "  $service is not running"
            log_message "INFO" "$service is not running"
        fi
    fi
}

#
# Confirm qBittorrent is actually gone before anything relaxes the firewall.
#
# Ordering invariant 1: the kill switch comes down only once the client is
# *confirmed* stopped. Issuing a kill is not the same as it having worked.
# stop_service_by_pid() removes the PID file unconditionally, and its plain
# kill/kill -9 are not run under sudo (only the pkill fallback is), so a client
# owned by another user or ignoring the signal survives silently into
# 2>/dev/null. This was the only teardown path that then reset ufw regardless.
#
# Mirrors monitor.py's stop_qbittorrent()/_await_qbittorrent_exit().
QBT_STOP_GRACE="${QBT_STOP_GRACE:-30}"

confirm_qbittorrent_stopped() {
    local waited=0
    while [ "$waited" -lt "$QBT_STOP_GRACE" ]; do
        pgrep -f qbittorrent-nox >/dev/null 2>&1 || return 0
        sleep 1
        waited=$((waited + 1))
        if [ $((waited % 5)) -eq 0 ]; then
            echo "  Still waiting for qBittorrent to shut down (${waited}s/${QBT_STOP_GRACE}s)"
        fi
    done
    echo "  qBittorrent has not exited after ${QBT_STOP_GRACE}s - sending SIGKILL"
    log_message "WARN" "qBittorrent still running after ${QBT_STOP_GRACE}s - sending SIGKILL"
    sudo pkill -9 -f qbittorrent-nox 2>/dev/null
    for _ in 1 2 3 4 5; do
        sleep 1
        pgrep -f qbittorrent-nox >/dev/null 2>&1 || return 0
    done
    return 1
}

# Function to stop services

shutdown_services() {
    rotate_logs
    log_message "INFO" "Starting service shutdown"
    divider
    echo "  Shutting down services..."
    echo ""

    echo "  [ qBittorrent ]"
    stop_service_by_pid "qbittorrent"
    if ! confirm_qbittorrent_stopped; then
        echo ""
        echo "  CRITICAL: qBittorrent is STILL RUNNING after SIGKILL."
        echo "  Teardown HALTED - the kill switch is being left ACTIVE, because"
        echo "  opening the firewall now would hand the client your ISP link."
        echo "  Kill it by hand, then run ./remove_killswitch.sh."
        echo ""
        log_message "CRITICAL" "Teardown HALTED - qBittorrent survived SIGKILL; kill switch left active"
        divider
        echo ""
        return 1
    fi

    echo ""
    echo "  [ Monitoring ]"
    stop_service_by_pid "checkip"
    sleep 1
    screen -S "checkip" -p 0 -X quit > /dev/null 2>&1
    log_message "INFO" "Stopping checkip script"

    echo ""
    echo "  [ OpenVPN ]"
    if pgrep -x "openvpn" >/dev/null; then
        echo "  Stopping OpenVPN"
        log_message "INFO" "Stopping OpenVPN"
        sudo pkill -x "openvpn"
        sleep 2
    else
        echo "  OpenVPN is not running"
        log_message "INFO" "OpenVPN is not running"
    fi

    echo ""
    echo "  [ Firewall ]"
    reset_ufw

    divider
    echo ""
}

# Main script logic
if [[ "$1" == "--shutdown-only" ]]; then
    log_message "INFO" "Shutdown requested (--shutdown-only)"
    if ! shutdown_services; then
        exit 1
    fi
    echo "Done."
    exit 0
fi

# Prompt user to shutdown services
read -rp "Shutdown services? [y/N]: " do_shutdown
do_shutdown=$(echo "$do_shutdown" | tr '[:upper:]' '[:lower:]' | tr -d '\r')
if [[ "$do_shutdown" == "y" ]]; then
    if ! shutdown_services; then
        # The firewall is still up and a client is still live; running the
        # organizer next would bury that message under a scan.
        exit 1
    fi
else
    echo ""
    echo "  Skipped service shutdown."
    echo ""
fi

# Prompt user to run file organizer
read -rp "Run file organizer? [y/N]: " do_organize
do_organize=$(echo "$do_organize" | tr '[:upper:]' '[:lower:]' | tr -d '\r')
if [[ "$do_organize" == "y" ]]; then
    log_message "INFO" "Starting file organizer"
    "$VPN_PYTHON" "$SCRIPT_DIR/organize.py"
    log_message "INFO" "File organizer completed"
else
    echo ""
    echo "  Skipped file organizer."
    echo ""
fi

#
# END
#