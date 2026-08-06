import ipaddress
import json
import logging
import os
import re
import secrets
import subprocess
import threading
from typing import Optional

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, render_template, request

from monitor import VPNMonitor, detect_external_ip, killswitch_blocking_outbound
from organizer import scan_directory, organize_files

load_dotenv()

if os.environ.get("ACCESS_LOG", "").strip().lower() not in ("1", "true", "yes"):
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

app = Flask(__name__)

# Cap request bodies — the .ovpn upload path reads the whole file into memory,
# so an unbounded POST is an easy way to OOM a Pi.
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024

monitor: Optional[VPNMonitor] = None

_ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


def _load_or_create_token():
    """Return the API token, generating and persisting one when unset.

    The API is never served unauthenticated. Every endpoint below reaches a
    sudo-backed operation, and because a cross-origin HTML form cannot set an
    Authorization header, requiring a token is also what stops any website the
    operator visits from driving these routes via CSRF. A generated token is
    written to webapp/.env (mode 0600) so it survives restarts.
    """
    token = os.environ.get("VPN_API_TOKEN", "").strip()
    if token:
        return token

    token = secrets.token_urlsafe(32)
    try:
        gap = "\n" if os.path.exists(_ENV_PATH) and os.path.getsize(_ENV_PATH) else ""
        with open(_ENV_PATH, "a", encoding="utf-8") as fh:
            fh.write(f"{gap}VPN_API_TOKEN={token}\n")
        os.chmod(_ENV_PATH, 0o600)
        saved_to = _ENV_PATH
    except OSError as exc:
        saved_to = f"NOT SAVED ({exc}) - this token lasts only until restart"

    print(f"\n  No VPN_API_TOKEN was set, so one was generated.\n"
          f"  Token:    {token}\n"
          f"  Saved to: {saved_to}\n"
          f"  Enter it once in the web UI to authenticate.\n", flush=True)
    return token


_API_TOKEN = _load_or_create_token().encode("utf-8")


def _bearer_token():
    """Extract the presented token from the Authorization header."""
    header = request.headers.get("Authorization", "").strip()
    parts = header.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return header


def _check_token(token):
    # compare_digest raises TypeError on non-ASCII str, so compare bytes.
    if not secrets.compare_digest(token.encode("utf-8", "replace"), _API_TOKEN):
        return jsonify({"error": "Unauthorized"}), 401
    return None


def _auth():
    """Return a 401 response if the token is wrong, else None."""
    return _check_token(_bearer_token())


def _auth_sse():
    """Like _auth() but also accepts ?token= query param (EventSource can't set headers)."""
    return _check_token(_bearer_token() or request.args.get("token", ""))


def _require_monitor():
    if monitor is None:
        return jsonify({"error": "No home IP configured"}), 400
    return None


def _ip_lookup_error():
    """Explain why an external IP lookup failed.

    The usual cause is not a dead lookup service: the kill switch is fail-closed
    and survives reboots, so a previous session that ended badly leaves outgoing
    traffic blocked — including the request used to capture the pre-VPN home IP.
    Saying only "could not detect" sends people hunting the wrong problem.
    """
    if killswitch_blocking_outbound():
        return ("The kill switch is still active from a previous session, so all "
                "outbound requests are blocked and your home IP cannot be detected. "
                "Click Stop VPN, or run ./remove_killswitch.sh on the Pi, then retry. "
                "You can also set HOME_IP in webapp/.env to skip detection entirely.")
    return ("Could not determine external IP — check this machine has internet access, "
            "or set HOME_IP in webapp/.env to skip detection.")


def _organizer_roots():
    """Directories the file organizer is permitted to touch.

    ORGANIZER_ROOTS is a comma-separated list. It defaults to the user's home
    directory so the organizer keeps working without configuration, while still
    refusing paths like /etc or /.
    """
    raw = os.environ.get("ORGANIZER_ROOTS", "").strip()
    entries = [p.strip() for p in raw.split(",") if p.strip()] or [os.path.expanduser("~")]
    return [os.path.realpath(p) for p in entries]


def _resolve_under_roots(path):
    """Return the resolved path if it sits inside an allowed root, else None."""
    resolved = os.path.realpath(path)
    for root in _organizer_roots():
        if resolved == root or resolved.startswith(root + os.sep):
            return resolved
    return None


@app.after_request
def _security_headers(resp):
    """Deny framing — a clickjacked "Stop All" tears down the kill switch —
    and keep the SSE token out of Referer headers. Both templates use inline
    handlers, so script-src must still allow 'unsafe-inline'.
    """
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'",
    )
    return resp


