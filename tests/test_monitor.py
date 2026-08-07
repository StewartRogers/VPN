"""Tests for webapp/monitor.py — VPNMonitor state machine, reconnect logic, and kill switch."""
from unittest.mock import MagicMock, patch

import pytest

import monitor as mon
from monitor import VPNMonitor


# ------------------------------------------------------------------ helpers

def _proc(returncode=0, stdout=b"", stderr=b"", text_stdout=""):
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout if isinstance(stdout, bytes) else stdout.encode()
    m.stderr = stderr if isinstance(stderr, bytes) else stderr.encode()
    # text=True variant
    if text_stdout:
        m.stdout = text_stdout
    return m


def make_monitor(home_ip="1.2.3.4", fast=0, ip_interval=0):
    return VPNMonitor(home_ip=home_ip, fast_interval=fast, ip_interval=ip_interval)


# ------------------------------------------------------------------ system checks

class TestSystemChecks:
    def test_check_openvpn_process_true(self):
        m = make_monitor()
        with patch("subprocess.run", return_value=_proc(0)):
            assert m.check_openvpn_process() is True

    def test_check_openvpn_process_false(self):
        m = make_monitor()
        with patch("subprocess.run", return_value=_proc(1)):
            assert m.check_openvpn_process() is False

    def test_check_vpn_interface_true(self):
        m = make_monitor()
        with patch("subprocess.run", return_value=_proc(0)):
            assert m.check_vpn_interface() is True

    def test_check_vpn_interface_false(self):
        m = make_monitor()
        with patch("subprocess.run", return_value=_proc(1)):
            assert m.check_vpn_interface() is False

    def test_check_default_route_true_when_tun0_in_output(self):
        m = make_monitor()
        result = _proc(0)
        result.stdout = "8.8.8.8 via 10.8.0.1 dev tun0 src 10.8.0.2"
        with patch("subprocess.run", return_value=result):
            assert m.check_default_route() is True

    def test_check_default_route_false_when_tun0_absent(self):
        m = make_monitor()
        result = _proc(0)
        result.stdout = "8.8.8.8 via 192.168.1.1 dev eth0 src 192.168.1.5"
        with patch("subprocess.run", return_value=result):
            assert m.check_default_route() is False

    def test_check_killswitch_active_true(self):
        m = make_monitor()
        result = _proc(0)
        result.stdout = "Default: deny (outgoing)\n  allow (incoming)"
        with patch("subprocess.run", return_value=result):
            assert m.check_killswitch_active() is True

    def test_check_killswitch_active_false(self):
        m = make_monitor()
        result = _proc(0)
        result.stdout = "Default: allow (outgoing)\n  disabled (routed)"
        with patch("subprocess.run", return_value=result):
            assert m.check_killswitch_active() is False


# ------------------------------------------------------------------ detect_external_ip

class TestDetectExternalIp:
    def test_returns_ip_on_success(self):
        resp = MagicMock()
        resp.json.return_value = {"ip": "5.6.7.8"}
        with patch("requests.get", return_value=resp):
            assert mon.detect_external_ip() == "5.6.7.8"

    def test_falls_back_to_next_service_on_exception(self):
        good = MagicMock()
        good.text = "9.10.11.12\n"
        with patch("requests.get", side_effect=[Exception("timeout"), good]):
            assert mon.detect_external_ip() == "9.10.11.12"

    def test_returns_none_when_all_fail(self):
        with patch("requests.get", side_effect=Exception("timeout")):
            assert mon.detect_external_ip() is None


# ------------------------------------------------------------------ kill switch behaviour

class TestKillSwitch:
    def test_setup_killswitch_sets_flag_on_success(self):
        m = make_monitor()
        with patch("subprocess.run", return_value=_proc(0)):
            m.setup_killswitch()
        assert m.status["kill_switch_active"] is True

    def test_setup_killswitch_raises_on_failure(self):
        m = make_monitor()
        with patch("subprocess.run", return_value=_proc(1)):
            with pytest.raises(RuntimeError):
                m.setup_killswitch()
        assert m.status["kill_switch_active"] is False

    def test_teardown_killswitch_clears_flag(self):
        m = make_monitor()
        m.status["kill_switch_active"] = True
        with patch("subprocess.run", return_value=_proc(0)):
            m.teardown_killswitch()
        assert m.status["kill_switch_active"] is False

    def test_teardown_killswitch_clears_flag_even_on_failure(self):
        m = make_monitor()
        m.status["kill_switch_active"] = True
        with patch("subprocess.run", return_value=_proc(1)):
            m.teardown_killswitch()
        assert m.status["kill_switch_active"] is False


# ------------------------------------------------------------------ qBittorrent lifecycle

