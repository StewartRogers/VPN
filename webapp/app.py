import ipaddress
import json
import logging
import os
import secrets
import subprocess
import threading
import time
import uuid
from typing import Optional

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, render_template, request

from monitor import VPNMonitor, detect_external_ip, read_config_value
import organizer as organizer_mod
from organizer import scan_directory, organize_files

load_dotenv()

if os.environ.get("ACCESS_LOG", "").strip().lower() not in ("1", "true", "yes"):
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

app = Flask(__name__)

monitor: Optional[VPNMonitor] = None

_API_TOKEN = os.environ.get("VPN_API_TOKEN", "").strip()

if not _API_TOKEN:
    print("WARNING: VPN_API_TOKEN is not set - the API is unauthenticated. "
          "Set VPN_API_TOKEN=<secret> to require a bearer token.", flush=True)


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
    save_path = monitor.save_path if monitor else read_config_value("QBT_SAVE_PATH", "")
    return render_template("index.html", home_ip=monitor.home_ip if monitor else "", save_path=save_path)


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
    live["save_path"] = monitor.save_path
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
    filename = f.filename or "config.ovpn"
    filename = os.path.basename(filename)  # strip any path components
    if not filename.endswith(".ovpn"):
        return jsonify({"error": "file must have a .ovpn extension"}), 400
    monitor.upload_ovpn(f.read(), filename)
    return jsonify({"started": True})


def _ordering_violation(step):
    """Enforce the start/stop ordering server-side.

    The UI disables buttons that would break the order, but that is a hint and
    not a control: curl, a stale tab, or a state change between 3s status polls
    all still reach these endpoints. Stopping a layer while something above it
    is still running is what leaves a torrent client on an unprotected link.
    """
    if step == "vpn_start":
        if monitor.status.get("vpn_starting"):
            return "VPN is still starting"
        if monitor.check_openvpn_process():
            return "VPN is already running"
    elif step == "vpn_stop":
        if monitor.is_qbittorrent_running():
            return "Stop qBittorrent before stopping the VPN"
        if monitor.status.get("running"):
            return "Stop the monitor before stopping the VPN"
    elif step == "monitor_stop":
        if monitor.is_qbittorrent_running():
            return "Stop qBittorrent before stopping the monitor"
    elif step == "reconnect":
        if monitor.is_qbittorrent_running():
            return "Stop qBittorrent before reconnecting the VPN"
    return None


@app.route("/api/vpn/start", methods=["POST"])
def vpn_start():
    err = _auth() or _require_monitor()
    if err:
        return err
    bad = _ordering_violation("vpn_start")
    if bad:
        return jsonify({"error": bad}), 409
    monitor.start_vpn()
    return jsonify({"started": True})


@app.route("/api/vpn/stop", methods=["POST"])
def vpn_stop():
    err = _auth() or _require_monitor()
    if err:
        return err
    bad = _ordering_violation("vpn_stop")
    if bad:
        return jsonify({"error": bad}), 409
    monitor.stop_vpn_bg()
    return jsonify({"stopped": True})


@app.route("/api/vpn/cancel-retry", methods=["POST"])
def vpn_cancel_retry():
    err = _auth() or _require_monitor()
    if err:
        return err
    monitor.cancel_retry()
    return jsonify({"cancelled": True})


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
    bad = _ordering_violation("monitor_stop")
    if bad:
        return jsonify({"error": bad}), 409
    monitor.stop()
    return jsonify({"stopped": True})