# ------------------------------------------------------------------ pages

@app.route("/")
def index():
    # home_ip is the operator's real pre-VPN ISP address — the one value this
    # project exists to conceal — so it is not rendered into a page that has to
    # stay loadable before a token is supplied. The UI reads it from
    # /api/status instead, which is authenticated.
    return render_template("index.html")


@app.route("/organizer")
def organizer():
    return render_template("organizer.html")


# ------------------------------------------------------------------ API

@app.route("/api/detect-ip")
def detect_ip():
    err = _auth()
    if err:
        return err
    ip = detect_external_ip()
    if ip:
        return jsonify({"ip": ip})
    return jsonify({"error": _ip_lookup_error()}), 503


@app.route("/api/status")
def status():
    err = _auth() or _require_monitor()
    if err:
        return err
    # Always reflect live system state for VPN/qbt, not just cached monitor values
    live = dict(monitor.status)
    live["vpn_process"] = monitor.check_openvpn_process()
    live["vpn_interface"] = monitor.check_vpn_interface()
    live["vpn_route"] = monitor.check_default_route()
    live["qbittorrent"] = monitor.is_qbittorrent_running()
    live["vpn_starting"] = monitor.status.get("vpn_starting", False)
    live["kill_switch_active"] = monitor.check_killswitch_active()
    live["home_ip"] = monitor.home_ip
    return jsonify(live)


@app.route("/api/logs/recent")
def logs_recent():
    err = _auth() or _require_monitor()
    if err:
        return err
    return jsonify(monitor.recent_logs())


@app.route("/api/logs/stream")
def logs_stream():
    err = _auth_sse() or _require_monitor()
    if err:
        return err

    from_seq = request.args.get("from_seq", 0, type=int)

    def generate():
        for line in monitor.stream_logs(from_seq=from_seq):
            if line is None:
                yield "event: keepalive\ndata: {}\n\n"
            else:
                yield f"data: {json.dumps(line)}\n\n"

    return Response(generate(), mimetype="text/event-stream",
                    headers={"X-Accel-Buffering": "no"})


@app.route("/api/vpn/running")
def vpn_running():
    """Check if OpenVPN is running — does not require a configured monitor."""
    err = _auth()
    if err:
        return err
    try:
        result = subprocess.run(["pgrep", "-x", "openvpn"], capture_output=True, timeout=2)
        return jsonify({"running": result.returncode == 0})
    except Exception:
        return jsonify({"running": False})


@app.route("/api/vpn/download-config", methods=["POST"])
def vpn_download_config():
    err = _auth() or _require_monitor()
    if err:
        return err
    url = (request.get_json(force=True) or {}).get("url", "").strip()
    if not url:
        return jsonify({"error": "url required"}), 400
    monitor.download_ovpn(url)
    return jsonify({"started": True})


@app.route("/api/vpn/upload-config", methods=["POST"])
def vpn_upload_config():
    err = _auth() or _require_monitor()
    if err:
        return err
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "no file uploaded"}), 400
    filename = os.path.basename(f.filename or "config.ovpn")  # strip path components
    # Must be a real name, not a bare ".ovpn" dotfile — glob("*.ovpn") would not
    # match that, so accepting it wipes the installed config and leaves nothing
    # the VPN can start from.
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}\.ovpn", filename) or filename.startswith("."):
        return jsonify({"error": "invalid filename — use letters, digits, . _ - and a .ovpn extension"}), 400
    monitor.upload_ovpn(f.read(), filename)
    return jsonify({"started": True})


@app.route("/api/vpn/start", methods=["POST"])
def vpn_start():
    err = _auth() or _require_monitor()
    if err:
        return err
    monitor.start_vpn()
    return jsonify({"started": True})


@app.route("/api/vpn/stop", methods=["POST"])
def vpn_stop():
    err = _auth() or _require_monitor()
    if err:
        return err
    monitor.stop_vpn_bg()
    return jsonify({"stopped": True})


@app.route("/api/start", methods=["POST"])
def start():
    err = _auth() or _require_monitor()
    if err:
        return err
    if monitor.status.get("vpn_starting"):
        return jsonify({"error": "VPN is still starting — wait for it to finish"}), 400
    if not monitor.check_openvpn_process() or not monitor.check_vpn_interface():
        return jsonify({"error": "VPN is not running. Start VPN first."}), 400
    started = monitor.start()
    return jsonify({"started": started})


@app.route("/api/stop", methods=["POST"])
def stop():
    err = _auth() or _require_monitor()
    if err:
        return err
    monitor.stop()
    return jsonify({"stopped": True})


