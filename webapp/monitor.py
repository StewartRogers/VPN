import glob
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from urllib.parse import urljoin, urlparse

# Strips the leading timestamp OpenVPN writes into its own log lines
# e.g. "2026-03-13 11:56:05 OpenVPN 2.6.3..." → "OpenVPN 2.6.3..."
_OVPN_TS = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\s+")

import requests

MAX_LOGS = 500

# Kill-switch probe tuning.
#
# `ufw status verbose` is a Python program that shells out to iptables; it costs
# ~0.5s on an idle Pi and gets slower under load.  Both the monitor's slow tier
# and every /api/status poll (every 3s from the browser) ask for it, and
# concurrent invocations contend on the xtables lock.  On 2026-08-14 that
# combination stretched the call past the old 3s timeout while qBittorrent was
# running; the timeout was caught and reported as "kill switch gone", which
# fail-stopped a healthy session.  Hence: one shared cached result, a timeout
# with real headroom, and a distinction between "UFW says no" (trip now) and
# "UFW did not answer" (retry first).
KILLSWITCH_CACHE_TTL = 5
KILLSWITCH_TIMEOUT = 10
KILLSWITCH_MAX_UNKNOWN = 3

# qBittorrent shutdown budget.
#
# A clean qBittorrent exit is not instant: it flushes resume data and rewrites
# qBittorrent.conf, which is the only copy of everything set through its WebUI.
# On 2026-08-14 the client caught SIGTERM and was still writing 4.3s later when
# a 5s window expired and SIGKILL landed — the config was never rewritten. The
# grace period exists to let that finish.
#
# Waiting is safe precisely because the kill switch is still up on every
# fail-stop path: UFW is denying all outgoing traffic, so nothing egresses
# while we wait. The one exception is a *confirmed* inactive kill switch, where
# UFW is passing traffic and a live client is leaking right now — that path
# passes urgent=True and goes straight to SIGKILL.
QBT_STOP_GRACE = 30
QBT_KILL_CONFIRM = 5
QBT_POLL_INTERVAL = 0.5

# How long to wait for libtorrent to open its peer socket before deciding the
# tunnel bind failed. Judging too early flags a client that is merely still
# starting and kills a healthy session.
QBT_BIND_CONFIRM = 15

# Timeout for the fast-tier local checks. These are cheap commands, but the Pi
# is also saturating its disk and NIC while torrenting, and the old 2s budget
# was tight enough that a slow answer read as "the tunnel is gone".
FAST_CHECK_TIMEOUT = 5

# Consecutive unanswered fast-tier probes tolerated before stopping. Same
# asymmetry as the kill switch: a definite "tun0 is gone" trips at once, an
# unanswered question gets retried.
FAST_MAX_UNKNOWN = 3

# Absolute path to the VPN project root (one level above this file's webapp/ dir)
_VPN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# qbt_config.py lives in the project root and is shared with the CLI path
if _VPN_DIR not in sys.path:
    sys.path.insert(0, _VPN_DIR)
import qbt_config

# Directory used to store iptables / DNS / IPv6 backups across VPN start/stop.
# Stored under the user's home directory — not in /tmp — to prevent other local
# users from replacing backup files before they are restored with sudo.
_BACKUP_DIR = os.path.join(os.path.expanduser("~"), ".vpn_backups")


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _shell_config_path():
    """Return the vpn_config.conf path checkip.sh/startvpn.sh would source, or None."""
    home_conf = os.path.join(os.path.expanduser("~"), ".vpn_config.conf")
    if os.path.isfile(home_conf):
        return home_conf
    repo_conf = os.path.join(_VPN_DIR, "vpn_config.conf")
    if os.path.isfile(repo_conf):
        return repo_conf
    return None


def read_config_value(key, default=""):
    """Read KEY="value" from the shell-style vpn_config.conf, same file the bash side uses."""
    path = _shell_config_path()
    if not path:
        return default
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line.startswith(f"{key}="):
                    return line[len(key) + 1:].strip().strip('"').strip("'")
    except Exception:
        pass
    return default


def write_config_value(key, value):
    """Update or append KEY="value" in the shell-style vpn_config.conf, so checkip.sh
    picks up the same value the web UI just set."""
    path = _shell_config_path() or os.path.join(_VPN_DIR, "vpn_config.conf")
    line = f'{key}="{value}"\n'
    try:
        lines = open(path).readlines() if os.path.isfile(path) else ["# VPN Configuration File\n"]
        for i, existing in enumerate(lines):
            if existing.strip().startswith(f"{key}="):
                lines[i] = line
                break
        else:
            lines.append(line)
        with open(path, "w") as f:
            f.writelines(lines)
    except Exception:
        pass


def detect_external_ip():
    """Return the external IPv4 address, or None on failure.

    Uses IPv4-only endpoints. api64.ipify.org is intentionally excluded —
    it is dual-stack and returns an IPv6 address when available, which would
    never match the (IPv4) home IP and make leak detection a false-positive.
    IPv6 is disabled via sysctl before the VPN starts, so all checks here
    should naturally return IPv4, but using IPv4-only endpoints is an
    extra layer of correctness.
    """
    services = [
        ("https://api.ipify.org?format=json", lambda r: r.json().get("ip")),
        ("https://ipv4.icanhazip.com", lambda r: r.text.strip()),
        ("https://httpbin.org/ip", lambda r: r.json().get("origin", "").split(",")[0].strip()),
    ]
    for url, extract in services:
        try:
            r = requests.get(url, timeout=3)
            # Without this, an HTTP error body is returned as "the external IP".
            # icanhazip is a plain-text endpoint, so a 502 HTML page would be
            # handed back as a string that never equals home_ip — and the leak
            # check would then pass silently, forever.
            r.raise_for_status()
            ip = (extract(r) or "").strip()
            if not ip:
                continue
            # Same reasoning: reject anything that is not an IPv4 address rather
            # than trusting whatever came back.
            ipaddress.IPv4Address(ip)
            return ip
        except Exception:
            continue
    return None