@app.route("/api/qbt/start", methods=["POST"])
def qbt_start():
    err = _auth() or _require_monitor()
    if err:
        return err
    # The UI disables its Start button unless the monitor reports secure, but
    # that is a client-side hint only: curl, a stale tab, or a VPN drop between
    # status polls all still reach this endpoint. Torrent traffic must never
    # begin unless the tunnel is verified here, on the server.
    blocked = monitor.torrent_start_blocked()
    if blocked:
        return jsonify({"error": f"Refusing to start qBittorrent — {blocked}"}), 409
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
    bad = _ordering_violation("reconnect")
    if bad:
        return jsonify({"error": bad}), 409
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

    try:
        addr = ipaddress.ip_address(home_ip)
        if not isinstance(addr, ipaddress.IPv4Address) or addr.is_unspecified or addr.is_reserved:
            raise ValueError
    except ValueError:
        return jsonify({"error": "Invalid home IP address"}), 400

    try:
        fast_interval = max(1, min(int(data.get("fast_interval", 2)), 60))
        # Default 10s, matching VPNMonitor, checkip.sh and vpn_config.conf.
        # The kill-switch probe rides this cadence too, so a lower default
        # doubles the ufw work for no extra leak protection.
        ip_interval = max(5, min(int(data.get("ip_interval", 10)), 300))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid interval value"}), 400

    save_path = (data.get("save_path") or "").strip()
    if save_path:
        save_path = os.path.expanduser(save_path)
        try:
            os.makedirs(save_path, exist_ok=True)
        except Exception as e:
            return jsonify({"error": f"Could not create save path: {e}"}), 400

    if monitor and monitor.status["running"]:
        monitor.stop()

    monitor = VPNMonitor(
        home_ip=home_ip,
        fast_interval=fast_interval,
        ip_interval=ip_interval,
    )
    monitor.set_save_path(save_path)
    return jsonify({"configured": True, "home_ip": home_ip, "save_path": save_path})


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
    exclude_dirs = {d.strip() for d in request.args.get("exclude", "").split(",") if d.strip()}
    # Destination folders, sent as repeated exclude_path params. They may sit
    # inside the source, and re-listing an already-filed library is noise at
    # best — every row would come back "Already in the 'movies' folder".
    exclude_paths = [p for p in request.args.getlist("exclude_path") if p.strip()]
    try:
        files = scan_directory(source_dir, exclude_dirs, exclude_paths=exclude_paths)
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


# ------------------------------------------------- organizer: browse / move / clean

# Move jobs run in a background thread: a cross-filesystem copy of a 20GB file
# is not something an HTTP request can wait on. The job record is how the UI
# answers "is the move finished yet" - and the delete step is gated on it.
#
# The record is mirrored to disk so it outlives both the browser tab and the
# web app. Before that, the job id existed only in a JavaScript variable: a
# reload during a 20GB copy left the copy running with nothing able to see it,
# and step 4 could never unlock.
_jobs = {}
_jobs_lock = threading.Lock()

_VPN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_JOBS_FILE = (os.environ.get("ORGANIZER_JOBS_FILE", "").strip()
              or os.path.join(_VPN_DIR, "organizer_jobs.json"))
_JOBS_KEEP = 20


def _save_jobs():
    """Write the job records out atomically.

    Called when a job starts, when each file finishes and when the job ends —
    not on every chunk. Per-chunk byte counts are a progress display; writing
    them would mean a disk write every few megabytes on an SD card.
    """
    try:
        with _jobs_lock:
            recent = sorted(_jobs.values(), key=lambda j: j.get("started", 0),
                            reverse=True)[:_JOBS_KEEP]
            for stale in list(_jobs):
                if stale not in {j["id"] for j in recent}:
                    del _jobs[stale]
            payload = json.dumps({"jobs": recent}, indent=1)
        tmp = _JOBS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, _JOBS_FILE)      # readers never see a half-written file
    except OSError as exc:
        print(f"WARNING: could not persist organizer jobs to {_JOBS_FILE}: {exc}",
              flush=True)


