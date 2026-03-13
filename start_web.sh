#!/bin/bash
#
# start_web.sh — Start the VPN Monitor web app
#
# Environment variables (optional):
#   VPN_API_TOKEN   Bearer token to protect the API. If unset, no auth is required.
#   BIND_HOST       Network interface to bind to (default: 0.0.0.0 = all interfaces).
#                   Set to your LAN IP (e.g. 192.168.1.100) to restrict access.
#   HOME_IP         Pre-VPN ISP IP. If set, the monitor is pre-configured on startup.
#
# Examples:
#   ./start_web.sh
#   VPN_API_TOKEN=mysecrettoken ./start_web.sh
#   BIND_HOST=192.168.1.100 VPN_API_TOKEN=mysecrettoken ./start_web.sh
#
# To generate a strong random token:
#   python3 -c "import secrets; print(secrets.token_hex(32))"
#

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

# Load optional config file
if [ -f "$HOME/.vpn_config.conf" ]; then
    source "$HOME/.vpn_config.conf"
elif [ -f "$SCRIPT_DIR/vpn_config.conf" ]; then
    source "$SCRIPT_DIR/vpn_config.conf"
fi

# Apply defaults
BIND_HOST="${BIND_HOST:-0.0.0.0}"
PORT="${PORT:-5000}"

echo ""
echo "VPN Monitor Web App"
echo ""
echo "  URL:   http://$(hostname -I | awk '{print $1}'):$PORT"
echo "  Bind:  $BIND_HOST:$PORT"
if [ -n "$VPN_API_TOKEN" ]; then
    echo "  Auth:  token set (${#VPN_API_TOKEN} chars)"
else
    echo "  Auth:  none (set VPN_API_TOKEN to enable)"
fi
if [ -n "$HOME_IP" ]; then
    echo "  Home IP: $HOME_IP"
fi
echo ""

exec env \
    BIND_HOST="$BIND_HOST" \
    VPN_API_TOKEN="${VPN_API_TOKEN:-}" \
    HOME_IP="${HOME_IP:-}" \
    python3 "$SCRIPT_DIR/webapp/app.py"
