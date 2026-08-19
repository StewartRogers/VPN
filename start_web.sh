#!/bin/bash
#
# start_web.sh — Start the VPN Monitor web app
#
# Usage: ./start_web.sh [--new-token]
#
#   --new-token   Rotate the API token before starting. Every browser holding
#                 the old token is logged out and must be given the new one.
#
# Environment variables (optional):
#   VPN_API_TOKEN   Bearer token protecting the API. If unset here and in
#                   webapp/.env, one is generated and saved to webapp/.env.
#   BIND_HOST       Network interface to bind to (default: 0.0.0.0 = all interfaces).
#                   Set to your LAN IP (e.g. 192.168.1.100) to restrict access.
#   HOME_IP         Pre-VPN ISP IP. If set, the monitor is pre-configured on startup.
#   ACCESS_LOG      Set to 1/true to log every HTTP request (default: off — only
#                    warnings/errors and the app's own MONITOR/OPENVPN log lines show).
#
# Examples:
#   ./start_web.sh                                  # generates a token on first run
#   ./start_web.sh --new-token                      # rotate it
#   VPN_API_TOKEN=mysecrettoken ./start_web.sh      # override for this run only
#   BIND_HOST=192.168.1.100 ./start_web.sh
#

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

NEW_TOKEN=0
while [ $# -gt 0 ]; do
    case "$1" in
        --new-token) NEW_TOKEN=1 ;;
        -h|--help)
            sed -n '2,25p' "${BASH_SOURCE[0]}" | sed 's/^#\{1,2\} \{0,1\}//'
            exit 0 ;;
        *)
            echo "Unknown option: $1" >&2
            echo "Usage: $0 [--new-token]" >&2
            exit 1 ;;
    esac
    shift
done

# Resolve the Python interpreter (./.venv if present, else system python3)
source "$SCRIPT_DIR/py_env.sh"

# Load webapp/.env, without clobbering vars already set in the environment
if [ -f "$SCRIPT_DIR/webapp/.env" ]; then
    while IFS='=' read -r key value; do
        case "$key" in ''|'#'*) continue ;; esac
        # Strip matching surrounding quotes, same as python-dotenv
        if [[ "$value" == \"*\" && "$value" == *\" ]] || [[ "$value" == \'*\' && "$value" == *\' ]]; then
            value="${value:1:-1}"
        fi
        if [ -z "${!key+x}" ]; then
            export "$key=$value"
        fi
    done < <(grep -v '^\s*#' "$SCRIPT_DIR/webapp/.env" | grep -v '^\s*$')
fi

# Load optional config file
if [ -f "$HOME/.vpn_config.conf" ]; then
    source "$HOME/.vpn_config.conf"
elif [ -f "$SCRIPT_DIR/vpn_config.conf" ]; then
    source "$SCRIPT_DIR/vpn_config.conf"
fi

# --- API token ------------------------------------------------------------
#
# The dashboard keeps the token in the browser's localStorage, so a token that
# changed on every launch would lock the browser out on every restart. Generate
# once, persist to webapp/.env (gitignored, mode 600), and reuse it from there
# on later starts. --new-token rotates it deliberately.
#
# An explicit VPN_API_TOKEN in the environment still wins and is never written
# to disk — that stays a this-run-only override.

ENV_FILE="$SCRIPT_DIR/webapp/.env"

generate_token() {
    # 32 bytes of hex. openssl and /dev/urandom are both CSPRNG-backed; the
    # Python fallback only matters on a box missing both.
    if command -v openssl > /dev/null 2>&1; then
        openssl rand -hex 32
    elif [ -r /dev/urandom ]; then
        head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'
    else
        "$VPN_PYTHON" -c "import secrets; print(secrets.token_hex(32))" 2>/dev/null
    fi
}

persist_token() {
    # Replace any VPN_API_TOKEN line in webapp/.env, leaving the rest intact.
    # Written to a temp file and moved into place so a reader never sees a
    # half-written .env, and created 600 before it ever holds the token.
    local token="$1" tmp
    mkdir -p "$(dirname "$ENV_FILE")" || return 1
    tmp="$(mktemp "${ENV_FILE}.tmp.XXXXXX")" || return 1
    chmod 600 "$tmp" || { rm -f "$tmp"; return 1; }
    if [ -f "$ENV_FILE" ]; then
        grep -v '^[[:space:]]*VPN_API_TOKEN=' "$ENV_FILE" >> "$tmp"
    else
        printf '# Created by start_web.sh — gitignored. Contains the API token.\n' >> "$tmp"
    fi
    printf 'VPN_API_TOKEN=%s\n' "$token" >> "$tmp"
    mv "$tmp" "$ENV_FILE" || { rm -f "$tmp"; return 1; }
}

TOKEN_ORIGIN="stored"
if [ "$NEW_TOKEN" -eq 1 ]; then
    VPN_API_TOKEN="$(generate_token)"
    TOKEN_ORIGIN="rotated"
elif [ -z "${VPN_API_TOKEN:-}" ]; then
    VPN_API_TOKEN="$(generate_token)"
    TOKEN_ORIGIN="generated"
fi

TOKEN_SAVED=1
if [ "$TOKEN_ORIGIN" != "stored" ]; then
    if [ -z "$VPN_API_TOKEN" ]; then
        echo "ERROR: could not generate an API token (no openssl, /dev/urandom or python3)."
        echo "       Set one by hand:  VPN_API_TOKEN=<secret> $0"
        exit 1
    fi
    persist_token "$VPN_API_TOKEN" || TOKEN_SAVED=0
fi

# Apply defaults
BIND_HOST="${BIND_HOST:-0.0.0.0}"
PORT="${PORT:-5000}"

echo ""
echo "VPN Monitor Web App"
echo ""
echo "  URL:   http://$(hostname -I | awk '{print $1}'):$PORT"
echo "  Bind:  $BIND_HOST:$PORT"
case "$TOKEN_ORIGIN" in
    generated) echo "  Token: $VPN_API_TOKEN   (new — paste into the dashboard's Token dialog)" ;;
    rotated)   echo "  Token: $VPN_API_TOKEN   (rotated — every existing browser must re-enter it)" ;;
    stored)    echo "  Auth:  token set (${#VPN_API_TOKEN} chars)" ;;
