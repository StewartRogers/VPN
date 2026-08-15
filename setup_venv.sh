#!/bin/bash
# Copyright (c) 2022-2025 Stewart Rogers
# SPDX-License-Identifier: MIT
#
# setup_venv.sh — create (or refresh) the project virtualenv in ./.venv and
# install webapp/requirements.txt into it.
#
# Safe to re-run: an existing venv is reused and its packages upgraded.
#
# The venv is created WITHOUT --system-site-packages on purpose.
# requirements.txt is the single source of truth for flask/requests rather
# than half-inheriting them from apt's python3-requests.

set -euo pipefail

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
VENV_DIR="${VPN_VENV:-$SCRIPT_DIR/.venv}"
REQS="$SCRIPT_DIR/webapp/requirements.txt"

echo ""
echo "VPN Monitor — Python environment setup"
echo ""

if ! command -v python3 >/dev/null 2>&1; then
    echo "ERROR: python3 not found."
    echo ""
    echo "Install with:"
    echo "  sudo apt install python3 python3-venv"
    echo ""
    exit 1
fi

py_major=$(python3 -c "import sys; print(sys.version_info.major)")
py_minor=$(python3 -c "import sys; print(sys.version_info.minor)")
if [ "$py_major" -lt 3 ] || { [ "$py_major" -eq 3 ] && [ "$py_minor" -lt 9 ]; }; then
    echo "ERROR: Python 3.9 or higher is required (found: $py_major.$py_minor)"
    exit 1
fi
echo "  Python: $py_major.$py_minor"

if [ ! -f "$REQS" ]; then
    echo "ERROR: $REQS not found."
    exit 1
fi

if [ -x "$VENV_DIR/bin/python" ]; then
    echo "  Venv:   $VENV_DIR (exists, reusing)"
else
    echo "  Venv:   $VENV_DIR (creating)"
    if ! python3 -m venv "$VENV_DIR"; then
        echo ""
        echo "ERROR: could not create the venv."
        echo ""
        echo "On Debian/Raspberry Pi OS the venv module is a separate package:"
        echo "  sudo apt install python3-venv"
        echo ""
        exit 1
    fi
fi

echo ""
echo "Installing dependencies from webapp/requirements.txt..."
echo ""
"$VENV_DIR/bin/python" -m pip install --upgrade pip
"$VENV_DIR/bin/python" -m pip install -r "$REQS"

echo ""
echo "Done. ./startvpn.sh and ./start_web.sh will now use $VENV_DIR."
echo ""
echo "To run tests or a script by hand:"
echo "  $VENV_DIR/bin/python -m pytest -q"
echo "  $VENV_DIR/bin/python qbt_config.py"
echo ""
