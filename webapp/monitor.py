import glob
import subprocess
import threading
import time
from datetime import datetime

import requests

MAX_LOGS = 500


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")



def detect_external_ip():
    services = [
        ("https://api.ipify.org?format=json", lambda r: r.json().get("ip")),
        ("https://api64.ipify.org?format=json", lambda r: r.json().get("ip")),
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
            "vpn_process": False,
            "vpn_interface": False,
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

    def log(self, message):
        entry = f"[{_now()}] {message}"
        with self._log_condition:
            self._log_seq += 1
            self._logs.append((self._log_seq, entry))
            if len(self._logs) > MAX_LOGS:
                self._logs.pop(0)
            self._log_condition.notify_all()

    def stream_logs(self, from_seq=0):
        """Generator yielding log lines for SSE. Yields None as a keepalive."""
        while True:
            with self._log_condition:
                new = [(s, m) for s, m in self._logs if s > from_seq]
                if new:
                    for seq, msg in new:
                        from_seq = seq
                    # Release lock before yielding
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
        self.log("Stopping qBittorrent...")
        subprocess.run(["sudo", "pkill", "-f", "qbittorrent-nox"], capture_output=True)
        self.status["qbittorrent"] = False

    def start_qbittorrent(self):
        if self.is_qbittorrent_running():
            self.log("qBittorrent already running")
            return True
        self.log("Starting qBittorrent...")
        proc = subprocess.Popen(
            ["qbittorrent-nox"],
            stdout=open("qbit.log", "w"),
            stderr=subprocess.STDOUT,
        )
        time.sleep(1)
        if proc.poll() is None:
            self.log(f"qBittorrent started (PID: {proc.pid})")
            self.status["qbittorrent"] = True
            return True
        self.log("WARNING: qBittorrent may have failed to start")
        return False

    # ------------------------------------------------------- VPN start/stop

    def _openvpn_start(self):
        """Launch OpenVPN daemon and return True on success. Logs each step."""
        self.log("Stopping any existing OpenVPN process...")
        subprocess.run(["sudo", "pkill", "-f", "openvpn"], capture_output=True)
        time.sleep(2)

        configs = glob.glob("/etc/openvpn/client/*.ovpn")
        if not configs:
            self.log("ERROR: No .ovpn config file found in /etc/openvpn/client/")
            return False

        config = configs[0]
        self.log(f"Using config: {config}")
        self.log("Starting OpenVPN daemon...")

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

        stdout = result.stdout.decode().strip()
        stderr = result.stderr.decode().strip()
        if stdout:
            for line in stdout.splitlines():
                self.log(f"openvpn: {line}")
        if stderr:
            for line in stderr.splitlines():
                self.log(f"openvpn stderr: {line}")
        if result.returncode != 0:
            self.log(f"ERROR: openvpn exited with code {result.returncode}")
            return False

        self.log("OpenVPN daemon launched, waiting for interface...")
        last_log_lines = 0
        for i in range(15):
            time.sleep(2)
            # Stream new OpenVPN log lines (file is root-owned, use sudo)
            try:
                result = subprocess.run(
                    ["sudo", "cat", "/var/log/openvpn.log"],
                    capture_output=True, text=True, timeout=2
                )
                lines = [l for l in result.stdout.splitlines() if l.strip()]
                for line in lines[last_log_lines:]:
                    self.log(f"  openvpn: {line}")
                last_log_lines = len(lines)
            except Exception:
                pass
            if self.check_vpn_interface():
                self.log("VPN interface (tun0) is up")
                ip = self.get_external_ip()
                if ip:
                    self.log(f"VPN connected — external IP: {ip}")
                return True

        self.log("ERROR: tun0 never came up — check openvpn log lines above for details")
        return False

    def download_ovpn(self, url):
        """Download a .ovpn file from url and install it. Runs in background."""
        def _run():
            self.log(f"Downloading OVPN config from: {url}")
            try:
                r = requests.get(url, timeout=15)
                r.raise_for_status()
            except Exception as e:
                self.log(f"ERROR: Download failed — {e}")
                return

            filename = url.split("/")[-1].split("?")[0]
            if not filename.endswith(".ovpn"):
                filename += ".ovpn"
            tmp = f"/tmp/{filename}"
            dest = f"/etc/openvpn/client/{filename}"

            try:
                with open(tmp, "wb") as f:
                    f.write(r.content)
                self.log(f"Downloaded {len(r.content)} bytes")
            except Exception as e:
                self.log(f"ERROR: Could not write temp file — {e}")
                return

            # Remove existing configs before installing the new one
            for old in glob.glob("/etc/openvpn/client/*.ovpn"):
                rm = subprocess.run(["sudo", "rm", "-f", old], capture_output=True)
                if rm.returncode == 0:
                    self.log(f"Removed old config: {old}")
                else:
                    self.log(f"WARNING: Could not remove {old}")

            result = subprocess.run(["sudo", "mv", tmp, dest], capture_output=True)
            if result.returncode != 0:
                self.log(f"ERROR: Could not install config — {result.stderr.decode().strip()}")
                return

            self.log(f"Installed: {dest} — ready to Start VPN")

        threading.Thread(target=_run, daemon=True).start()

    def start_vpn(self):
        """Run VPN start in a background thread so logs stream immediately."""
        threading.Thread(target=self._openvpn_start, daemon=True).start()

    def stop_vpn(self):
        self.log("Stopping OpenVPN...")
        subprocess.run(["sudo", "pkill", "-f", "openvpn"], capture_output=True)
        self.log("OpenVPN stopped")

    def stop_vpn_bg(self):
        threading.Thread(target=self.stop_vpn, daemon=True).start()


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
        self.log("Reconnection failed")
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
            self.status["vpn_process"] = vpn_proc
            self.status["vpn_interface"] = vpn_iface

            if not vpn_proc or not vpn_iface:
                what = "process" if not vpn_proc else "interface"
                self.log(f"CRITICAL: VPN {what} down!")
                if self.is_qbittorrent_running():
                    self.stop_qbittorrent()

                if reconnect_count < self.max_reconnects:
                    reconnect_count += 1
                    self.status["reconnect_count"] = reconnect_count
                    self.log(f"Reconnect attempt {reconnect_count}/{self.max_reconnects}")
                    if self.attempt_reconnect():
                        reconnect_count = 0
                        last_ip_check = time.time()
                        self.status["reconnect_count"] = 0
                        continue
                else:
                    self.log("Max reconnection attempts reached. Stopping monitor.")
                    self.status["secure"] = False
                    break

            now_ts = time.time()
            if now_ts - last_ip_check >= self.ip_interval:
                ip = self.get_external_ip()
                self.status["external_ip"] = ip

                if ip is None:
                    self.log("Cannot determine external IP — network issue")
                    self.status["secure"] = False
                elif ip.strip() == self.home_ip.strip():
                    self.log(f"IP LEAK DETECTED: {ip}")
                    self.status["secure"] = False
                    if self.is_qbittorrent_running():
                        self.stop_qbittorrent()
                    if reconnect_count < self.max_reconnects:
                        reconnect_count += 1
                        self.status["reconnect_count"] = reconnect_count
                        if self.attempt_reconnect():
                            reconnect_count = 0
                            last_ip_check = time.time()
                            self.status["reconnect_count"] = 0
                            continue
                    else:
                        self.log("Max reconnection attempts reached. Stopping monitor.")
                        break
                else:
                    self.status["secure"] = True
                    self.log(f"VPN secure (external IP: {ip})")
                    reconnect_count = 0
                    self.status["reconnect_count"] = 0

                last_ip_check = now_ts

            self.status["qbittorrent"] = self.is_qbittorrent_running()
            self._stop_event.wait(self.fast_interval)

        self.status["running"] = False
        self.log("Monitoring stopped — shutting down qBittorrent and OpenVPN")
        self.stop_qbittorrent()
        self.stop_vpn()

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
