import json
import os
import secrets
import subprocess
import threading

from flask import Flask, Response, jsonify, render_template, request

from monitor import VPNMonitor, detect_external_ip
from organizer import scan_directory, organize_files

app = Flask(__name__)

monitor: VPNMonitor | None = None

_API_TOKEN = os.environ.get("VPN_API_TOKEN", "").strip()


def _auth():
    """Return a 401 response if the token is wrong, else None."""
    if not _API_TOKEN:
        return None  # auth disabled when no token configured
    auth_header = request.headers.get("Authorization", "")
    token = auth_header.removeprefix("Bearer ").strip()
    if not secrets.compare_digest(token, _API_TOKEN):
        return jsonify({"error": "Unauthorized"}), 401
    return None


def _auth_sse():
    """Like _auth() but also accepts ?token= query param (EventSource can't set headers)."""
    if not _API_TOKEN:
        return None
    auth_header = request.headers.get("Authorization", "")
    token = auth_header.removeprefix("Bearer ").strip() or request.args.get("token", "")
    if not secrets.compare_digest(token, _API_TOKEN):
        return jsonify({"error": "Unauthorized"}), 401
    return None


def _require_monitor():
    if monitor is None:
        return jsonify({"error": "No home IP configured"}), 400
    return None


# ------------------------------------------------------------------ pages

@app.route("/")
def index():
    return render_template("index.html", home_ip=monitor.home_ip if monitor else "")


# ------------------------------------------------------------------ API

@app.route("/api/detect-ip")
def detect_ip():
    err = _auth()
    if err:
        return err
    ip = detect_external_ip()
    if ip:
        return jsonify({"ip": ip})
    return jsonify({"error": "Could not determine external IP"}), 503


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
        result = subprocess.run(["pgrep", "-f", "openvpn"], capture_output=True, timeout=2)
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
    filename = f.filename or "config.ovpn"
    filename = os.path.basename(filename)  # strip any path components
    if not filename.endswith(".ovpn"):
        return jsonify({"error": "file must have a .ovpn extension"}), 400
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
        home_ip = detect_external_ip()
    if not home_ip:
        return jsonify({"error": "Could not detect home IP"}), 503

    if monitor and monitor.status["running"]:
        monitor.stop()

    monitor = VPNMonitor(
        home_ip=home_ip,
        fast_interval=int(data.get("fast_interval", 2)),
        ip_interval=int(data.get("ip_interval", 5)),
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
    source_dir = os.path.realpath(source_dir)
    if not os.path.isdir(source_dir):
        return jsonify({"error": "Directory not found"}), 404
    try:
        files = scan_directory(source_dir)
        return jsonify({"source_dir": source_dir, "files": files})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


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
    source_dir = os.path.realpath(source_dir)
    if not os.path.isdir(source_dir):
        return jsonify({"error": "Directory not found"}), 404
    try:
        results = organize_files(source_dir, operations)
        return jsonify({"results": results})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ------------------------------------------------------------------ main

if __name__ == "__main__":
    home_ip = os.environ.get("HOME_IP", "").strip()
    if home_ip:
        monitor = VPNMonitor(home_ip)

    bind_host = os.environ.get("BIND_HOST", "0.0.0.0").strip()
    app.run(host=bind_host, port=5000, threaded=True)
