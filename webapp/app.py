import json
import os
import threading

from flask import Flask, Response, jsonify, render_template, request

from monitor import VPNMonitor, detect_external_ip

app = Flask(__name__)

monitor: VPNMonitor | None = None


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
    ip = detect_external_ip()
    if ip:
        return jsonify({"ip": ip})
    return jsonify({"error": "Could not determine external IP"}), 503


@app.route("/api/status")
def status():
    err = _require_monitor()
    if err:
        return err
    # Always reflect live system state for VPN/qbt, not just cached monitor values
    live = dict(monitor.status)
    live["vpn_process"] = monitor.check_openvpn_process()
    live["vpn_interface"] = monitor.check_vpn_interface()
    live["qbittorrent"] = monitor.is_qbittorrent_running()
    return jsonify(live)


@app.route("/api/logs/recent")
def logs_recent():
    err = _require_monitor()
    if err:
        return err
    return jsonify(monitor.recent_logs())


@app.route("/api/logs/stream")
def logs_stream():
    err = _require_monitor()
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


@app.route("/api/vpn/download-config", methods=["POST"])
def vpn_download_config():
    err = _require_monitor()
    if err:
        return err
    url = (request.get_json(force=True) or {}).get("url", "").strip()
    if not url:
        return jsonify({"error": "url required"}), 400
    monitor.download_ovpn(url)
    return jsonify({"started": True})


@app.route("/api/vpn/start", methods=["POST"])
def vpn_start():
    err = _require_monitor()
    if err:
        return err
    monitor.start_vpn()
    return jsonify({"started": True})


@app.route("/api/vpn/stop", methods=["POST"])
def vpn_stop():
    err = _require_monitor()
    if err:
        return err
    monitor.stop_vpn_bg()
    return jsonify({"stopped": True})


@app.route("/api/start", methods=["POST"])
def start():
    err = _require_monitor()
    if err:
        return err
    started = monitor.start()
    return jsonify({"started": started})


@app.route("/api/stop", methods=["POST"])
def stop():
    err = _require_monitor()
    if err:
        return err
    monitor.stop()
    return jsonify({"stopped": True})


@app.route("/api/qbt/start", methods=["POST"])
def qbt_start():
    err = _require_monitor()
    if err:
        return err
    threading.Thread(target=monitor.start_qbittorrent, daemon=True).start()
    return jsonify({"started": True})


@app.route("/api/qbt/stop", methods=["POST"])
def qbt_stop():
    err = _require_monitor()
    if err:
        return err
    threading.Thread(target=monitor.stop_qbittorrent, daemon=True).start()
    return jsonify({"stopped": True})



@app.route("/api/reconnect", methods=["POST"])
def reconnect():
    err = _require_monitor()
    if err:
        return err
    success = monitor.attempt_reconnect()
    return jsonify({"success": success})


@app.route("/api/configure", methods=["POST"])
def configure():
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
        ip_interval=int(data.get("ip_interval", 10)),
        max_reconnects=int(data.get("max_reconnects", 3)),
    )
    return jsonify({"configured": True, "home_ip": home_ip})


# ------------------------------------------------------------------ main

if __name__ == "__main__":
    home_ip = os.environ.get("HOME_IP", "").strip()
    if home_ip:
        monitor = VPNMonitor(home_ip)

    app.run(host="0.0.0.0", port=5000, threaded=True)
