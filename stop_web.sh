#!/bin/bash
# Copyright (c) 2022-2025 Stewart Rogers
# SPDX-License-Identifier: MIT
#
# stop_web.sh — Stop the VPN Monitor web app and reset UFW to base state
# (undoes the kill switch so normal internet access is restored).
#
# Usage: bash stop_web.sh
#

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

if pgrep -f "webapp/app.py" > /dev/null; then
    echo "Stopping VPN Monitor web app..."
    pkill -f "webapp/app.py"
else
    echo "VPN Monitor web app is not running."
fi

echo "Resetting UFW to base state..."
if sudo bash "$SCRIPT_DIR/ufw_base.sh"; then
    echo "UFW base state restored - outgoing unrestricted."
else
    echo "WARNING: UFW reset failed - run manually: sudo bash $SCRIPT_DIR/ufw_base.sh"
fi
