#!/bin/bash
# Copyright (c) 2022-2025 Stewart Rogers
# SPDX-License-Identifier: MIT
#
# py_env.sh — resolve which Python interpreter this checkout should use.
#
# Sourced (never executed) by startvpn.sh, checkip.sh, stopvpn.sh and
# start_web.sh. Sets:
#
#   VPN_PYTHON       absolute path to the interpreter to invoke
#   VPN_VENV         where the venv is (or would be)
#   VPN_PYTHON_KIND  "venv" or "system" — for log/status lines only
#
# The venv is preferred when present and the system python3 is the fallback,
# so a checkout without a venv keeps working exactly as it did before.
#
# Nothing in this project runs Python under sudo — every privileged call is a
# bash-level sudo of ufw/openvpn/pkill/sysctl. That is deliberate and worth
# keeping: sudo resets PATH and would drop the venv, which is how venv'd
# projects end up having to hardcode interpreter paths into sudoers.

_py_env_dir="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
VPN_VENV="${VPN_VENV:-$_py_env_dir/.venv}"
unset _py_env_dir

if [ -x "$VPN_VENV/bin/python" ]; then
    VPN_PYTHON="$VPN_VENV/bin/python"
    VPN_PYTHON_KIND="venv"
else
    VPN_PYTHON="python3"
    VPN_PYTHON_KIND="system"
fi

export VPN_PYTHON VPN_VENV VPN_PYTHON_KIND