class TestQbittorrent:
    def test_stop_sets_status_false(self):
        m = make_monitor()
        m.status["qbittorrent"] = True
        with patch("subprocess.run"):
            m.stop_qbittorrent()
        assert m.status["qbittorrent"] is False

    def test_is_qbittorrent_running_true(self):
        m = make_monitor()
        with patch("subprocess.run", return_value=_proc(0)):
            assert m.is_qbittorrent_running() is True

    def test_is_qbittorrent_running_false(self):
        m = make_monitor()
        with patch("subprocess.run", return_value=_proc(1)):
            assert m.is_qbittorrent_running() is False


# ------------------------------------------------------------------ _check_ovpn_url (SSRF guard)

class TestCheckOvpnUrl:
    def test_accepts_valid_https_public_url(self):
        with patch("socket.gethostbyname", return_value="1.2.3.4"):
            assert VPNMonitor._check_ovpn_url("https://example.com/config.ovpn") is None

    def test_rejects_http(self):
        assert VPNMonitor._check_ovpn_url("http://example.com/config.ovpn") is not None

    def test_rejects_private_ip(self):
        with patch("socket.gethostbyname", return_value="192.168.1.1"):
            assert VPNMonitor._check_ovpn_url("https://internal.lan/config.ovpn") is not None

    def test_rejects_loopback(self):
        with patch("socket.gethostbyname", return_value="127.0.0.1"):
            assert VPNMonitor._check_ovpn_url("https://localhost/config.ovpn") is not None

    def test_rejects_url_with_no_host(self):
        assert VPNMonitor._check_ovpn_url("https:///config.ovpn") is not None

    def test_rejects_unresolvable_host(self):
        import socket as _socket
        with patch("socket.gethostbyname", side_effect=_socket.gaierror("no such host")):
            assert VPNMonitor._check_ovpn_url("https://doesnotexist.invalid/a.ovpn") is not None


# ------------------------------------------------------------------ monitor loop — VPN failure path

class TestMonitorLoopVpnFailure:
    def _run_loop(self, monitor, side_effects_proc, side_effects_iface, side_effects_route=None):
        """Drive _run() with controlled check results."""
        proc_iter = iter(side_effects_proc)
        iface_iter = iter(side_effects_iface)
        route_iter = iter(side_effects_route or [True] * 20)

        def fake_proc():
            try:
                return next(proc_iter)
            except StopIteration:
                return False

        def fake_iface():
            try:
                return next(iface_iter)
            except StopIteration:
                return False

        def fake_route():
            try:
                return next(route_iter)
            except StopIteration:
                return False

        monitor.check_openvpn_process = fake_proc
        monitor.check_vpn_interface = fake_iface
        monitor.check_default_route = fake_route
        monitor.get_external_ip = MagicMock(return_value="5.6.7.8")
        monitor.is_qbittorrent_running = MagicMock(return_value=True)
        monitor.stop_qbittorrent = MagicMock()
        monitor._stop_event.wait = MagicMock()  # don't actually sleep

        with patch("subprocess.run"):
            monitor._run()

    def test_qbittorrent_stopped_on_vpn_process_failure(self):
        m = make_monitor(ip_interval=9999)
        self._run_loop(m, [False], [True])
        m.stop_qbittorrent.assert_called()

    def test_qbittorrent_stopped_on_interface_failure(self):
        m = make_monitor(ip_interval=9999)
        self._run_loop(m, [True], [False])
        m.stop_qbittorrent.assert_called()

    def test_qbittorrent_stopped_on_ip_leak(self):
        m = make_monitor(home_ip="1.2.3.4", ip_interval=0)
        m.check_openvpn_process = MagicMock(return_value=True)
        m.check_vpn_interface = MagicMock(return_value=True)
        m.check_default_route = MagicMock(return_value=True)
        m.get_external_ip = MagicMock(return_value="1.2.3.4")  # matches home IP
        m.is_qbittorrent_running = MagicMock(return_value=True)
        m.stop_qbittorrent = MagicMock()
        m._stop_event.wait = MagicMock()
        with patch("subprocess.run"):
            m._run()
        m.stop_qbittorrent.assert_called()

    def test_status_secure_false_on_leak(self):
        m = make_monitor(home_ip="1.2.3.4", ip_interval=0)
        m.check_openvpn_process = MagicMock(return_value=True)
        m.check_vpn_interface = MagicMock(return_value=True)
        m.check_default_route = MagicMock(return_value=True)
        m.get_external_ip = MagicMock(return_value="1.2.3.4")
        m.is_qbittorrent_running = MagicMock(return_value=False)
        m.stop_qbittorrent = MagicMock()
        m._stop_event.wait = MagicMock()
        with patch("subprocess.run"):
            m._run()
        assert m.status["secure"] is False

    def test_kill_switch_remains_active_after_vpn_failure(self):
        """Kill switch must NOT be torn down on internal VPN failure (regression for PR #4)."""
        m = make_monitor(ip_interval=9999)
        m.status["kill_switch_active"] = True
        m.teardown_killswitch = MagicMock()
        self._run_loop(m, [False], [True])
        m.teardown_killswitch.assert_not_called()
        assert m.status["kill_switch_active"] is True

    def test_status_running_false_after_loop_exits(self):
        m = make_monitor(ip_interval=9999)
        self._run_loop(m, [False], [True])
        assert m.status["running"] is False