@app.route("/api/qbt/start", methods=["POST"])
def qbt_start():
    err = _auth() or _require_monitor()
    if err:
        return err
    threading.Thread(target=monitor.start_qbittorrent, daemon=True).start()
    return jsonify({"started": True})


@app.route("/api/qbt/stop", methods=["POST"])
def qbt_stop():
    err = _auth() or _require_monitor()
    if err:
        return err
    threading.Thread(target=monitor.stop_qbittorrent, daemon=True).start()
    return jsonify({"stopped": True})


@app.route("/api/stop-all", methods=["POST"])
def stop_all():
    err = _auth() or _require_monitor()
    if err:
        return err
    threading.Thread(target=monitor.stop_all, daemon=True).start()
    return jsonify({"stopped": True})


@app.route("/api/reconnect", methods=["POST"])
def reconnect():
    err = _auth() or _require_monitor()
    if err:
        return err
    threading.Thread(target=monitor.attempt_reconnect, daemon=True).start()
    return jsonify({"started": True})


@app.route("/api/configure", methods=["POST"])
def configure():
    err = _auth()
    if err:
        return err
    global monitor
    data = request.get_json(force=True)
    home_ip = (data.get("home_ip") or "").strip()
    if not home_ip:
        home_ip = os.environ.get("HOME_IP", "").strip() or detect_external_ip()
    if not home_ip:
        return jsonify({"error": _ip_lookup_error()}), 503

    try:
        addr = ipaddress.ip_address(home_ip)
        if not isinstance(addr, ipaddress.IPv4Address) or addr.is_unspecified or addr.is_reserved:
            raise ValueError
    except ValueError:
        return jsonify({"error": "Invalid home IP address"}), 400

    try:
        fast_interval = max(1, min(int(data.get("fast_interval", 2)), 60))
        ip_interval = max(5, min(int(data.get("ip_interval", 5)), 300))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid interval value"}), 400

    if monitor and monitor.status["running"]:
        monitor.stop()

    monitor = VPNMonitor(
        home_ip=home_ip,
        fast_interval=fast_interval,
        ip_interval=ip_interval,
    )
    return jsonify({"configured": True, "home_ip": home_ip})


# ------------------------------------------------------------------ file organizer

@app.route("/api/files/scan")
def files_scan():
    err = _auth()
    if err:
        return err
    source_dir = request.args.get("dir", "").strip()
    if not source_dir:
        return jsonify({"error": "dir parameter required"}), 400
    source_dir = _resolve_under_roots(source_dir)
    if source_dir is None:
        return jsonify({"error": "Directory is outside the permitted roots"}), 403
    if not os.path.isdir(source_dir):
        return jsonify({"error": "Directory not found"}), 404
    exclude_dirs = {d.strip() for d in request.args.get("exclude", "").split(",") if d.strip()}
    try:
        files = scan_directory(source_dir, exclude_dirs)
        return jsonify({"source_dir": source_dir, "files": files})
    except Exception:
        app.logger.exception("scan failed for %s", source_dir)
        return jsonify({"error": "Scan failed"}), 500


@app.route("/api/files/organize", methods=["POST"])
def files_organize():
    err = _auth()
    if err:
        return err
    data = request.get_json(force=True) or {}
    source_dir = (data.get("source_dir") or "").strip()
    operations = data.get("files", [])
    if not source_dir:
        return jsonify({"error": "source_dir required"}), 400
    if not isinstance(operations, list) or not operations:
        return jsonify({"error": "files list required"}), 400
    source_dir = _resolve_under_roots(source_dir)
    if source_dir is None:
        return jsonify({"error": "Directory is outside the permitted roots"}), 403
    if not os.path.isdir(source_dir):
        return jsonify({"error": "Directory not found"}), 404
    try:
        results = organize_files(source_dir, operations)
        return jsonify({"results": results})
    except Exception:
        app.logger.exception("organize failed for %s", source_dir)
        return jsonify({"error": "Organize failed"}), 500


# ------------------------------------------------------------------ main

if __name__ == "__main__":
    home_ip = os.environ.get("HOME_IP", "").strip()
    if home_ip:
        monitor = VPNMonitor(home_ip)

    bind_host = os.environ.get("BIND_HOST", "0.0.0.0").strip() or "0.0.0.0"
    # A bare "PORT=" line in .env yields "", which int() would reject.
    port_raw = os.environ.get("PORT", "").strip() or "5000"
    try:
        port = int(port_raw)
    except ValueError:
        print(f"WARNING: PORT={port_raw!r} is not a number — using 5000", flush=True)
        port = 5000
    app.run(host=bind_host, port=port, threaded=True)