esac
if [ "$TOKEN_SAVED" -eq 0 ]; then
    echo "  WARN:  could not write $ENV_FILE — this token is valid for this run only."
elif [ "$TOKEN_ORIGIN" != "stored" ]; then
    echo "         Saved to $ENV_FILE"
fi
if [ -n "$HOME_IP" ]; then
    echo "  Home IP: $HOME_IP"
fi
if [[ "${ACCESS_LOG,,}" =~ ^(1|true|yes)$ ]]; then
    echo "  Access log: on"
else
    echo "  Access log: off (set ACCESS_LOG=1 to enable)"
fi
echo ""

# Check Python version is 3.9+
py_version=$("$VPN_PYTHON" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null)
py_major=$("$VPN_PYTHON" -c "import sys; print(sys.version_info.major)" 2>/dev/null)
py_minor=$("$VPN_PYTHON" -c "import sys; print(sys.version_info.minor)" 2>/dev/null)
if [ -z "$py_version" ] || [ "$py_major" -lt 3 ] || { [ "$py_major" -eq 3 ] && [ "$py_minor" -lt 9 ]; }; then
    echo "ERROR: Python 3.9 or higher is required (found: ${py_version:-none})"
    echo ""
    echo "Install with:"
    echo "  sudo apt install python3 python3-venv"
    echo ""
    exit 1
fi
echo "  Python: $py_version ($VPN_PYTHON_KIND)"

# Check required Python packages are installed
missing=()
for pkg in flask requests; do
    if ! "$VPN_PYTHON" -c "import $pkg" 2>/dev/null; then
        missing+=("$pkg")
    fi
done
if [ ${#missing[@]} -gt 0 ]; then
    echo "ERROR: Missing Python package(s): ${missing[*]}"
    echo ""
    if [ "$VPN_PYTHON_KIND" = "venv" ]; then
        echo "The venv at $VPN_VENV is incomplete. Refresh it with:"
    else
        echo "Create the project venv and install dependencies with:"
    fi
    echo "  $SCRIPT_DIR/setup_venv.sh"
    echo ""
    exit 1
fi

exec env \
    BIND_HOST="$BIND_HOST" \
    VPN_API_TOKEN="${VPN_API_TOKEN:-}" \
    HOME_IP="${HOME_IP:-}" \
    ACCESS_LOG="${ACCESS_LOG:-}" \
    "$VPN_PYTHON" "$SCRIPT_DIR/webapp/app.py"
