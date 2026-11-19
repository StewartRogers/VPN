"""Auto-remove finished torrents from qBittorrent, leaving their files on disk.

Web path only: one background thread, started once from app.py's __main__
block. The web app is a single Flask process (app.run(threaded=True)), so a
start-once guard is all the "only one poller" protection this needs.

Each cycle reads its settings fresh from vpn_config.conf:

    QBT_AUTO_REMOVE=true|false      off when missing or unrecognised
    QBT_AUTO_REMOVE_INTERVAL=30     seconds, clamped to a minimum of 5

then lists completed torrents over the local WebUI API and removes each one
with deleteFiles=false, one torrent per call, so every log line names a
torrent that was actually removed.

What this deliberately does not touch: the tunnel, the kill switch and
qBittorrent.conf. It talks to 127.0.0.1 only, through the same qbt_api() the
tunnel bind uses (localhost no-auth first, QBT_WEBUI_USER / QBT_WEBUI_PASS as
the fallback).
"""
import subprocess
import threading

from monitor import qbt_api, read_config_value

DEFAULT_INTERVAL = 30
MIN_INTERVAL = 5
API_TIMEOUT = 10

# Completed torrents in these states are left for a later cycle. "moving" is
# mid-relocation of its data; "checkingUP" is complete but being rechecked;
# "checkingResumeData" is the client still loading it at startup, before its
# state is known for certain.
SKIP_STATES = frozenset({"moving", "checkingUP", "checkingResumeData"})

# Consecutive failing cycles before the outage is reported. The WebUI takes a
# few seconds to accept connections after qbittorrent-nox starts, so a single
# failure right after every launch is expected and not worth a warning.
OUTAGE_GRACE_CYCLES = 2

_TRUE = {"1", "true", "yes", "on"}


def auto_remove_enabled():
    return read_config_value("QBT_AUTO_REMOVE", "").strip().lower() in _TRUE


def auto_remove_interval():
    raw = read_config_value("QBT_AUTO_REMOVE_INTERVAL", "")
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL
    return max(MIN_INTERVAL, value)


def probe_qbittorrent():
    """Tri-state: True running, False confirmed not running, None could not ask."""
    try:
        r = subprocess.run(["pgrep", "-x", "qbittorrent-nox"],
                           capture_output=True, timeout=5)
    except Exception:
        return None
    if r.returncode == 0:
        return True
    if r.returncode == 1:
        return False
    return None


def _clean_name(name):
    """One printable line, whatever the torrent is called."""
    text = "".join(c if c.isprintable() else " " for c in str(name or "")).strip()
    return text or "(unnamed)"


class _ApiError(Exception):
    pass


class TorrentCleaner:
    """Background poller. `log(message, level=None)` is the output sink."""

    def __init__(self, log, api=qbt_api, is_running=probe_qbittorrent):
        self._log = log
        self._api = api
        self._is_running = is_running
        self._stop = threading.Event()
        self._thread = None
        self._failures = 0          # consecutive failing cycles
        self._outage_reported = False
        self._enabled = None        # last observed setting, for change logging

    # ---------------------------------------------------------------- thread

    def start(self):
        """Start the poller. A second call is a no-op."""
        if self._thread and self._thread.is_alive():
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="torrent-cleanup",
                                        daemon=True)
        self._thread.start()
        return True

    def stop(self, timeout=None):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)

    def _run(self):
        while not self._stop.is_set():
            self.run_once()
            # Read fresh so a config edit takes effect on the next cycle.
            try:
                interval = auto_remove_interval()
            except Exception:
                interval = DEFAULT_INTERVAL
            self._stop.wait(interval)

    # ----------------------------------------------------------------- cycle

    def run_once(self):
        """One cycle. Returns the number of torrents removed. Never raises."""
        try:
            return self._cycle()
        except Exception as e:  # never let the thread die
            self._failed(f"unexpected error: {e}")
            return 0

    def _cycle(self):
        enabled = auto_remove_enabled()
        if enabled != self._enabled:
            if enabled:
                self._log("Auto-remove of finished torrents is on "
                          f"(every {auto_remove_interval()}s; files are kept)")
            elif self._enabled is not None:
                self._log("Auto-remove of finished torrents is off")
            self._enabled = enabled
        if not enabled:
            self._reset_outage()
            return 0

        # Off whenever the VPN is: skip silently, and forget any outage, so a
        # normal stop/start of the client does not log a "recovered" line.
        if self._is_running() is False:
            self._reset_outage()
            return 0

        try:
            removed = self._remove_completed()
        except _ApiError as e:
            self._failed(str(e))
            return 0
        if self._outage_reported:
            self._log("qBittorrent WebUI reachable again; auto-remove resumed")
        self._reset_outage()
        return removed

    def _remove_completed(self):
        session, base = self._api()
        if not session:
            raise _ApiError(
                "cannot reach or log in to the qBittorrent WebUI. Set "
                "WebUI\\LocalHostAuth=false or add QBT_WEBUI_USER / "
                "QBT_WEBUI_PASS to vpn_config.conf.")
        try:
            r = session.get(f"{base}/api/v2/torrents/info",
                            params={"filter": "completed"}, timeout=API_TIMEOUT)
        except Exception as e:
            raise _ApiError(f"listing torrents failed: {e}")
        if r.status_code != 200:
            raise _ApiError(f"listing torrents returned HTTP {r.status_code}")
        try:
            torrents = r.json()
        except ValueError:
            raise _ApiError("listing torrents returned something other than JSON")

        removed = 0
        failed = []
        for t in torrents if isinstance(torrents, list) else []:
            if not isinstance(t, dict):
                continue
            torrent_hash = t.get("hash")
            if not torrent_hash or t.get("state") in SKIP_STATES:
                continue
            # filter=completed already means this; checked again so a client
            # that ever reports otherwise cannot get a partial torrent removed.
            try:
                if float(t.get("progress", 0)) < 1:
                    continue
            except (TypeError, ValueError):
                continue
            name = _clean_name(t.get("name"))
            if self._delete_one(session, base, torrent_hash):
                removed += 1
                self._log(f"Removed torrent (files kept): {name}")
            else:
                failed.append(name)
        if failed:
            raise _ApiError(f"could not remove {len(failed)} torrent(s): "
                            + ", ".join(failed[:3]))
        return removed

    @staticmethod
    def _delete_one(session, base, torrent_hash):
        # deleteFiles is hardcoded "false". No setting may change it: the
        # whole point is that the downloaded data stays where it is.
        try:
            r = session.post(f"{base}/api/v2/torrents/delete",
                             data={"hashes": torrent_hash, "deleteFiles": "false"},
                             headers={"Referer": base}, timeout=API_TIMEOUT)
        except Exception:
            return False
        return r.status_code == 200

    # ---------------------------------------------------------------- outage

    def _failed(self, reason):
        self._failures += 1
        if self._failures >= OUTAGE_GRACE_CYCLES and not self._outage_reported:
            self._outage_reported = True
            self._log(f"Auto-remove paused: {reason}", level="WARNING")

    def _reset_outage(self):
        self._failures = 0
        self._outage_reported = False