def _load_jobs():
    """Restore job records at startup.

    A job still marked "running" belonged to a process that is gone, so its
    copy thread died with it. It becomes "interrupted", never "complete": the
    delete step is gated on "complete", and a job that stopped mid-copy has
    results it never finished writing. The user rescans and moves again — the
    files that did complete are recorded as moved and will be skipped as
    duplicates on the second pass.
    """
    try:
        with open(_JOBS_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return
    for job in data.get("jobs", []):
        if not isinstance(job, dict) or "id" not in job:
            continue
        if job.get("state") == "running":
            job["state"] = "interrupted"
            job["current"] = None
        _jobs[job["id"]] = job


_load_jobs()


@app.route("/api/files/browse")
def files_browse():
    err = _auth()
    if err:
        return err
    try:
        return jsonify(organizer_mod.browse(request.args.get("path", "/")))
    except NotADirectoryError:
        return jsonify({"error": "Not a directory"}), 404
    except PermissionError:
        return jsonify({"error": "Permission denied"}), 403
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/files/move", methods=["POST"])
def files_move():
    err = _auth()
    if err:
        return err
    data = request.get_json(force=True)
    source_dir = os.path.realpath((data.get("source_dir") or "").strip())
    operations = data.get("operations") or []
    if not os.path.isdir(source_dir):
        return jsonify({"error": "Source directory not found"}), 404

    # Destinations are declared once, by label, and each file names a label.
    # Resolving them here rather than per-file is what keeps the validation
    # honest: an operation can only ever land in a folder the request declared
    # up front, so a crafted `dest` cannot write outside the chosen roots.
    declared = data.get("destinations") or {}
    if not isinstance(declared, dict):
        return jsonify({"error": "destinations must be an object"}), 400
    if data.get("output_dir"):           # single-folder form, still supported
        declared = dict(declared, output=data["output_dir"])
    dests = {}
    for label, path in declared.items():
        # Check the raw string BEFORE realpath: os.path.realpath("") returns the
        # process's working directory, not "", so an unset destination used to
        # sail past the emptiness check and resolve to wherever the web app was
        # started from — the repo checkout. Files were then moved into it and
        # the job reported success.
        raw = (path or "").strip()
        if not raw:
            return jsonify({"error": f"Destination '{label}' is not set"}), 400
        path = os.path.realpath(raw)
        if path == "/":
            return jsonify({"error": f"Destination '{label}' is not set"}), 400
        # A destination *inside* the source is allowed — organizing
        # /mnt/media into /mnt/media/Movies is the normal library layout. What
        # made that unsafe was not the nesting itself but the delete step: a
        # rescan picks the already-filed library back up, and cleaning its
        # folders deletes artwork and .nfo sidecars, which count as junk. Two
        # guards below close that off instead of banning the layout: _plan()
        # refuses a source already inside a destination, and the cleanup step
        # never descends into one. The source folder itself is still refused —
        # it would make dst == src for every file sitting at the root.
        if path == source_dir:
            return jsonify({"error": f"Destination '{label}' cannot be the "
                                     "source folder itself"}), 400
        try:
            os.makedirs(path, exist_ok=True)
        except Exception as exc:
            return jsonify({"error": f"Could not create '{label}' folder: {exc}"}), 400
        dests[label] = path
    if not dests:
        return jsonify({"error": "At least one destination folder is required"}), 400

    def _plan(op):
        """Resolve one operation to (src, dst, error)."""
        rel = op.get("original", "")
        src = os.path.realpath(os.path.join(source_dir, rel))
        if not src.startswith(source_dir + os.sep) or not os.path.isfile(src):
            return src, None, "File not found in source"
        # Already filed. Re-moving it would mark its folder as `moved` and hand
        # the destination to the delete step, which strips artwork and .nfo
        # files as junk. Only reachable when a destination sits inside the
        # source and a rescan swept the library back up.
        for dlabel, droot in dests.items():
            if src == droot or src.startswith(droot + os.sep):
                return src, None, f"Already in the '{dlabel}' folder"
        label = op.get("dest") or ("output" if "output" in dests else None)
        root = dests.get(label)
        if not root:
            return src, None, f"No destination chosen ({label or 'unset'})"
        name = os.path.basename(op.get("rename_to") or op.get("proposed") or rel)
        dst = os.path.realpath(os.path.join(root, name))
        if not dst.startswith(root + os.sep):
            return src, None, "Destination escapes the output folder"
        return src, dst, None

    job_id = uuid.uuid4().hex[:12]
    total_bytes = 0
    for op in operations:
        src, dst, bad = _plan(op)
        if not bad:
            total_bytes += os.path.getsize(src)

    job = {"id": job_id, "state": "running",
           "done_bytes": 0, "total_bytes": total_bytes,
           "done_files": 0, "total_files": len(operations),
           "current": None, "current_bytes": 0, "current_total": 0,
           "results": [], "source_dir": source_dir,
           "destinations": dests,
           "started": time.time(), "finished": None}
    with _jobs_lock:
        _jobs[job_id] = job
    _save_jobs()

    def run():
        for op in operations:
            rel = op.get("original", "")
            src, dst, bad = _plan(op)
            if bad:
                job["results"].append({"original": rel, "status": "error",
                                       "message": bad})
                job["done_files"] += 1
                _save_jobs()
                continue
            size = os.path.getsize(src)
            job["current"] = rel
            job["current_total"] = size
            job["current_bytes"] = 0
            base = job["done_bytes"]

            def progress(done, total, _base=base):
                job["current_bytes"] = done
                job["done_bytes"] = _base + done

            res = organizer_mod.move_file(src, dst, chunk_cb=progress)
            job["done_bytes"] = base + (res.get("bytes") or 0)
            job["done_files"] += 1
            res["original"] = rel
            res["destination"] = dst
            job["results"].append(res)
            _save_jobs()
        job["current"] = None
        job["current_bytes"] = job["current_total"] = 0
        # A run in which every file errored is not a completed move. It used to
        # be marked "complete" regardless, which is the flag step 4 unlocks on,
        # so a job that moved nothing presented as a success with a live Delete
        # button. Partial success stays "complete": the files that did move are
        # real, and cleanup only ever visits folders reported as `moved`.
        moved = any(r.get("status") == "moved" for r in job["results"])
        errored = any(r.get("status") == "error" for r in job["results"])
        job["state"] = "failed" if errored and not moved else "complete"
        job["finished"] = time.time()
        _save_jobs()

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/api/files/move", methods=["GET"])
def files_move_list():
    """Job records, newest first — how a reloaded page finds a move in flight.

    Summary fields only: the results list of a large job is far bigger than
    anything the picker needs, and /api/files/move/<id> serves it in full.
    """
    err = _auth()
    if err:
        return err
    with _jobs_lock:
        jobs = sorted(_jobs.values(), key=lambda j: j.get("started", 0), reverse=True)
        summary = [{k: j.get(k) for k in
                    ("id", "state", "started", "finished", "source_dir",
                     "done_bytes", "total_bytes", "done_files", "total_files",
                     "current", "current_bytes", "current_total")}
                   for j in jobs]
        for row, job in zip(summary, jobs):
            row["moved"] = sum(1 for r in job.get("results", [])
                               if r.get("status") == "moved")
    return jsonify({"jobs": summary})


@app.route("/api/files/move/<job_id>")
def files_move_status(job_id):
    err = _auth()
    if err:
        return err
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        return jsonify({"error": "Unknown job"}), 404
    return jsonify(job)


@app.route("/api/files/cleanup", methods=["POST"])
def files_cleanup():
    err = _auth()
    if err:
        return err
    data = request.get_json(force=True)
    job_id = (data.get("job_id") or "").strip()
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        return jsonify({"error": "Unknown job - run the move step first"}), 404
    # The delete step only becomes available once the move is genuinely
    # finished; deleting sources under an in-flight copy destroys the only
    # complete copy of the file.
    if job["state"] == "interrupted":
        return jsonify({"error": "That move was interrupted before it finished — "
                                 "scan and move again before deleting sources"}), 409
    if job["state"] == "failed":
        return jsonify({"error": "That move did not move anything — every file "
                                 "failed, so there are no sources to delete"}), 409
    if job["state"] != "complete":
        return jsonify({"error": "Move is still running"}), 409
    moved = [r for r in job["results"] if r.get("status") == "moved"]
    if not moved:
        return jsonify({"results": [], "message": "Nothing was moved - nothing to clean"})

    source_dir = job["source_dir"]
    # Destinations may legitimately sit inside the source tree, and the delete
    # step treats .nfo/.jpg/.txt as junk — so a destination must never be
    # walked, whatever the results list says.
    dest_roots = [os.path.realpath(p) for p in (job.get("destinations") or {}).values()]

    def _in_destination(path):
        return any(path == root or path.startswith(root + os.sep) for root in dest_roots)

    folders, results = [], []
    for r in moved:
        d = os.path.dirname(os.path.realpath(os.path.join(source_dir, r["original"])))
        if d != source_dir and d not in folders and not _in_destination(d):
            folders.append(d)
    for folder in folders:
        results.extend(organizer_mod.cleanup_source(folder, source_dir))
    return jsonify({"results": results})


# ------------------------------------------------------------------ main

if __name__ == "__main__":
    home_ip = os.environ.get("HOME_IP", "").strip()
    if home_ip:
        monitor = VPNMonitor(home_ip)

    bind_host = os.environ.get("BIND_HOST", "0.0.0.0").strip()
    port = int(os.environ.get("PORT", "5000").strip())
    app.run(host=bind_host, port=port, threaded=True)