class VPNMonitor:
    def __init__(self, home_ip, fast_interval=2, ip_interval=10):
        self.home_ip = home_ip
        self.fast_interval = fast_interval
        self.ip_interval = ip_interval
        self.save_path = read_config_value("QBT_SAVE_PATH", "")
        self.max_active_downloads = qbt_config.configured_max_active()

        self.status = {
            "running": False,
            "vpn_starting": False,
            "vpn_retry_pending": False,
            "vpn_process": False,
            "vpn_interface": False,
            "vpn_route": False,
            "kill_switch_active": False,
            "external_ip": None,
            "secure": None,
            "qbittorrent": False,
        }

        self._logs = []
        self._log_condition = threading.Condition()
        self._log_seq = 0

        self._thread = None
        self._stop_event = threading.Event()
        self._retry_cancel = threading.Event()

        # Shared kill-switch probe result: (monotonic_timestamp, state).
        # The lock is held across the subprocess call on purpose — a second
        # caller waits for the in-flight probe and reuses its answer instead of
        # starting a competing `ufw` process.
        self._ks_lock = threading.Lock()
        self._ks_cache = None

    # ------------------------------------------------------------------ logging

    def log(self, message, source="MONITOR", level=None):
        """Append a log entry.

        Format: [timestamp] [SOURCE] message
                [timestamp] [SOURCE] [LEVEL] message  (when level is set)

        source: "MONITOR", "OPENVPN", or "QBIT"
        level:  None (info), "WARNING", "ERROR", or "CRITICAL"
        """
        level_tag = f" [{level}]" if level else ""
        entry = f"[{_now()}] [{source}]{level_tag} {message}"
        with self._log_condition:
            self._log_seq += 1
            self._logs.append((self._log_seq, entry))
            if len(self._logs) > MAX_LOGS:
                self._logs.pop(0)
            self._log_condition.notify_all()

    def _log_openvpn(self, line, level=None):
        """Log an OpenVPN log line, stripping OpenVPN's own leading timestamp."""
        clean = _OVPN_TS.sub("", line.strip())
        if clean:
            self.log(clean, source="OPENVPN", level=level)

    def stream_logs(self, from_seq=0):
        """Generator yielding log lines for SSE. Yields None as a keepalive."""
        while True:
            with self._log_condition:
                new = [(s, m) for s, m in self._logs if s > from_seq]
                if new:
                    for seq, msg in new:
                        from_seq = seq
                    entries = [m for _, m in new]
                else:
                    self._log_condition.wait(timeout=5)
                    entries = []
            for msg in entries:
                yield msg
            if not entries:
                yield None  # keepalive

    def recent_logs(self, n=200):
        with self._log_condition:
            return [m for _, m in self._logs[-n:]]

    # ---------------------------------------------------------- system checks

    def _probe(self, cmd, decide, timeout=FAST_CHECK_TIMEOUT):
        """Run `cmd` and return True/False, or None when it could not be asked.

        None is the whole point, and it is the same rule as probe_killswitch():
        a command that timed out or could not run says nothing about the state
        of the tunnel. Reporting it as False fail-stopped a healthy session on
        2026-08-14 11:32 — OpenVPN's own log shows tun0 up continuously while
        the monitor declared "VPN interface down" and tore everything down.
        These run every FAST_CHECK_INTERVAL on a Pi that is also saturating its
        disk and NIC with torrent traffic, so slow answers are normal.
        """
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except Exception:
            return None
        return decide(r)

    def probe_openvpn_process(self):
        return self._probe(["pgrep", "-x", "openvpn"], lambda r: r.returncode == 0)

    def probe_vpn_interface(self):
        # `ip link show tun0` exits non-zero only when the device is absent,
        # which is a real answer; anything else is an unanswered question.
        return self._probe(["ip", "link", "show", "tun0"], lambda r: r.returncode == 0)

    def probe_default_route(self):
        return self._probe(["ip", "route", "get", "8.8.8.8"],
                           lambda r: "tun0" in r.stdout if r.returncode == 0 else None)

    def check_openvpn_process(self):
        """Bool wrapper — an unanswered probe is treated as not running, so
        everything gated on this (torrent_start_blocked) fails closed."""
        return self.probe_openvpn_process() is True

    def check_vpn_interface(self):
        return self.probe_vpn_interface() is True

    def check_default_route(self):
        """Returns True if internet traffic routes through tun0.

        Uses 'ip route get 8.8.8.8' rather than 'ip route show default' because
        OpenVPN's redirect-gateway def1 (used by VPNGate) installs two /1 routes
        instead of replacing the default route, so the default route still points
        at the physical interface even when the tunnel is correctly carrying all
        traffic.  'ip route get' asks the kernel what it would actually use.
        """
        return self.probe_default_route() is True

    def _invalidate_killswitch_cache(self):
        """Drop the cached probe result after a deliberate UFW transition."""
        with self._ks_lock:
            self._ks_cache = None

    def probe_killswitch(self, force=False):
        """Ask UFW whether the kill switch is up. Returns one of:

            "active"    UFW answered, outgoing policy is deny
            "inactive"  UFW answered, outgoing policy is NOT deny
            "unknown"   UFW did not answer (timeout, non-zero exit, sudo denied)

        The third state is the point of this method.  Collapsing it into
        "inactive" — as this check used to — means a slow box is
        indistinguishable from a torn-down firewall, and the monitor kills a
        healthy session over it.  Callers that gate an action treat "unknown"
        as unsafe (see check_killswitch_active); the monitor loop retries it.

        Results are cached for KILLSWITCH_CACHE_TTL seconds and shared between
        the monitor thread and the /api/status request threads, so UI polling
        cannot multiply the number of `ufw` processes.
        """
        with self._ks_lock:
            cached = self._ks_cache
            if not force and cached is not None:
                ts, state = cached
                if time.monotonic() - ts < KILLSWITCH_CACHE_TTL:
                    return state

            try:
                r = subprocess.run(
                    ["sudo", "ufw", "status", "verbose"],
                    capture_output=True, text=True, timeout=KILLSWITCH_TIMEOUT,
                )
                if r.returncode != 0:
                    # ufw itself failed — most often missing passwordless sudo.
                    # Not evidence either way about the outgoing policy.
                    state = "unknown"
                elif "deny (outgoing)" in r.stdout:
                    state = "active"
                else:
                    state = "inactive"
            except Exception:
                state = "unknown"

            self._ks_cache = (time.monotonic(), state)
            return state

    def check_killswitch_active(self):
        """Returns True only if UFW is confirmed to be in kill-switch mode.

        An inconclusive probe returns False, so anything gated on this fails
        closed (torrent_start_blocked, the monitor's startup check). The
        monitor loop deliberately does not use this — it needs to tell a
        confirmed teardown from an unanswered question.
        """
        return self.probe_killswitch() == "active"

    def check_ipv6_leak(self):
        """Returns True if a global-scope IPv6 address is present — a leak risk,
        since the UFW rules this app applies are IPv4-only. disable_ipv6() is
        called before every OpenVPN start, but something outside this app's
        control (a router re-announcing IPv6, a config revert) could re-enable
        it mid-session, so this is re-checked on the same cadence as the other
        fast checks rather than only once at startup."""
        try:
            r = subprocess.run(
                ["ip", "-6", "addr", "show"],
                capture_output=True, text=True, timeout=2,
            )
            for line in r.stdout.splitlines():
                line = line.strip()
                if line.startswith("inet6") and "scope global" in line:
                    return True
            return False
        except Exception:
            return False

    def get_external_ip(self):
        return detect_external_ip()

    def is_qbittorrent_running(self):
        try:
            r = subprocess.run(["pgrep", "-f", "qbittorrent-nox"], capture_output=True, timeout=2)
            return r.returncode == 0
        except Exception:
            return False

    # --------------------------------------------------------- qbt management

    def stop_qbittorrent(self, urgent=False):
        """Stop qBittorrent and report whether it is *confirmed* gone.

        Returns True only when the process is actually no longer running.
        Callers gate the next teardown step on that: this used to return None
        and set status["qbittorrent"] = False unconditionally — even after a
        SIGKILL that did not work — so every caller proceeded on an assumption
        rather than a fact.

        urgent=True skips the graceful window and sends SIGKILL immediately.
        That is for the single trigger where waiting is unsafe: a confirmed
        inactive kill switch means UFW is passing traffic and a live client is
        egressing on the ISP link right now, so losing unsaved settings is the
        cheaper cost. Every other fail-stop runs with the kill switch still up,
        where nothing can leak while we wait, so the client gets QBT_STOP_GRACE
        seconds to write its config and resume data first.

        Mirrors checkip.sh's stop_qbittorrent().
        """
        if not self.is_qbittorrent_running():
            self.status["qbittorrent"] = False
            return True

        if urgent:
            self.log("Stopping qBittorrent NOW (SIGKILL) — the kill switch is down "
                     "and traffic is unprotected", source="QBIT", level="WARNING")
            subprocess.run(["sudo", "pkill", "-9", "-f", "qbittorrent-nox"],
                           capture_output=True)
            stopped = self._await_qbittorrent_exit(QBT_KILL_CONFIRM)
        else:
            self.log("Stopping qBittorrent...", source="QBIT")
            subprocess.run(["sudo", "pkill", "-f", "qbittorrent-nox"], capture_output=True)
            stopped = self._await_qbittorrent_exit(QBT_STOP_GRACE, announce=True)
            if not stopped:
                self.log(f"qBittorrent has not exited after {QBT_STOP_GRACE}s — "
                         f"sending SIGKILL", source="QBIT", level="WARNING")
                subprocess.run(["sudo", "pkill", "-9", "-f", "qbittorrent-nox"],
                               capture_output=True)
                stopped = self._await_qbittorrent_exit(QBT_KILL_CONFIRM)

        self.status["qbittorrent"] = not stopped
        if stopped:
            self.log("qBittorrent stopped", source="QBIT")
        else:
            self.log("qBittorrent is STILL RUNNING after SIGKILL", source="QBIT",
                     level="CRITICAL")
        return stopped

    def _await_qbittorrent_exit(self, timeout, announce=False):
        """Poll until qBittorrent is gone or `timeout` seconds elapse.

        Returns True only if the process is actually gone. `announce` logs
        progress every 5s so a long graceful shutdown does not look like a hang.
        """
        deadline = time.monotonic() + timeout
        next_notice = time.monotonic() + 5
        while time.monotonic() < deadline:
            if not self.is_qbittorrent_running():
                return True
            if announce and time.monotonic() >= next_notice:
                self.log(f"Still waiting for qBittorrent to finish shutting down "
                         f"(~{int(deadline - time.monotonic())}s left)...", source="QBIT")
                next_notice += 5
            time.sleep(QBT_POLL_INTERVAL)
        return not self.is_qbittorrent_running()

    def apply_qbittorrent_config(self):
        """Apply the settings this project owns to the live qBittorrent config.

        Shared with the CLI path via qbt_config.py, so the two implementations
        cannot drift: the tun0 bind (by name and by live address — an address
        bind is enforced by the kernel, a name bind is only a preference), the
        save path, and the concurrent-download limit.

        This merges rather than overwriting. qBittorrent rewrites its whole
        config on exit, so anything set from its Web UI lives only in that
        file; copying the repo template over it reset those settings on every
        start.
        """
        cmd = [sys.executable, os.path.join(_VPN_DIR, "qbt_config.py"),
               "--save-path", self.save_path or ""]
        if self.max_active_downloads is not None:
            cmd += ["--max-active", str(self.max_active_downloads)]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        except Exception as e:
            self.log(f"Could not apply qBittorrent config — {e}", source="QBIT", level="WARNING")
            return
        for line in result.stdout.splitlines():
            if line.strip():
                self.log(f"Applied qBittorrent config — {line.strip()}", source="QBIT")
        for line in result.stderr.splitlines():
            if line.strip():
                self.log(f"qBittorrent config — {line.strip()}", source="QBIT", level="WARNING")

    def set_save_path(self, path):
        """Update and persist the qBittorrent download location for future starts."""
        self.save_path = path
        write_config_value("QBT_SAVE_PATH", path)

    def torrent_start_blocked(self):
        """Return a reason string if it is not safe to start the torrent client,
        or None if every precondition holds.

        This is the single gate every start path goes through. The monitor must
        be running, the tunnel must exist, must actually be carrying traffic,
        the kill switch must be in place, and the exit IP must not be the home
        IP. The UI also disables its Start button, but a disabled button is a
        hint and not a control — curl, a stale browser tab, or a VPN drop
        between status polls all still reach the endpoint.

        The monitor check is what makes the manual step ordering safe: starting
        the VPN no longer starts anything else, so without this a torrent could
        run on a tunnel with nothing watching it for leaks.
        """
        if not self.status.get("running"):
            return "the monitor is not running — start it first (step 3)"
        if not self.check_openvpn_process():
            return "OpenVPN is not running"
        if not self.check_vpn_interface():
            return "VPN interface (tun0) is down"
        if not self.check_default_route():
            return "Traffic is not routing through tun0"
        if not self.check_killswitch_active():
            return "Kill switch is not active"
        ip = self.get_external_ip()
        if ip is None:
            return "Could not confirm the external IP"
        if ip.strip() == self.home_ip.strip():
            return f"External IP {ip} is the home IP — the tunnel is not carrying traffic"
        return None

    def verify_tunnel_bind(self):
        """Check that qBittorrent's listening socket is really on the tun0 address.

        Returns "bound", "unbound", or "unknown".

        Writing Session\\InterfaceAddress is not proof it took effect. The client
        reads its config once at startup and binds then; if the address it was
        told to use is not present at that moment it falls back to listening on
        every interface and says nothing. A stale address from a previous tunnel
        looks exactly like that — and VPNGate hands out a different tun0 IP on
        every connection, so "stale" is the normal case across sessions, not an
        edge case. Hence: verify, do not assume.
        """
        tun0_ip = qbt_config.detect_tun0_ip()
        if not tun0_ip:
            return "unknown"

        # Only the BitTorrent peer listener matters. The WebUI is a separate
        # socket and is *meant* to be on 0.0.0.0 (WebUI\Address=*) so the
        # dashboard is reachable over the LAN — judging every qbittorrent
        # socket flags that healthy listener and kills the client.
        peer_port = self._qbt_peer_port()
        if not peer_port:
            return "unknown"

        try:
            r = subprocess.run(["ss", "-tlnp"], capture_output=True, text=True, timeout=5)
            if r.returncode != 0:
                return "unknown"
        except Exception:
            return "unknown"

        for ln in r.stdout.splitlines():
            if "qbittorrent" not in ln:
                continue
            parts = ln.split()
            if len(parts) < 4:
                continue
            local = parts[3]
            addr, _, port = local.rpartition(":")
            if port != str(peer_port):
                continue
            # ss prints the scope as "10.211.1.29%tun0" once a socket is bound
            # to a specific interface, so the address has to be split off the
            # scope before comparing — matching the raw string reports a
            # correctly bound client as unbound.
            addr = addr.strip("[]").split("%")[0]
            if addr in ("0.0.0.0", "*", "::"):
                return "unbound"
            return "bound" if addr == tun0_ip else "unbound"

        # The peer socket is not open yet — libtorrent takes a moment after
        # the process starts. Not an answer either way; the caller retries.
        return "unknown"

    def _qbt_peer_port(self):
        """The BitTorrent listen port from qBittorrent.conf, or None."""
        try:
            sections = qbt_config.read_ini(qbt_config.DEFAULT_CONFIG)
            value = qbt_config.get_key(sections, "BitTorrent", r"Session\Port")
            return int(value) if value else None
        except Exception:
            return None

    # ------------------------------------------------- qBittorrent tunnel bind

    def _qbt_webui_port(self):
        try:
            sections = qbt_config.read_ini(qbt_config.DEFAULT_CONFIG)
            value = qbt_config.get_key(sections, "Preferences", r"WebUI\Port")
            return int(value) if value else 8080
        except Exception:
            return 8080

    def _qbt_api(self):
        """Return a requests.Session authenticated to the local WebUI, or None.

        Tries the unauthenticated localhost path first (WebUI\\LocalHostAuth=false)
        and falls back to QBT_WEBUI_USER / QBT_WEBUI_PASS from vpn_config.conf,
        so this works whichever way qBittorrent is configured.
        """
        base = f"http://127.0.0.1:{self._qbt_webui_port()}"
        s = requests.Session()
        try:
            r = s.get(f"{base}/api/v2/app/preferences", timeout=5)
            if r.status_code == 200:
                return s, base
        except Exception:
            return None, base

        user = read_config_value("QBT_WEBUI_USER", "")
        password = read_config_value("QBT_WEBUI_PASS", "")
        if not user:
            return None, base
        try:
            r = s.post(f"{base}/api/v2/auth/login",
                       data={"username": user, "password": password},
                       headers={"Referer": base}, timeout=5)
            if r.status_code == 200 and "Fails" not in r.text:
                return s, base
        except Exception:
            pass
        return None, base

    def apply_tunnel_bind(self):
        """Bind qBittorrent to tun0 through its WebUI API. Returns True on success.

        This cannot be done through qBittorrent.conf. Verified on 4.2.5: values
        written to Session\\Interface, Session\\InterfaceAddress and Session\\Port
        are preserved in the file but never applied — the client reports an empty
        interface, picks a random listen port, and listens on every address.
        Setting the same three values over the API binds it to a single address
        immediately. qbt_config.py still writes the keys in case a later
        qBittorrent honours them, but this is what actually takes effect.
        """
        tun0_ip = qbt_config.detect_tun0_ip()
        if not tun0_ip:
            self.log("Cannot bind qBittorrent — tun0 has no address",
                     source="QBIT", level="CRITICAL")
            return False

        # The WebUI takes a few seconds to accept connections after startup.
        session = None
        deadline = time.monotonic() + QBT_BIND_CONFIRM
        while time.monotonic() < deadline:
            session, base = self._qbt_api()
            if session:
                break
            time.sleep(0.5)
        if not session:
            self.log(
                "Cannot reach the qBittorrent WebUI to apply the tunnel bind. "
                "Either set WebUI\\LocalHostAuth=false or add QBT_WEBUI_USER / "
                "QBT_WEBUI_PASS to ~/.vpn_config.conf.",
                source="QBIT", level="CRITICAL")
            return False

        port = self._qbt_peer_port() or 19806
        prefs = {
            "current_network_interface": "tun0",
            "current_interface_address": tun0_ip,
            "listen_port": port,
            "random_port": False,
            "upnp": False,
        }
        try:
            r = session.post(f"{base}/api/v2/app/setPreferences",
                             data={"json": json.dumps(prefs)},
                             headers={"Referer": base}, timeout=10)
            if r.status_code != 200:
                self.log(f"Tunnel bind request rejected (HTTP {r.status_code})",
                         source="QBIT", level="CRITICAL")
                return False
        except Exception as exc:
            self.log(f"Tunnel bind request failed — {exc}",
                     source="QBIT", level="CRITICAL")
            return False

        # Read it back. A 200 means the request was accepted, not that the bind
        # took — and "accepted but not applied" is exactly how the config-file
        # approach failed silently for so long. Ask the client what it thinks
        # it is bound to.
        try:
            got = session.get(f"{base}/api/v2/app/preferences", timeout=5).json()
        except Exception as exc:
            self.log(f"Could not read back the tunnel bind — {exc}",
                     source="QBIT", level="CRITICAL")
            return False

        actual_addr = got.get("current_interface_address", "")
        actual_iface = got.get("current_network_interface", "")
        if actual_addr != tun0_ip or actual_iface != "tun0":
            self.log(
                f"Tunnel bind did not take — qBittorrent reports interface "
                f"{actual_iface or '(any)'} address {actual_addr or '(all)'}, "
                f"expected tun0 / {tun0_ip}",
                source="QBIT", level="CRITICAL")
            return False

        self.log(f"Tunnel bind applied and confirmed — tun0 ({tun0_ip}) port "
                 f"{got.get('listen_port', port)}", source="QBIT")
        return True

    def start_qbittorrent(self):
        if self.is_qbittorrent_running():
            # Do not report success on an instance we did not configure. Its
            # bind comes from whenever it was last started, which may be a
            # previous tunnel with a different address, and the config cannot
            # be applied to a running client — it reads the file only at
            # startup and overwrites it on exit.
            bind = self.verify_tunnel_bind()
            if bind == "bound":
                self.log("qBittorrent already running and bound to tun0", source="QBIT")
                return True
            self.log(
                f"qBittorrent is already running but its tunnel bind is {bind} — "
                "it was not started by this session, so its config was never "
                "re-applied. Stop it and start it again from here.",
                source="QBIT", level="CRITICAL",
            )
            return False
        blocked = self.torrent_start_blocked()
        if blocked:
            self.log(f"Refusing to start qBittorrent — {blocked}",
                     source="QBIT", level="CRITICAL")
            return False
        self.apply_qbittorrent_config()
        self.log("Starting qBittorrent...", source="QBIT")
        proc = subprocess.Popen(
            ["qbittorrent-nox"],
            stdout=open(os.path.join(_VPN_DIR, "qbit.log"), "w"),
            stderr=subprocess.STDOUT,
        )
        time.sleep(1)
        if proc.poll() is not None:
            self.log("qBittorrent may have failed to start", source="QBIT", level="WARNING")
            return False

        self.log(f"qBittorrent started (PID: {proc.pid})", source="QBIT")
        self.status["qbittorrent"] = True

        # The config file cannot bind this client (see apply_tunnel_bind), so
        # the bind has to be applied over the API once the WebUI is up.
        if not self.apply_tunnel_bind():
            self.log("Could not bind qBittorrent to the tunnel — stopping it. "
                     "The kill switch is still up, but an unbound client would "
                     "keep seeding the moment the tunnel dropped.",
                     source="QBIT", level="CRITICAL")
            self.stop_qbittorrent()
            return False

        # Confirm the bind actually took. libtorrent opens its peer socket a
        # few seconds after the process starts, so keep asking for the whole
        # window rather than judging the first answer — an unopened socket is
        # "unknown", not "unbound", and killing the client over it is exactly
        # the false positive this check is supposed to prevent.
        bind = "unknown"
        deadline = time.monotonic() + QBT_BIND_CONFIRM
        while time.monotonic() < deadline:
            time.sleep(0.5)
            bind = self.verify_tunnel_bind()
            if bind == "bound":
                self.log("Tunnel bind confirmed — peer port on tun0", source="QBIT")
                return True
        if bind == "unbound":
            self.log(
                "qBittorrent is NOT bound to tun0 — it is listening on all "
                "interfaces and would keep seeding if the tunnel dropped. "
                "Stopping it.", source="QBIT", level="CRITICAL",
            )
            self.stop_qbittorrent()
            return False
        self.log("Could not confirm the tunnel bind (ss gave no answer)",
                 source="QBIT", level="WARNING")
        return True

    # ------------------------------------------------------- security measures

    def setup_killswitch(self):
        """Apply UFW kill switch — calls ufw_killswitch.sh to block all non-VPN output."""
        self.log("Applying UFW kill switch...")
        result = subprocess.run(
            ["sudo", "bash", os.path.join(_VPN_DIR, "ufw_killswitch.sh")],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            err = (result.stderr or result.stdout).strip()
            self.log(f"UFW kill switch failed — {err}", level="ERROR")
            raise RuntimeError(f"UFW kill switch failed: {err}")
        self._invalidate_killswitch_cache()
        self.status["kill_switch_active"] = True
        self.log("Kill switch active — all outgoing blocked except VPN tunnel and LAN")

    def teardown_killswitch(self):
        """Restore UFW to base state — calls ufw_base.sh."""
        self.log("Resetting UFW to base state...")
        result = subprocess.run(
            ["sudo", "bash", os.path.join(_VPN_DIR, "ufw_base.sh")],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            self.log("UFW base state restored — outgoing unrestricted")
        else:
            err = (result.stderr or result.stdout).strip()
            self.log(f"UFW reset failed — {err}", level="WARNING")
        self._invalidate_killswitch_cache()
        self.status["kill_switch_active"] = False

    def disable_ipv6(self):
        """Disable IPv6 system-wide to prevent bypass of the VPN tunnel."""
        self.log("Disabling IPv6 to prevent leaks...")
        subprocess.run(
            ["sudo", "sysctl", "-w", "net.ipv6.conf.all.disable_ipv6=1"],
            capture_output=True,
        )
        subprocess.run(
            ["sudo", "sysctl", "-w", "net.ipv6.conf.default.disable_ipv6=1"],
            capture_output=True,
        )

    def restore_ipv6(self):
        """Re-enable IPv6."""
        self.log("Re-enabling IPv6...")
        subprocess.run(
            ["sudo", "sysctl", "-w", "net.ipv6.conf.all.disable_ipv6=0"],
            capture_output=True,
        )
        subprocess.run(
            ["sudo", "sysctl", "-w", "net.ipv6.conf.default.disable_ipv6=0"],
            capture_output=True,
        )

    def setup_dns(self):
        """Replace /etc/resolv.conf with leak-proof DNS servers and lock the file."""
        os.makedirs(_BACKUP_DIR, exist_ok=True)
        backup = os.path.join(_BACKUP_DIR, "resolv.conf.backup")
        if os.path.exists(backup):
            self.log("DNS protection already applied")
            return
        self.log("Configuring DNS leak prevention (Cloudflare 1.1.1.1 / 1.0.0.1)...")
        subprocess.run(["sudo", "chattr", "-i", "/etc/resolv.conf"], capture_output=True)
        subprocess.run(["sudo", "cp", "/etc/resolv.conf", backup], capture_output=True)
        dns_content = "nameserver 1.1.1.1\nnameserver 1.0.0.1\n"
        subprocess.run(
            ["sudo", "tee", "/etc/resolv.conf"],
            input=dns_content.encode(), capture_output=True,
        )
        subprocess.run(["sudo", "chattr", "+i", "/etc/resolv.conf"], capture_output=True)
        self.log("DNS locked to 1.1.1.1 / 1.0.0.1")

    def restore_dns(self):
        """Restore /etc/resolv.conf from backup."""
        backup = os.path.join(_BACKUP_DIR, "resolv.conf.backup")
        if not os.path.exists(backup):
            return
        self.log("Restoring original DNS configuration...")
        subprocess.run(["sudo", "chattr", "-i", "/etc/resolv.conf"], capture_output=True)
        subprocess.run(["sudo", "mv", backup, "/etc/resolv.conf"], capture_output=True)
        self.log("DNS restored")

    # ------------------------------------------------------- VPN start/stop

    def _openvpn_start(self):
        """Launch OpenVPN daemon and return True on success. Logs each step."""

        # 1. Locate config FIRST — needed by kill switch before we stop existing openvpn
        # Newest first, matching startvpn.sh. These used to disagree — this
        # picked an arbitrary glob entry while the shell path and the kill
        # switch each chose differently, so with more than one config present
        # the firewall could whitelist one server while OpenVPN dialled another.
        configs = sorted(
            glob.glob("/etc/openvpn/client/*.ovpn"),
            key=os.path.getmtime, reverse=True,
        )
        if not configs:
            self.log("No .ovpn config file found in /etc/openvpn/client/", level="ERROR")
            return False
        config = configs[0]
        self.log(f"Using config: {config}")

        # 2. Apply / update kill switch BEFORE stopping existing OpenVPN.
        try:
            self.setup_killswitch()
        except RuntimeError as e:
            self.log(str(e), level="ERROR")
            return False

        # 3. Stop existing OpenVPN — kill switch is now in place
        self.log("Stopping any existing OpenVPN process...")
        subprocess.run(["sudo", "pkill", "-f", "openvpn"], capture_output=True)
        time.sleep(2)

        # 4. Apply remaining security measures (idempotent — safe on reconnects)
        self.disable_ipv6()
        self.setup_dns()

        # 5. Start OpenVPN daemon
        self.log("Starting OpenVPN daemon...", source="OPENVPN")
        result = subprocess.run(
            [
                "sudo", "openvpn",
                "--config", config,
                "--log", "/var/log/openvpn.log",
                "--daemon",
                "--script-security", "0",
                "--ping", "10",
                "--ping-exit", "60",
                "--auth-nocache",
                "--mute-replay-warnings",
                "--data-ciphers", "AES-256-GCM:AES-128-GCM:CHACHA20-POLY1305:AES-128-CBC",
                "--data-ciphers-fallback", "AES-128-CBC",
                "--verb", "3",
            ],
            capture_output=True,
        )

        for line in result.stdout.decode().splitlines():
            if line.strip():
                self._log_openvpn(line)
        for line in result.stderr.decode().splitlines():
            if line.strip():
                self._log_openvpn(line, level="WARNING")
        if result.returncode != 0:
            self.log(f"openvpn exited with code {result.returncode}", source="OPENVPN", level="ERROR")
            self.log("Reverting kill switch so a new config can be downloaded", level="WARNING")
            self.teardown_killswitch()
            return False

        # 6. Wait for tun0 to come up, streaming the OpenVPN log
        self.log("Daemon launched, waiting for tun0 interface...", source="OPENVPN")
        last_log_lines = 0
        for i in range(15):
            time.sleep(2)
            try:
                result = subprocess.run(
                    ["sudo", "cat", "/var/log/openvpn.log"],
                    capture_output=True, text=True, timeout=2,
                )
                lines = [l for l in result.stdout.splitlines() if l.strip()]
                for line in lines[last_log_lines:]:
                    self._log_openvpn(line)
                last_log_lines = len(lines)
            except Exception:
                pass
            # tun0 appearing is not the same as the tunnel working. Require the
            # default route to have moved onto it, and require the exit IP to
            # differ from the home IP, before reporting success — otherwise
            # "VPN connected" can be announced over a tunnel carrying nothing.
            if not self.check_vpn_interface():
                continue
            if not self.check_default_route():
                continue   # routes can land a moment after the interface

            self.log("tun0 is up and carrying the default route", source="OPENVPN")
            ip = self.get_external_ip()
            if ip is None:
                self.log("tun0 is up but the external IP could not be read",
                         source="OPENVPN", level="ERROR")
                break
            if ip.strip() == self.home_ip.strip():
                self.log(f"tun0 is up but the external IP is still the home IP ({ip})",
                         source="OPENVPN", level="CRITICAL")
                break
            self.status["external_ip"] = ip
            self.status["secure"] = True
            self.log(f"VPN connected — external IP: {ip}")
            return True
        else:
            self.log("tun0 never came up — check OpenVPN log lines above",
                     source="OPENVPN", level="ERROR")

        self.log("Reverting kill switch so a new config can be downloaded", level="WARNING")
        self.teardown_killswitch()
        return False

    def _install_ovpn(self, tmp_path, filename):
        """Move tmp_path into /etc/openvpn/client/, removing any existing .ovpn files first."""
        dest = f"/etc/openvpn/client/{filename}"

        for old in glob.glob("/etc/openvpn/client/*.ovpn"):
            rm = subprocess.run(["sudo", "rm", "-f", old], capture_output=True)
            if rm.returncode == 0:
                self.log(f"Removed old config: {old}")
            else:
                self.log(f"Could not remove {old}", level="WARNING")

        result = subprocess.run(["sudo", "mv", tmp_path, dest], capture_output=True)
        if result.returncode != 0:
            self.log(f"Could not install config — {result.stderr.decode().strip()}", level="ERROR")
            return False

        subprocess.run(["sudo", "chmod", "600", dest], capture_output=True)
        subprocess.run(["sudo", "chown", "root:root", dest], capture_output=True)
        self.log(f"Installed: {dest} — ready to Start VPN")
        return True

    # Serializes the socket.getaddrinfo monkeypatch in _fetch_pinned so concurrent
    # downloads can't clobber each other's pinned hostname/IP.
    _dns_pin_lock = threading.Lock()

    @staticmethod
    def _check_ovpn_url(url):
        """Return an error string if url is not a safe HTTPS URL to a public host, else None."""
        try:
            parsed = urlparse(url)
        except Exception:
            return "Invalid URL"
        if parsed.scheme != "https":
            return "Only HTTPS URLs are allowed"
        host = parsed.hostname
        if not host:
            return "No host in URL"
        try:
            ip = ipaddress.ip_address(socket.gethostbyname(host))
        except Exception:
            return f"Could not resolve host: {host}"
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved:
            return "Private/internal addresses not allowed"
        return None

    @staticmethod
    def _resolve_public_ip(host):
        """Resolve host and return its IP as a string, raising ValueError if it's
        not a public address (blocks loopback/private/link-local/etc. targets)."""
        try:
            ip = ipaddress.ip_address(socket.gethostbyname(host))
        except Exception:
            raise ValueError(f"Could not resolve host: {host}")
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved:
            raise ValueError("Private/internal addresses not allowed")
        return str(ip)

    @classmethod
    def _fetch_pinned(cls, url, timeout=15, max_redirects=5, max_bytes=1024 * 1024):
        """GET url, pinning the TCP connection for each hop to the IP that was just
        validated as public for that hop's hostname. Plain "resolve once, then let
        requests resolve again to connect" has a DNS-rebinding gap: the second
        resolution can return a different (internal) address. Redirects are
        followed manually and re-validated so a malicious redirect can't reach
        an internal host either."""
        for _ in range(max_redirects + 1):
            err = cls._check_ovpn_url(url)
            if err:
                raise ValueError(err)
            host = urlparse(url).hostname
            ip = cls._resolve_public_ip(host)

            real_getaddrinfo = socket.getaddrinfo

            def _pinned_getaddrinfo(node, *args, **kwargs):
                if node == host:
                    node = ip
                return real_getaddrinfo(node, *args, **kwargs)

            with cls._dns_pin_lock:
                socket.getaddrinfo = _pinned_getaddrinfo
                try:
                    r = requests.get(
                        url, timeout=timeout, allow_redirects=False, stream=True,
                        # requests honours HTTPS_PROXY by default, and a proxy
                        # resolves the hostname itself — which would bypass the
                        # pin established above.
                        proxies={"http": None, "https": None},
                    )
                finally:
                    socket.getaddrinfo = real_getaddrinfo

            if r.is_redirect or r.is_permanent_redirect:
                location = r.headers.get("Location")
                r.close()
                if not location:
                    raise ValueError("Redirect with no Location header")
                url = urljoin(url, location)
                continue

            try:
                r.raise_for_status()
                # Cap the body so a hostile or wrong URL cannot exhaust memory.
                body = bytearray()
                for chunk in r.iter_content(8192):
                    body.extend(chunk)
                    if len(body) > max_bytes:
                        raise ValueError(f"config exceeds {max_bytes} bytes")
            finally:
                r.close()
            return bytes(body)

        raise ValueError("Too many redirects")

    def download_ovpn(self, url):
        """Download a .ovpn file from url and install it. Runs in background."""
        def _run():
            self.log(f"Downloading OVPN config from: {url}")
            try:
                data = self._fetch_pinned(url)
            except Exception as e:
                self.log(f"Download rejected — {e}", level="ERROR")
                return

            # Make sure this is actually an OpenVPN config and not an error page
            # saved under a .ovpn name — otherwise the failure only shows up
            # later as a tunnel that will not come up.
            if not re.search(rb"^[ \t]*remote[ \t]+\S+", data, re.MULTILINE):
                self.log("Download rejected — no 'remote' line, not an OpenVPN config",
                         level="ERROR")
                return

            filename = os.path.basename(url.split("/")[-1].split("?")[0])
            if not filename.endswith(".ovpn"):
                filename += ".ovpn"

            fd, tmp = tempfile.mkstemp(suffix=".ovpn", prefix="vpnconf_")
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                self.log(f"Downloaded {len(data)} bytes")
            except Exception as e:
                self.log(f"Could not write temp file — {e}", level="ERROR")
                return

            self._install_ovpn(tmp, filename)

        threading.Thread(target=_run, daemon=True).start()

    def upload_ovpn(self, data, filename):
        """Install an uploaded .ovpn file (data is bytes). Runs in background."""
        def _run():
            self.log(f"Installing uploaded OVPN config: {filename} ({len(data)} bytes)")
            fd, tmp = tempfile.mkstemp(suffix=".ovpn", prefix="vpnconf_")
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
            except Exception as e:
                self.log(f"Could not write temp file — {e}", level="ERROR")
                return
            self._install_ovpn(tmp, filename)

        threading.Thread(target=_run, daemon=True).start()

    def start_vpn(self):
        """Start the VPN and stop there.

        Step 2 of the UI brings up the tunnel; the monitor (step 3) and the
        torrent client (step 4) are separate, deliberate clicks. Chaining them
        off this one call took the ordering decision away from the operator,
        which matters when the tunnel comes up on the wrong exit or a config
        needs swapping before anything else touches the link.

        Nothing is lost by not chaining: torrent_start_blocked() is the gate on
        every qBittorrent start path, and it refuses unless the monitor is
        running over a verified tunnel.

        Runs in a background thread so logs stream to the UI immediately.
        """
        self._retry_cancel.clear()

        def _thread():
            self.status["vpn_starting"] = True
            try:
                max_attempts = int(read_config_value("MAX_STARTUP_ATTEMPTS", "3") or 3)
                for attempt in range(1, max_attempts + 1):
                    if attempt > 1:
                        self.log(f"Retrying VPN connection (attempt {attempt} of {max_attempts})... "
                                 "upload or download a different .ovpn now if you want to try one.")
                    if self._openvpn_start():
                        break
                    if attempt < max_attempts:
                        self.log(f"Connection attempt {attempt} failed", level="WARNING")
                        self.status["vpn_retry_pending"] = True
                        cancelled = self._retry_cancel.wait(5)
                        self.status["vpn_retry_pending"] = False
                        if cancelled:
                            self.log("Retry cancelled by user", level="WARNING")
                            return
                else:
                    self.log(f"VPN startup failed after {max_attempts} attempt(s) - giving up",
                             level="CRITICAL")
                    return
            finally:
                self.status["vpn_starting"] = False
                self.status["vpn_retry_pending"] = False
            self.log("VPN is up. Start the monitor (step 3), then qBittorrent (step 4).")
        threading.Thread(target=_thread, daemon=True).start()

    def cancel_retry(self):
        """Stop the startup retry loop before it burns through MAX_STARTUP_ATTEMPTS.

        Only has an effect while start_vpn() is waiting between attempts
        (status["vpn_retry_pending"] is True) - it does not interrupt an
        attempt already in progress.
        """
        self._retry_cancel.set()

    def stop_vpn(self):
        # qBittorrent must be *confirmed* stopped before the kill switch is
        # touched — teardown_killswitch() sets outgoing to unrestricted, and
        # anything still running at that point would egress on the ISP link.
        if not self.stop_qbittorrent():
            self.log(
                "Teardown HALTED at step 1 — qBittorrent survived SIGKILL. OpenVPN "
                "is left running and the kill switch stays ACTIVE; opening UFW now "
                "would hand the client the ISP link. Kill it by hand, then run "
                "./remove_killswitch.sh.",
                level="CRITICAL",
            )
            return False

        self.log("Stopping OpenVPN...", source="OPENVPN")
        subprocess.run(["sudo", "pkill", "-f", "openvpn"], capture_output=True)
        for _ in range(10):
            if not self.check_openvpn_process():
                break
            time.sleep(0.5)
        else:
            self.log("OpenVPN still running - sending SIGKILL", source="OPENVPN", level="WARNING")
            subprocess.run(["sudo", "pkill", "-9", "-f", "openvpn"], capture_output=True)
            time.sleep(0.5)
        if self.check_openvpn_process():
            # Step 2 did not complete, so step 3 does not start.
            self.log(
                "Teardown HALTED at step 2 — OpenVPN survived SIGKILL. The kill "
                "switch stays ACTIVE. Kill it by hand, then run "
                "./remove_killswitch.sh to restore network access.",
                source="OPENVPN", level="CRITICAL",
            )
            return False
        self.log("OpenVPN stopped", source="OPENVPN")

        # Last gate before the firewall opens. Issuing the stops in the right
        # order is not the same as them having worked: if a client survived
        # both SIGTERM and SIGKILL, relaxing UFW hands it the ISP link. Leave
        # the kill switch up instead and say so. Re-checked here rather than
        # trusted from step 1 — qBittorrent could have been restarted by hand
        # while OpenVPN was being stopped.
        if self.is_qbittorrent_running():
            self.log(
                "Kill switch left ACTIVE — qBittorrent is running again and would "
                "egress unprotected. Kill it, then run ./remove_killswitch.sh.",
                level="CRITICAL",
            )
            return False

        # Step 3: only now is it safe to open the firewall back up.
        self.teardown_killswitch()
        self.restore_ipv6()
        self.restore_dns()
        return True

    def stop_vpn_bg(self):
        threading.Thread(target=self.stop_vpn, daemon=True).start()

    def stop_all(self):
        """Ordered shutdown: qBittorrent → monitor → VPN → restore.

        Each step must be confirmed complete before the next one starts; any
        step that cannot be confirmed halts the sequence with the kill switch
        left up, rather than carrying on and opening the firewall on faith.
        """
        self.log("Stop All — shutting down in order...")

        # Step 1: the torrent client.
        if not self.stop_qbittorrent():
            self.log(
                "Stop All HALTED at step 1 — qBittorrent survived SIGKILL. The "
                "monitor, OpenVPN and the kill switch are all left as they are.",
                level="CRITICAL",
            )
            return False

        # Step 2: the monitor. Wait for the thread to actually exit — signalling
        # it is not the same as it having stopped, and stop_vpn() must not race
        # a loop iteration that is still probing and logging.
        self._stop_event.set()
        self.status["running"] = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=self.fast_interval + QBT_KILL_CONFIRM)
            if self._thread.is_alive():
                self.log(
                    "Stop All HALTED at step 2 — the monitor thread did not exit. "
                    "OpenVPN and the kill switch are left as they are.",
                    level="CRITICAL",
                )
                return False
        self.log("Monitor stopped")

        # Steps 3 and 4: OpenVPN, then restore. stop_vpn() gates them the same way.
        return self.stop_vpn()

    # ------------------------------------------------------- VPN reconnect

    def attempt_reconnect(self):
        self.log("Attempting VPN reconnection...")
        # Stop the client before touching the kill switch. _openvpn_start calls
        # setup_killswitch, which resets UFW and briefly restores the default
        # allow-outgoing policy — anything still running would egress
        # unprotected during that window.
        if not self.stop_qbittorrent():
            self.log(
                "Reconnect ABORTED — qBittorrent survived SIGKILL. _openvpn_start() "
                "reapplies the kill switch, which briefly restores the default "
                "allow-outgoing policy, and the client would egress unprotected "
                "in that window.",
                level="CRITICAL",
            )
            return False
        ok = self._openvpn_start()
        if ok:
            ip = self.get_external_ip()
            if ip and ip.strip() != self.home_ip.strip():
                self.log("Reconnection successful!")
                self.start_qbittorrent()
                return True
        self.log("Reconnection failed", level="WARNING")
        return False

    # ---------------------------------------------------------- monitor loop

    def _run(self):
        self.log(f"Starting VPN monitoring (home IP: {self.home_ip})")
        self.log(f"Fast checks every {self.fast_interval}s, IP checks every {self.ip_interval}s")

        last_ip_check = 0
        consecutive_ip_errors = 0
        consecutive_ks_unknown = 0
        consecutive_fast_unknown = 0
        # Set only by the confirmed-inactive-kill-switch branch: that is the one
        # exit where the client is egressing unprotected and must die at once.
        # Every other break leaves the kill switch up, so a clean exit is safe.
        stop_urgent = False

        while not self._stop_event.is_set():
            # Tri-state: True / False / None ("could not ask").
            vpn_proc = self.probe_openvpn_process()
            vpn_iface = self.probe_vpn_interface()
            # Only check the route when tun0 is up — the route is meaningless
            # without it, and unknowable if we could not even see the interface.
            vpn_route = self.probe_default_route() if vpn_iface is True else (
                None if vpn_iface is None else False)
            ipv6_leak = self.check_ipv6_leak()
            self.status["vpn_process"] = vpn_proc is True
            self.status["vpn_interface"] = vpn_iface is True
            self.status["vpn_route"] = vpn_route is True

            # A definite False is a real failure and trips immediately.
            failure = (
                "VPN process down" if vpn_proc is False
                else "VPN interface down" if vpn_iface is False
                else "default route not through tunnel (traffic bypassing tunnel)"
                if vpn_route is False
                else "global IPv6 address detected (leak risk)" if ipv6_leak
                else None
            )
            if failure:
                self.log(f"{failure} — stopping everything", level="CRITICAL")
                self.status["secure"] = False
                break

            # None means the command did not answer — that is not evidence the
            # tunnel is gone. Retry, exactly as the kill-switch probe does.
            if None in (vpn_proc, vpn_iface, vpn_route):
                consecutive_fast_unknown += 1
                unanswered = ("OpenVPN process" if vpn_proc is None
                              else "tun0 interface" if vpn_iface is None
                              else "default route")
                self.log(
                    f"Fast check inconclusive — {unanswered} check did not respond "
                    f"({consecutive_fast_unknown} consecutive)", level="WARNING")
                if consecutive_fast_unknown >= FAST_MAX_UNKNOWN:
                    self.log(
                        f"{FAST_MAX_UNKNOWN} consecutive fast checks could not be "
                        f"confirmed — stopping everything", level="CRITICAL")
                    self.status["secure"] = False
                    break
                self._stop_event.wait(self.fast_interval)
                continue
            consecutive_fast_unknown = 0

            now_ts = time.time()
            if now_ts - last_ip_check >= self.ip_interval:
                # Re-verify the kill switch on the same cadence as the IP check.
                # It used to be checked only at startup, so a UFW reset from
                # another terminal (stop_web.sh, ufw_base.sh) went unnoticed
                # while the monitor carried on reporting healthy.
                ks = self.probe_killswitch()
                if ks == "inactive":
                    # UFW answered and the deny-outgoing policy is gone. This is
                    # a real teardown — trip immediately, no retries.
                    self.log("Kill switch is no longer active — stopping everything",
                             level="CRITICAL")
                    self.status["kill_switch_active"] = False
                    self.status["secure"] = False
                    # UFW is open and the client is egressing unprotected:
                    # kill it now, do not wait for a clean exit.
                    stop_urgent = True
                    break
                if ks == "unknown":
                    # UFW did not answer. Says nothing about the firewall, so
                    # retry rather than killing a possibly-healthy session —
                    # same tolerance the external IP check below gets.
                    consecutive_ks_unknown += 1
                    self.log(
                        f"Kill switch check inconclusive — UFW did not respond "
                        f"({consecutive_ks_unknown} consecutive)",
                        level="WARNING",
                    )
                    if consecutive_ks_unknown >= KILLSWITCH_MAX_UNKNOWN:
                        self.log(
                            f"{KILLSWITCH_MAX_UNKNOWN} consecutive kill switch checks "
                            f"could not be confirmed — stopping everything",
                            level="CRITICAL",
                        )
                        self.status["secure"] = False
                        break
                else:
                    consecutive_ks_unknown = 0
                    self.status["kill_switch_active"] = True

                ip = self.get_external_ip()
                post_check_ts = time.time()
                self.status["external_ip"] = ip

                if ip is None:
                    consecutive_ip_errors += 1
                    self.log(f"IP check error ({consecutive_ip_errors} consecutive)", level="WARNING")
                    if consecutive_ip_errors >= 3:
                        self.log("3 consecutive IP check failures — stopping everything", level="CRITICAL")
                        self.status["secure"] = False
                        break
                    # Retry sooner than normal (stamp from post-check time)
                    last_ip_check = post_check_ts - self.ip_interval + 5
                elif ip.strip() == self.home_ip.strip():
                    self.log(f"IP LEAK DETECTED: external IP {ip} matches home IP — stopping everything", level="CRITICAL")
                    self.status["secure"] = False
                    break
                else:
                    consecutive_ip_errors = 0
                    self.status["secure"] = True
                    self.log(f"VPN secure — external IP: {ip}")
                    last_ip_check = post_check_ts

            self.status["qbittorrent"] = self.is_qbittorrent_running()
            self._stop_event.wait(self.fast_interval)

        self.status["running"] = False
        if not self._stop_event.is_set():
            # Internal exit (VPN failure / leak) — stop qBittorrent and OpenVPN,
            # but leave the kill switch active so no traffic leaks out.
            # User must click Stop VPN to restore network access.
            self.log("Monitoring stopped due to VPN failure — kill switch remains active", level="WARNING")

            # Step 1: qBittorrent, and nothing else until it is confirmed gone.
            # Stopping OpenVPN first would drop the tunnel out from under a live
            # client; the ordering is only worth anything if each step is
            # verified rather than merely issued.
            if not self.stop_qbittorrent(urgent=stop_urgent):
                self.log(
                    "Teardown HALTED at step 1 — qBittorrent survived SIGKILL. "
                    "OpenVPN and the kill switch are left exactly as they are. "
                    "Kill it by hand, then run ./remove_killswitch.sh.",
                    level="CRITICAL",
                )
                return

            # Step 2: OpenVPN, now that no client can be holding the tunnel.
            subprocess.run(["sudo", "pkill", "-f", "openvpn"], capture_output=True)
            self.log("OpenVPN stopped — use Stop VPN to restore network access", source="OPENVPN")
        else:
            # External exit (Stop Monitor or Stop All) — caller handles teardown
            self.log("Monitoring stopped")

    # --------------------------------------------------------- public control

    def start(self):
        if self._thread and self._thread.is_alive():
            return False
        self._stop_event.clear()
        self.status["running"] = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self._stop_event.set()
        self.status["running"] = False
