import glob
import ipaddress
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

    def check_openvpn_process(self):
        try:
            r = subprocess.run(["pgrep", "-x", "openvpn"], capture_output=True, timeout=2)
            return r.returncode == 0
        except Exception:
            return False

    def check_vpn_interface(self):
        try:
            r = subprocess.run(["ip", "link", "show", "tun0"], capture_output=True, timeout=2)
            return r.returncode == 0
        except Exception:
            return False

    def check_default_route(self):
        """Returns True if internet traffic routes through tun0.

        Uses 'ip route get 8.8.8.8' rather than 'ip route show default' because
        OpenVPN's redirect-gateway def1 (used by VPNGate) installs two /1 routes
        instead of replacing the default route, so the default route still points
        at the physical interface even when the tunnel is correctly carrying all
        traffic.  'ip route get' asks the kernel what it would actually use.
        """
        try:
            r = subprocess.run(
                ["ip", "route", "get", "8.8.8.8"],
                capture_output=True, text=True, timeout=2,
            )
            return "tun0" in r.stdout
        except Exception:
            return False

    def check_killswitch_active(self):
        """Returns True if UFW is in kill-switch mode (outgoing deny default)."""
        try:
            r = subprocess.run(
                ["sudo", "ufw", "status", "verbose"],
                capture_output=True, text=True, timeout=3,
            )
            return "deny (outgoing)" in r.stdout
        except Exception:
            return False

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

    def stop_qbittorrent(self):
        """Stop qBittorrent and wait for it to actually exit before returning.

        A fire-and-forget pkill can leave the process alive for a moment into
        whatever runs right after this call — and callers that immediately
        relax the kill switch (stop_vpn, attempt_reconnect) need it gone
        first, not just signaled. Mirrors checkip.sh's stop_qbittorrent().
        """
        if not self.is_qbittorrent_running():
            self.status["qbittorrent"] = False
            return
        self.log("Stopping qBittorrent...", source="QBIT")
        subprocess.run(["sudo", "pkill", "-f", "qbittorrent-nox"], capture_output=True)
        for _ in range(10):
            if not self.is_qbittorrent_running():
                break
            time.sleep(0.5)
        else:
            self.log("qBittorrent still running - sending SIGKILL", source="QBIT", level="WARNING")
            subprocess.run(["sudo", "pkill", "-9", "-f", "qbittorrent-nox"], capture_output=True)
            time.sleep(0.5)
        self.status["qbittorrent"] = False

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

    def start_qbittorrent(self):
        if self.is_qbittorrent_running():
            self.log("qBittorrent already running", source="QBIT")
            return True
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
        if proc.poll() is None:
            self.log(f"qBittorrent started (PID: {proc.pid})", source="QBIT")
            self.status["qbittorrent"] = True
            return True
        self.log("qBittorrent may have failed to start", source="QBIT", level="WARNING")
        return False

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
        if self.is_qbittorrent_running():
            self.stop_qbittorrent()

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
            self.log("OpenVPN did not stop", source="OPENVPN", level="WARNING")
        else:
            self.log("OpenVPN stopped", source="OPENVPN")

        # Last gate before the firewall opens. Issuing the stops in the right
        # order is not the same as them having worked: if a client survived
        # both SIGTERM and SIGKILL, relaxing UFW hands it the ISP link. Leave
        # the kill switch up instead and say so.
        if self.is_qbittorrent_running():
            self.log(
                "Kill switch left ACTIVE — qBittorrent is still running and would "
                "egress unprotected. Kill it, then run ./remove_killswitch.sh.",
                level="CRITICAL",
            )
            return
        self.teardown_killswitch()
        self.restore_ipv6()
        self.restore_dns()

    def stop_vpn_bg(self):
        threading.Thread(target=self.stop_vpn, daemon=True).start()

    def stop_all(self):
        """Graceful ordered shutdown: qBittorrent → monitor → VPN."""
        self.log("Stop All — shutting down in order...")
        if self.is_qbittorrent_running():
            self.stop_qbittorrent()
        # Signal the monitor loop to exit so it doesn't try to reconnect
        self._stop_event.set()
        self.status["running"] = False
        self.stop_vpn()

    # ------------------------------------------------------- VPN reconnect

    def attempt_reconnect(self):
        self.log("Attempting VPN reconnection...")
        # Stop the client before touching the kill switch. _openvpn_start calls
        # setup_killswitch, which resets UFW and briefly restores the default
        # allow-outgoing policy — anything still running would egress
        # unprotected during that window.
        if self.is_qbittorrent_running():
            self.stop_qbittorrent()
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

        while not self._stop_event.is_set():
            vpn_proc = self.check_openvpn_process()
            vpn_iface = self.check_vpn_interface()
            # Only check default route when tun0 is up — route is meaningless without it
            vpn_route = self.check_default_route() if vpn_iface else False
            ipv6_leak = self.check_ipv6_leak()
            self.status["vpn_process"] = vpn_proc
            self.status["vpn_interface"] = vpn_iface
            self.status["vpn_route"] = vpn_route

            if not vpn_proc or not vpn_iface or not vpn_route or ipv6_leak:
                what = (
                    "VPN process down" if not vpn_proc
                    else "VPN interface down" if not vpn_iface
                    else "default route not through tunnel (traffic bypassing tunnel)" if not vpn_route
                    else "global IPv6 address detected (leak risk)"
                )
                self.log(f"{what} — stopping everything", level="CRITICAL")
                self.status["secure"] = False
                if self.is_qbittorrent_running():
                    self.stop_qbittorrent()
                break

            now_ts = time.time()
            if now_ts - last_ip_check >= self.ip_interval:
                # Re-verify the kill switch on the same cadence as the IP check.
                # It used to be checked only at startup, so a UFW reset from
                # another terminal (stop_web.sh, ufw_base.sh) went unnoticed
                # while the monitor carried on reporting healthy.
                if not self.check_killswitch_active():
                    self.log("Kill switch is no longer active — stopping everything",
                             level="CRITICAL")
                    self.status["kill_switch_active"] = False
                    self.status["secure"] = False
                    if self.is_qbittorrent_running():
                        self.stop_qbittorrent()
                    break
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
                        if self.is_qbittorrent_running():
                            self.stop_qbittorrent()
                        break
                    # Retry sooner than normal (stamp from post-check time)
                    last_ip_check = post_check_ts - self.ip_interval + 5
                elif ip.strip() == self.home_ip.strip():
                    self.log(f"IP LEAK DETECTED: external IP {ip} matches home IP — stopping everything", level="CRITICAL")
                    self.status["secure"] = False
                    if self.is_qbittorrent_running():
                        self.stop_qbittorrent()
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
            self.stop_qbittorrent()
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