# ------------------------------------------------------------------ monitor loop — IP error tolerance

class TestMonitorLoopIpErrors:
    def test_tolerates_two_consecutive_ip_errors(self):
        """Monitor should not stop after 1 or 2 consecutive IP check failures."""
        m = make_monitor(home_ip="1.2.3.4", ip_interval=0)
        m.check_openvpn_process = MagicMock(return_value=True)
        m.check_vpn_interface = MagicMock(return_value=True)
        m.check_default_route = MagicMock(return_value=True)
        # Two failures then success then stop
        m.get_external_ip = MagicMock(side_effect=[None, None, "5.6.7.8", "5.6.7.8"])
        m.is_qbittorrent_running = MagicMock(return_value=False)
        m.stop_qbittorrent = MagicMock()

        call_count = 0
        def stop_after_success(*a, **kw):
            nonlocal call_count
            call_count += 1
            if call_count >= 3:
                m._stop_event.set()

        m._stop_event.wait = stop_after_success

        with patch("subprocess.run"):
            m._run()

        m.stop_qbittorrent.assert_not_called()

    def test_stops_after_three_consecutive_ip_errors(self):
        m = make_monitor(home_ip="1.2.3.4", ip_interval=0)
        m.check_openvpn_process = MagicMock(return_value=True)
        m.check_vpn_interface = MagicMock(return_value=True)
        m.check_default_route = MagicMock(return_value=True)
        m.get_external_ip = MagicMock(return_value=None)
        m.is_qbittorrent_running = MagicMock(return_value=True)
        m.stop_qbittorrent = MagicMock()
        m._stop_event.wait = MagicMock()
        with patch("subprocess.run"):
            m._run()
        m.stop_qbittorrent.assert_called()
        assert m.status["secure"] is False


# ------------------------------------------------------------------ stop_all ordering

class TestStopAll:
    def test_stop_all_stops_qbittorrent_before_vpn(self):
        m = make_monitor()
        call_order = []
        m.is_qbittorrent_running = MagicMock(return_value=True)
        m.stop_qbittorrent = MagicMock(side_effect=lambda: call_order.append("qbt"))
        m.stop_vpn = MagicMock(side_effect=lambda: call_order.append("vpn"))
        m.stop_all()
        assert call_order == ["qbt", "vpn"]

    def test_stop_all_sets_stop_event(self):
        m = make_monitor()
        m.is_qbittorrent_running = MagicMock(return_value=False)
        m.stop_vpn = MagicMock()
        m.stop_all()
        assert m._stop_event.is_set()

    def test_stop_vpn_calls_teardown_killswitch(self):
        m = make_monitor()
        m.teardown_killswitch = MagicMock()
        m.restore_ipv6 = MagicMock()
        m.restore_dns = MagicMock()
        with patch("subprocess.run"):
            m.stop_vpn()
        m.teardown_killswitch.assert_called_once()


# ------------------------------------------------------------------ logging

class TestLogging:
    def test_log_appends_entry(self):
        m = make_monitor()
        m.log("hello")
        assert any("hello" in msg for _, msg in m._logs)

    def test_log_includes_source_tag(self):
        m = make_monitor()
        m.log("msg", source="OPENVPN")
        assert any("[OPENVPN]" in msg for _, msg in m._logs)

    def test_log_includes_level_tag(self):
        m = make_monitor()
        m.log("bad", level="ERROR")
        assert any("[ERROR]" in msg for _, msg in m._logs)

    def test_recent_logs_returns_last_n(self):
        m = make_monitor()
        for i in range(10):
            m.log(f"msg{i}")
        recent = m.recent_logs(3)
        assert len(recent) == 3
        assert "msg9" in recent[-1]

    def test_log_seq_increments(self):
        m = make_monitor()
        m.log("a")
        m.log("b")
        seqs = [s for s, _ in m._logs]
        assert seqs == sorted(set(seqs))
        assert len(set(seqs)) == 2
