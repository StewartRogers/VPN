import glob
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from datetime import datetime

# Strips the leading timestamp OpenVPN writes into its own log lines
# e.g. "2026-03-13 11:56:05 OpenVPN 2.6.3..." → "OpenVPN 2.6.3..."
_OVPN_TS = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\s+")

import requests

MAX_LOGS = 500

# Absolute path to the VPN project root (one level above this file's webapp/ dir)
_VPN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Directory used to store iptables / DNS / IPv6 backups across VPN start/stop.
# Stored under the user's home directory — not in /tmp — to prevent other local
# users from replacing backup files before they are restored with sudo.
_BACKUP_DIR = os.path.join(os.path.expanduser("~"), ".vpn_backups")


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


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
            ip = extract(r)
            if ip:
                return ip
        except Exception:
            continue
    return None


class VPNMonitor:
    def __init__(self, home_ip, fast_interval=2, ip_interval=10, max_reconnects=3):
        self.home_ip = home_ip
        self.fast_interval = fast_interval
        self.ip_interval = ip_interval
        self.max_reconnects = max_reconnects

        self.status = {
            "running": False,
            "vpn_starting": False,
            "vpn_process": False,
            "vpn_interface": False,
            "vpn_route": False,
            "kill_switch_active": False,
            "external_ip": None,
            "secure": None,
            "qbittorrent": False,
            "reconnect_count": 0,
        }

        self._logs = []
        self._log_condition = threading.Condition()
        self._log_seq = 0

        self._thread = None
        self._stop_event = threading.Event()

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
            r = subprocess.run(["pgrep", "-f", "openvpn"], capture_output=True, timeout=2)
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
        """Returns True if the iptables OUTPUT chain has a DROP default policy."""
        try:
            r = subprocess.run(
                ["sudo", "iptables", "-L", "OUTPUT"],
                capture_output=True, text=True, timeout=3,
            )
            return "policy DROP" in r.stdout
        except Exception:
            return False

    def get_external_ip(self):
        return detect_external_ip()

    def _get_tun0_ip(self):
        """Return the current IPv4 address assigned to tun0, or None."""
        try:
            r = subprocess.run(
                ["ip", "-4", "addr", "show", "tun0"],
                capture_output=True, text=True, timeout=2,
            )
            for line in r.stdout.splitlines():
                line = line.strip()
                if line.startswith("inet "):
                    return line.split()[1].split("/")[0]
        except Exception:
            pass
        return None

    def is_qbittorrent_running(self):
        try:
            r = subprocess.run(["pgrep", "-f", "qbittorrent-nox"], capture_output=True, timeout=2)
            return r.returncode == 0
        except Exception:
            return False

    # --------------------------------------------------------- qbt management

    def stop_qbittorrent(self):
        self.log("Stopping qBittorrent...", source="QBIT")
        subprocess.run(["sudo", "pkill", "-f", "qbittorrent-nox"], capture_output=True)
        self.status["qbittorrent"] = False

    def apply_qbittorrent_config(self):
        """Install the repo's qBittorrent.conf before starting, injecting the
        current tun0 IP as Session\\InterfaceAddress for a hard socket-level bind.
        Binding by name alone is a soft preference; binding by IP is enforced at
        the OS level — the kernel will reject sends if the source IP is invalid.
        """
        src = os.path.join(_VPN_DIR, "qBittorrent.conf")
        dst = os.path.expanduser("~/.config/qBittorrent/qBittorrent.conf")
        if not os.path.exists(src):
            self.log("No qBittorrent.conf in repo — skipping config install", source="QBIT", level="WARNING")
            return
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        try:
            shutil.copy2(src, dst)
        except Exception as e:
            self.log(f"Could not install qBittorrent config — {e}", source="QBIT", level="WARNING")
            return

        # Inject the live tun0 IP so qBittorrent uses a hard IP-level socket bind
        tun0_ip = self._get_tun0_ip()
        if tun0_ip:
            try:
                with open(dst, "r") as f:
                    content = f.read()
                # Replace existing InterfaceAddress or insert after InterfaceName line
                if r"Session\InterfaceAddress" in content:
                    content = re.sub(
                        r"Session\\InterfaceAddress=.*",
                        f"Session\\\\InterfaceAddress={tun0_ip}",
                        content,
                    )
                else:
                    content = content.replace(
                        r"Session\InterfaceName=tun0",
                        f"Session\\InterfaceName=tun0\nSession\\InterfaceAddress={tun0_ip}",
                    )
                with open(dst, "w") as f:
                    f.write(content)
                self.log(f"Applied qBittorrent config — bound to tun0 ({tun0_ip})", source="QBIT")
            except Exception as e:
                self.log(f"Could not inject InterfaceAddress — {e}", source="QBIT", level="WARNING")
        else:
            self.log("Applied qBittorrent config — tun0 IP unavailable, bound by name only", source="QBIT", level="WARNING")

    def start_qbittorrent(self):
        if self.is_qbittorrent_running():
            self.log("qBittorrent already running", source="QBIT")
            return True
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

    def _parse_ovpn_servers(self, config_path):
        """Parse all VPN server entries from .ovpn. Returns list of (ip, port, proto)."""
        results = []
        file_proto = "udp"
        content = None

        # Try direct read first (works if file is world-readable, e.g. installed by web app)
        try:
            with open(config_path, "r") as f:
                content = f.read()
        except PermissionError:
            # File is root-only (installed by startvpn.sh with chmod 600) — try sudo cat
            try:
                r = subprocess.run(
                    ["sudo", "cat", config_path],
                    capture_output=True, text=True, timeout=3,
                )
                if r.returncode == 0:
                    content = r.stdout
            except Exception:
                pass
        except Exception:
            pass

        if content is None:
            self.log(f"Cannot read {config_path} — kill switch will use port fallback", level="WARNING")
            return []

        for line in content.splitlines():
            line = line.strip()
            if line.startswith("proto "):
                parts = line.split()
                if len(parts) >= 2:
                    file_proto = parts[1]
            elif line.startswith("remote "):
                parts = line.split()
                if len(parts) >= 2:
                    host = parts[1]
                    port = parts[2] if len(parts) >= 3 else "1194"
                    line_proto = parts[3] if len(parts) >= 4 else file_proto
                    try:
                        ip = socket.gethostbyname(host)
                        results.append((ip, port, line_proto))
                        self.log(f"Resolved VPN server: {host} → {ip}:{port}/{line_proto}")
                    except Exception:
                        self.log(f"Could not resolve VPN server hostname: {host}", level="WARNING")
        return results

    def setup_killswitch(self, config_path):
        """Apply iptables kill switch — drops all non-VPN output traffic.

        Called BEFORE stopping existing OpenVPN so there is no traffic gap:
        the new server IP is allowed through before the old tunnel comes down.
        Safe to call on reconnects — it flushes and re-applies with the new
        server IP each time without losing the original-state backup.
        """
        os.makedirs(_BACKUP_DIR, exist_ok=True)
        self.log("Applying iptables kill switch...")

        # Resolve VPN server IPs BEFORE touching iptables
        servers = self._parse_ovpn_servers(config_path)

        # Save original iptables/ip6tables rules on first application only.
        # On reconnects the backup already exists — we don't overwrite it so
        # teardown always restores to the true pre-VPN state.
        backup = os.path.join(_BACKUP_DIR, "iptables.backup")
        ip6backup = os.path.join(_BACKUP_DIR, "ip6tables.backup")
        if not os.path.exists(backup):
            try:
                result = subprocess.run(
                    ["sudo", "iptables-save"],
                    capture_output=True, text=True, check=True,
                )
                with open(backup, "w") as f:
                    f.write(result.stdout)
                self.log(f"Saved original iptables rules to {backup}")
            except Exception as e:
                self.log(f"Could not save iptables backup — {e}", level="ERROR")
                self.log("Aborting kill switch setup to avoid locking you out", level="ERROR")
                raise RuntimeError(f"iptables backup failed: {e}") from e
        if not os.path.exists(ip6backup):
            try:
                result = subprocess.run(
                    ["sudo", "ip6tables-save"],
                    capture_output=True, text=True, check=True,
                )
                with open(ip6backup, "w") as f:
                    f.write(result.stdout)
                self.log(f"Saved original ip6tables rules to {ip6backup}")
            except Exception as e:
                self.log(f"Could not save ip6tables backup — {e} (continuing without IPv6 kill switch)", level="WARNING")

        # Temporarily set ACCEPT so we don't hard-drop traffic while rebuilding rules
        subprocess.run(["sudo", "iptables", "-P", "OUTPUT", "ACCEPT"], capture_output=True)
        subprocess.run(["sudo", "iptables", "-F", "OUTPUT"], capture_output=True)

        # Loopback — always allow
        subprocess.run(
            ["sudo", "iptables", "-A", "OUTPUT", "-o", "lo", "-j", "ACCEPT"],
            capture_output=True,
        )

        # Established / related connections
        subprocess.run(
            ["sudo", "iptables", "-A", "OUTPUT",
             "-m", "state", "--state", "ESTABLISHED,RELATED", "-j", "ACCEPT"],
            capture_output=True,
        )

        # Local networks — keep LAN and the web UI accessible
        for net in ["192.168.0.0/16", "10.0.0.0/8", "172.16.0.0/12"]:
            subprocess.run(
                ["sudo", "iptables", "-A", "OUTPUT", "-d", net, "-j", "ACCEPT"],
                capture_output=True,
            )

        # DNS — allow queries to the DNS servers we configure in setup_dns()
        # so that OpenVPN can resolve the VPN server hostname after the kill
        # switch is active.  Without this, DNS to 1.1.1.1 is blocked and
        # OpenVPN cannot connect.
        for dns_ip in ["1.1.1.1", "1.0.0.1"]:
            for p in ["udp", "tcp"]:
                subprocess.run(
                    ["sudo", "iptables", "-A", "OUTPUT",
                     "-d", dns_ip, "-p", p, "--dport", "53", "-j", "ACCEPT"],
                    capture_output=True,
                )

        # VPN server(s) — allow OpenVPN to reach the endpoint through the kill switch
        if servers:
            for ip, port, proto in servers:
                p = "udp" if "udp" in proto else "tcp"
                subprocess.run(
                    ["sudo", "iptables", "-A", "OUTPUT",
                     "-d", ip, "-p", p, "--dport", port, "-j", "ACCEPT"],
                    capture_output=True,
                )
        else:
            # Fallback when server hostname could not be resolved
            self.log("Kill switch fallback: allowing ports 1194 and 443 (UDP+TCP) to any host", level="WARNING")
            for port in ["1194", "443"]:
                for p in ["udp", "tcp"]:
                    subprocess.run(
                        ["sudo", "iptables", "-A", "OUTPUT",
                         "-p", p, "--dport", port, "-j", "ACCEPT"],
                        capture_output=True,
                    )

        # All traffic through the VPN tunnel
        subprocess.run(
            ["sudo", "iptables", "-A", "OUTPUT", "-o", "tun+", "-j", "ACCEPT"],
            capture_output=True,
        )

        # DROP everything else — set last so rules above take effect cleanly
        subprocess.run(["sudo", "iptables", "-P", "OUTPUT", "DROP"], capture_output=True)

        # IPv6 kill switch — defense-in-depth even though IPv6 is disabled via sysctl.
        # If sysctl is reset (reboot, manual change) this prevents IPv6 from bypassing
        # the tunnel entirely. VPNGate doesn't use IPv6 so we allow only loopback + tun+.
        subprocess.run(["sudo", "ip6tables", "-P", "OUTPUT", "ACCEPT"], capture_output=True)
        subprocess.run(["sudo", "ip6tables", "-F", "OUTPUT"], capture_output=True)
        subprocess.run(
            ["sudo", "ip6tables", "-A", "OUTPUT", "-o", "lo", "-j", "ACCEPT"],
            capture_output=True,
        )
        subprocess.run(
            ["sudo", "ip6tables", "-A", "OUTPUT",
             "-m", "state", "--state", "ESTABLISHED,RELATED", "-j", "ACCEPT"],
            capture_output=True,
        )
        # Allow link-local (fe80::/10) for neighbour discovery / LAN
        subprocess.run(
            ["sudo", "ip6tables", "-A", "OUTPUT", "-d", "fe80::/10", "-j", "ACCEPT"],
            capture_output=True,
        )
        # Allow any traffic that exits via the tunnel
        subprocess.run(
            ["sudo", "ip6tables", "-A", "OUTPUT", "-o", "tun+", "-j", "ACCEPT"],
            capture_output=True,
        )
        subprocess.run(["sudo", "ip6tables", "-P", "OUTPUT", "DROP"], capture_output=True)

        self.status["kill_switch_active"] = True
        self.log("Kill switch active — non-VPN output traffic blocked (IPv4 + IPv6)")

    def _tighten_killswitch_dns(self):
        """Remove the direct DNS exception rules now that tun0 is up.

        The DNS ACCEPT rules for 1.1.1.1/1.0.0.1 are needed during VPN startup
        so OpenVPN can resolve the server hostname before the tunnel exists.
        Once tun0 is up, redirect-gateway routes all traffic (including 1.1.1.1)
        through the tunnel — so the explicit exception is no longer needed and
        becomes a potential leak path if the tunnel drops unexpectedly.
        """
        removed = 0
        for dns_ip in ["1.1.1.1", "1.0.0.1"]:
            for p in ["udp", "tcp"]:
                r = subprocess.run(
                    ["sudo", "iptables", "-D", "OUTPUT",
                     "-d", dns_ip, "-p", p, "--dport", "53", "-j", "ACCEPT"],
                    capture_output=True,
                )
                if r.returncode == 0:
                    removed += 1
        if removed:
            self.log("DNS kill switch rules tightened — DNS now routes through tunnel")

    def teardown_killswitch(self):
        """Restore iptables and ip6tables to the state recorded before VPN was started."""
        # IPv4
        backup = os.path.join(_BACKUP_DIR, "iptables.backup")
        if os.path.exists(backup):
            self.log("Restoring original iptables rules...")
            result = subprocess.run(
                ["sudo", "iptables-restore", backup], capture_output=True
            )
            if result.returncode == 0:
                os.remove(backup)
                self.log("iptables restored")
            else:
                self.log("iptables-restore failed — resetting OUTPUT to ACCEPT", level="WARNING")
                subprocess.run(["sudo", "iptables", "-F", "OUTPUT"], capture_output=True)
                subprocess.run(
                    ["sudo", "iptables", "-P", "OUTPUT", "ACCEPT"], capture_output=True
                )
        else:
            self.log("No iptables backup found — resetting OUTPUT to ACCEPT")
            subprocess.run(["sudo", "iptables", "-F", "OUTPUT"], capture_output=True)
            subprocess.run(
                ["sudo", "iptables", "-P", "OUTPUT", "ACCEPT"], capture_output=True
            )

        # IPv6
        ip6backup = os.path.join(_BACKUP_DIR, "ip6tables.backup")
        if os.path.exists(ip6backup):
            self.log("Restoring original ip6tables rules...")
            result = subprocess.run(
                ["sudo", "ip6tables-restore", ip6backup], capture_output=True
            )
            if result.returncode == 0:
                os.remove(ip6backup)
                self.log("ip6tables restored")
            else:
                self.log("ip6tables-restore failed — resetting OUTPUT to ACCEPT", level="WARNING")
                subprocess.run(["sudo", "ip6tables", "-F", "OUTPUT"], capture_output=True)
                subprocess.run(
                    ["sudo", "ip6tables", "-P", "OUTPUT", "ACCEPT"], capture_output=True
                )
        else:
            subprocess.run(["sudo", "ip6tables", "-F", "OUTPUT"], capture_output=True)
            subprocess.run(
                ["sudo", "ip6tables", "-P", "OUTPUT", "ACCEPT"], capture_output=True
            )

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
        configs = glob.glob("/etc/openvpn/client/*.ovpn")
        if not configs:
            self.log("No .ovpn config file found in /etc/openvpn/client/", level="ERROR")
            return False
        config = configs[0]
        self.log(f"Using config: {config}")

        # 2. Apply / update kill switch BEFORE stopping existing OpenVPN.
        #    On reconnect this atomically updates the allowed VPN server IP so
        #    the new OpenVPN process can reach its endpoint through the kill switch.
        try:
            self.setup_killswitch(config)
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
            if self.check_vpn_interface():
                self.log("tun0 interface is up", source="OPENVPN")
                self._tighten_killswitch_dns()
                ip = self.get_external_ip()
                if ip:
                    self.log(f"VPN connected — external IP: {ip}")
                return True

        self.log("tun0 never came up — check OpenVPN log lines above", source="OPENVPN", level="ERROR")
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

    def download_ovpn(self, url):
        """Download a .ovpn file from url and install it. Runs in background."""
        def _run():
            self.log(f"Downloading OVPN config from: {url}")
            try:
                r = requests.get(url, timeout=15)
                r.raise_for_status()
            except Exception as e:
                self.log(f"Download failed — {e}", level="ERROR")
                return

            filename = url.split("/")[-1].split("?")[0]
            if not filename.endswith(".ovpn"):
                filename += ".ovpn"
            tmp = f"/tmp/{filename}"

            try:
                with open(tmp, "wb") as f:
                    f.write(r.content)
                self.log(f"Downloaded {len(r.content)} bytes")
            except Exception as e:
                self.log(f"Could not write temp file — {e}", level="ERROR")
                return

            self._install_ovpn(tmp, filename)

        threading.Thread(target=_run, daemon=True).start()

    def upload_ovpn(self, data, filename):
        """Install an uploaded .ovpn file (data is bytes). Runs in background."""
        def _run():
            self.log(f"Installing uploaded OVPN config: {filename} ({len(data)} bytes)")
            tmp = f"/tmp/{filename}"
            try:
                with open(tmp, "wb") as f:
                    f.write(data)
            except Exception as e:
                self.log(f"Could not write temp file — {e}", level="ERROR")
                return
            self._install_ovpn(tmp, filename)

        threading.Thread(target=_run, daemon=True).start()

    def start_vpn(self):
        """Run VPN start in a background thread so logs stream immediately."""
        def _thread():
            self.status["vpn_starting"] = True
            try:
                self._openvpn_start()
            finally:
                self.status["vpn_starting"] = False
        threading.Thread(target=_thread, daemon=True).start()

    def stop_vpn(self):
        self.log("Stopping OpenVPN...", source="OPENVPN")
        subprocess.run(["sudo", "pkill", "-f", "openvpn"], capture_output=True)
        self.log("OpenVPN stopped", source="OPENVPN")
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
        reconnect_count = 0

        while not self._stop_event.is_set():
            vpn_proc = self.check_openvpn_process()
            vpn_iface = self.check_vpn_interface()
            # Only check default route when tun0 is up — route is meaningless without it
            vpn_route = self.check_default_route() if vpn_iface else False
            self.status["vpn_process"] = vpn_proc
            self.status["vpn_interface"] = vpn_iface
            self.status["vpn_route"] = vpn_route

            if not vpn_proc or not vpn_iface or not vpn_route:
                what = (
                    "process" if not vpn_proc
                    else "interface" if not vpn_iface
                    else "default route (traffic bypassing tunnel)"
                )
                self.log(f"VPN {what} down!", level="CRITICAL")
                if self.is_qbittorrent_running():
                    self.stop_qbittorrent()

                if reconnect_count < self.max_reconnects:
                    reconnect_count += 1
                    self.status["reconnect_count"] = reconnect_count
                    self.log(f"Reconnect attempt {reconnect_count}/{self.max_reconnects}", level="WARNING")
                    if self.attempt_reconnect():
                        reconnect_count = 0
                        last_ip_check = time.time()
                        self.status["reconnect_count"] = 0
                        continue
                else:
                    self.log("Max reconnection attempts reached — stopping monitor", level="CRITICAL")
                    self.status["secure"] = False
                    break

            now_ts = time.time()
            if now_ts - last_ip_check >= self.ip_interval:
                ip = self.get_external_ip()
                self.status["external_ip"] = ip

                if ip is None:
                    self.log("Cannot determine external IP — treating as potential leak", level="WARNING")
                    self.status["secure"] = False
                    if self.is_qbittorrent_running():
                        self.stop_qbittorrent()
                    if reconnect_count < self.max_reconnects:
                        reconnect_count += 1
                        self.status["reconnect_count"] = reconnect_count
                        self.log(f"Reconnect attempt {reconnect_count}/{self.max_reconnects}", level="WARNING")
                        if self.attempt_reconnect():
                            reconnect_count = 0
                            last_ip_check = time.time()
                            self.status["reconnect_count"] = 0
                            continue
                    else:
                        self.log("Max reconnection attempts reached — stopping monitor", level="CRITICAL")
                        break
                elif ip.strip() == self.home_ip.strip():
                    self.log(f"IP LEAK DETECTED: external IP {ip} matches home IP", level="CRITICAL")
                    self.status["secure"] = False
                    if self.is_qbittorrent_running():
                        self.stop_qbittorrent()
                    if reconnect_count < self.max_reconnects:
                        reconnect_count += 1
                        self.status["reconnect_count"] = reconnect_count
                        self.log(f"Reconnect attempt {reconnect_count}/{self.max_reconnects}", level="WARNING")
                        if self.attempt_reconnect():
                            reconnect_count = 0
                            last_ip_check = time.time()
                            self.status["reconnect_count"] = 0
                            continue
                    else:
                        self.log("Max reconnection attempts reached — stopping monitor", level="CRITICAL")
                        break
                else:
                    self.status["secure"] = True
                    self.log(f"VPN secure — external IP: {ip}")
                    reconnect_count = 0
                    self.status["reconnect_count"] = 0

                last_ip_check = now_ts

            self.status["qbittorrent"] = self.is_qbittorrent_running()
            self._stop_event.wait(self.fast_interval)

        self.status["running"] = False
        if not self._stop_event.is_set():
            # Internal exit (max reconnects / failure) — shut everything down
            self.log("Monitoring stopped due to failures — shutting down qBittorrent and OpenVPN", level="WARNING")
            self.stop_qbittorrent()
            self.stop_vpn()
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
