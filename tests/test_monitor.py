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


class TestKillswitchProbe:
    """The probe must distinguish 'UFW says no' from 'UFW did not answer'.

    Collapsing the two is what fail-stopped a healthy session on 2026-08-14:
    `ufw status verbose` blocked past its timeout under load and the timeout was
    reported as a torn-down firewall.
    """

    def _stdout(self, text):
        result = _proc(0)
        result.stdout = text
        return result

    def test_probe_active(self):
        m = make_monitor()
        with patch("subprocess.run", return_value=self._stdout("Default: deny (outgoing)")):
            assert m.probe_killswitch() == "active"

    def test_probe_inactive(self):
        m = make_monitor()
        with patch("subprocess.run", return_value=self._stdout("Default: allow (outgoing)")):
            assert m.probe_killswitch() == "inactive"

    def test_probe_timeout_is_unknown_not_inactive(self):
        m = make_monitor()
        import subprocess as _sp
        with patch("subprocess.run", side_effect=_sp.TimeoutExpired("ufw", 10)):
            assert m.probe_killswitch() == "unknown"

    def test_probe_nonzero_exit_is_unknown(self):
        """ufw failing (e.g. missing passwordless sudo) is not evidence of policy."""
        m = make_monitor()
        result = _proc(1)
        result.stdout = ""
        with patch("subprocess.run", return_value=result):
            assert m.probe_killswitch() == "unknown"

    def test_check_killswitch_active_fails_closed_on_unknown(self):
        """Anything gated on the bool helper must treat 'unknown' as unsafe."""
        m = make_monitor()
        import subprocess as _sp
        with patch("subprocess.run", side_effect=_sp.TimeoutExpired("ufw", 10)):
            assert m.check_killswitch_active() is False

    def test_result_is_cached_across_callers(self):
        """UI polling must not multiply `ufw` processes."""
        m = make_monitor()
        with patch("subprocess.run", return_value=self._stdout("Default: deny (outgoing)")) as run:
            for _ in range(5):
                assert m.probe_killswitch() == "active"
            assert run.call_count == 1

    def test_force_bypasses_cache(self):
        m = make_monitor()
        with patch("subprocess.run", return_value=self._stdout("Default: deny (outgoing)")) as run:
            m.probe_killswitch()
            m.probe_killswitch(force=True)
            assert run.call_count == 2

    def test_cache_expires(self):
        m = make_monitor()
        with patch("subprocess.run", return_value=self._stdout("Default: deny (outgoing)")) as run:
            m.probe_killswitch()
            m._ks_cache = (m._ks_cache[0] - mon.KILLSWITCH_CACHE_TTL - 1, "active")
            m.probe_killswitch()
            assert run.call_count == 2

    def test_setup_killswitch_invalidates_cache(self):
        m = make_monitor()
        m._ks_cache = (9e9, "inactive")
        with patch("subprocess.run", return_value=_proc(0)):
            m.setup_killswitch()
        assert m._ks_cache is None

    def test_teardown_killswitch_invalidates_cache(self):
        m = make_monitor()
        m._ks_cache = (9e9, "active")
        with patch("subprocess.run", return_value=_proc(0)):
            m.teardown_killswitch()
        assert m._ks_cache is None


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

        monitor.probe_openvpn_process = fake_proc
        monitor.probe_vpn_interface = fake_iface
        monitor.probe_default_route = fake_route
        # The loop re-checks the kill switch on the IP-check cadence. Without
        # this, subprocess.run is a bare MagicMock whose stdout does not contain
        # "deny (outgoing)", so every test below would exit via the kill-switch
        # branch instead of the one it means to exercise.
        monitor.probe_killswitch = MagicMock(return_value="active")
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
        m.probe_openvpn_process = MagicMock(return_value=True)
        m.probe_vpn_interface = MagicMock(return_value=True)
        m.probe_default_route = MagicMock(return_value=True)
        m.probe_killswitch = MagicMock(return_value="active")
        m.get_external_ip = MagicMock(return_value="1.2.3.4")  # matches home IP
        m.is_qbittorrent_running = MagicMock(return_value=True)
        m.stop_qbittorrent = MagicMock()
        m._stop_event.wait = MagicMock()
        with patch("subprocess.run"):
            m._run()
        m.stop_qbittorrent.assert_called()

    def test_status_secure_false_on_leak(self):
        m = make_monitor(home_ip="1.2.3.4", ip_interval=0)
        m.probe_openvpn_process = MagicMock(return_value=True)
        m.probe_vpn_interface = MagicMock(return_value=True)
        m.probe_default_route = MagicMock(return_value=True)
        m.probe_killswitch = MagicMock(return_value="active")
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
        m.probe_openvpn_process = MagicMock(return_value=True)
        m.probe_vpn_interface = MagicMock(return_value=True)
        m.probe_default_route = MagicMock(return_value=True)
        m.probe_killswitch = MagicMock(return_value="active")
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
        m.probe_openvpn_process = MagicMock(return_value=True)
        m.probe_vpn_interface = MagicMock(return_value=True)
        m.probe_default_route = MagicMock(return_value=True)
        m.probe_killswitch = MagicMock(return_value="active")
        m.get_external_ip = MagicMock(return_value=None)
        m.is_qbittorrent_running = MagicMock(return_value=True)
        m.stop_qbittorrent = MagicMock()
        m._stop_event.wait = MagicMock()
        with patch("subprocess.run"):
            m._run()
        m.stop_qbittorrent.assert_called()
        assert m.status["secure"] is False


class TestTunnelBindVerification:
    """Writing Session\\InterfaceAddress is not proof the bind took effect.
    qBittorrent reads its config once at startup and silently falls back to
    all interfaces if the address is absent then — and VPNGate issues a new
    tun0 IP every connection, so a stale address is the normal case."""

    WEBUI = ('LISTEN 0 4096 0.0.0.0:8080 0.0.0.0:* '
             'users:(("qbittorrent-nox",pid=1,fd=20))')

    def _ss(self, *rows):
        r = _proc(0)
        r.stdout = "\n".join(rows)
        return r

    def _check(self, m, *rows, tun0="10.211.1.41"):
        with patch("qbt_config.detect_tun0_ip", return_value=tun0), \
             patch.object(VPNMonitor, "_qbt_peer_port", return_value=19806), \
             patch("subprocess.run", return_value=self._ss(*rows)):
            return m.verify_tunnel_bind()

    def test_bound_when_peer_port_is_on_tun0(self):
        m = make_monitor()
        peer = ('LISTEN 0 4096 10.211.1.41:19806 0.0.0.0:* '
                'users:(("qbittorrent-nox",pid=1,fd=25))')
        assert self._check(m, self.WEBUI, peer) == "bound"

    def test_webui_on_all_interfaces_is_not_a_failure(self):
        """Regression: WebUI\\Address=* puts the dashboard on 0.0.0.0:8080 by
        design. Judging every qbittorrent socket flagged that healthy listener
        and killed the client on a correctly bound session (2026-08-14 11:18)."""
        m = make_monitor()
        peer = ('LISTEN 0 4096 10.211.1.41:19806 0.0.0.0:* '
                'users:(("qbittorrent-nox",pid=1,fd=25))')
        assert self._check(m, self.WEBUI, peer) == "bound"

    def test_unbound_when_peer_port_is_on_all_interfaces(self):
        m = make_monitor()
        peer = ('LISTEN 0 4096 0.0.0.0:19806 0.0.0.0:* '
                'users:(("qbittorrent-nox",pid=1,fd=25))')
        assert self._check(m, self.WEBUI, peer) == "unbound"

    def test_unbound_when_peer_port_is_on_the_wrong_address(self):
        m = make_monitor()
        peer = ('LISTEN 0 4096 192.168.1.5:19806 0.0.0.0:* '
                'users:(("qbittorrent-nox",pid=1,fd=25))')
        assert self._check(m, self.WEBUI, peer) == "unbound"

    def test_unknown_while_the_peer_socket_is_still_opening(self):
        """libtorrent opens its peer socket seconds after the process starts.
        Absent is not the same as unbound."""
        m = make_monitor()
        assert self._check(m, self.WEBUI) == "unknown"

    def test_unknown_when_tun0_has_no_address(self):
        m = make_monitor()
        with patch("qbt_config.detect_tun0_ip", return_value=None):
            assert m.verify_tunnel_bind() == "unknown"

    def test_unknown_when_no_qbittorrent_rows_at_all(self):
        m = make_monitor()
        assert self._check(m, "LISTEN 0 128 *:22 *:*") == "unknown"

    def test_already_running_but_unbound_is_refused(self):
        """Regression: this used to return True without applying the config,
        so a client left over from a previous tunnel kept its dead bind."""
        m = make_monitor()
        m.is_qbittorrent_running = MagicMock(return_value=True)
        m.verify_tunnel_bind = MagicMock(return_value="unbound")
        assert m.start_qbittorrent() is False

    def test_already_running_and_bound_is_accepted(self):
        m = make_monitor()
        m.is_qbittorrent_running = MagicMock(return_value=True)
        m.verify_tunnel_bind = MagicMock(return_value="bound")
        assert m.start_qbittorrent() is True

    def test_start_stops_the_client_if_bind_did_not_take(self):
        m = make_monitor()
        m.is_qbittorrent_running = MagicMock(return_value=False)
        m.torrent_start_blocked = MagicMock(return_value=None)
        m.apply_qbittorrent_config = MagicMock()
        m.apply_tunnel_bind = MagicMock(return_value=True)
        m.verify_tunnel_bind = MagicMock(return_value="unbound")
        m.stop_qbittorrent = MagicMock(return_value=True)
        proc = MagicMock(); proc.poll.return_value = None; proc.pid = 123
        with patch("subprocess.Popen", return_value=proc), patch("builtins.open"), \
             patch("time.sleep"):
            assert m.start_qbittorrent() is False
        m.stop_qbittorrent.assert_called()


# ------------------------------------------------- qBittorrent stop semantics

class TestStopQbittorrent:
    """Two rules: report the truth about whether the client is gone, and only
    skip the graceful window when waiting would actually leak."""

    def test_returns_true_when_already_stopped(self):
        m = make_monitor()
        m.is_qbittorrent_running = MagicMock(return_value=False)
        assert m.stop_qbittorrent() is True

    def test_graceful_path_sends_sigterm_first(self):
        m = make_monitor()
        # alive for the initial check, gone on the first poll
        m.is_qbittorrent_running = MagicMock(side_effect=[True, False])
        with patch("subprocess.run") as run:
            assert m.stop_qbittorrent() is True
        signals = [c.args[0] for c in run.call_args_list]
        assert signals == [["sudo", "pkill", "-f", "qbittorrent-nox"]]
        assert m.status["qbittorrent"] is False

    def test_urgent_path_sends_sigkill_immediately(self):
        """A confirmed-down kill switch means it is leaking now — no SIGTERM,
        no grace period."""
        m = make_monitor()
        m.is_qbittorrent_running = MagicMock(side_effect=[True, False])
        with patch("subprocess.run") as run:
            assert m.stop_qbittorrent(urgent=True) is True
        signals = [c.args[0] for c in run.call_args_list]
        assert signals == [["sudo", "pkill", "-9", "-f", "qbittorrent-nox"]]

    def test_graceful_path_escalates_to_sigkill(self):
        m = make_monitor()
        m.is_qbittorrent_running = MagicMock(return_value=True)   # never dies
        m._await_qbittorrent_exit = MagicMock(side_effect=[False, False])
        with patch("subprocess.run") as run:
            assert m.stop_qbittorrent() is False
        signals = [c.args[0] for c in run.call_args_list]
        assert signals == [
            ["sudo", "pkill", "-f", "qbittorrent-nox"],
            ["sudo", "pkill", "-9", "-f", "qbittorrent-nox"],
        ]

    def test_reports_failure_and_does_not_claim_stopped(self):
        """Regression: status used to be set to stopped unconditionally, even
        after a SIGKILL that did not work."""
        m = make_monitor()
        m.is_qbittorrent_running = MagicMock(return_value=True)
        m._await_qbittorrent_exit = MagicMock(return_value=False)
        with patch("subprocess.run"):
            assert m.stop_qbittorrent() is False
        assert m.status["qbittorrent"] is True

    def test_grace_period_allows_a_slow_clean_exit(self):
        """The 2026-08-14 shutdown needed 4.3s and was killed at 5s."""
        m = make_monitor()
        calls = {"n": 0}
        def alive():
            calls["n"] += 1
            return calls["n"] < 12          # ~5.5s of polling at 0.5s
        m.is_qbittorrent_running = alive
        with patch("subprocess.run") as run, patch("time.sleep"):
            assert m.stop_qbittorrent() is True
        signals = [c.args[0] for c in run.call_args_list]
        assert ["sudo", "pkill", "-9", "-f", "qbittorrent-nox"] not in signals


class TestTeardownGating:
    """'Only do the next step when the previous step is complete.'"""

    def test_stop_vpn_halts_before_openvpn_if_qbt_survives(self):
        m = make_monitor()
        m.stop_qbittorrent = MagicMock(return_value=False)
        m.teardown_killswitch = MagicMock()
        with patch("subprocess.run") as run:
            assert m.stop_vpn() is False
        m.teardown_killswitch.assert_not_called()
        assert not any("openvpn" in str(c.args[0]) for c in run.call_args_list)

    def test_stop_vpn_halts_before_teardown_if_openvpn_survives(self):
        m = make_monitor()
        m.stop_qbittorrent = MagicMock(return_value=True)
        m.check_openvpn_process = MagicMock(return_value=True)   # never dies
        m.teardown_killswitch = MagicMock()
        m.restore_ipv6 = MagicMock()
        m.restore_dns = MagicMock()
        with patch("subprocess.run"), patch("time.sleep"):
            assert m.stop_vpn() is False
        m.teardown_killswitch.assert_not_called()
        m.restore_dns.assert_not_called()

    def test_stop_vpn_completes_when_every_step_confirms(self):
        m = make_monitor()
        m.stop_qbittorrent = MagicMock(return_value=True)
        m.check_openvpn_process = MagicMock(return_value=False)
        m.is_qbittorrent_running = MagicMock(return_value=False)
        m.teardown_killswitch = MagicMock()
        m.restore_ipv6 = MagicMock()
        m.restore_dns = MagicMock()
        with patch("subprocess.run"):
            assert m.stop_vpn() is True
        m.teardown_killswitch.assert_called()

    def test_reconnect_aborts_if_qbt_survives(self):
        """_openvpn_start reapplies the kill switch, briefly restoring the
        default allow-outgoing policy."""
        m = make_monitor()
        m.stop_qbittorrent = MagicMock(return_value=False)
        m._openvpn_start = MagicMock()
        assert m.attempt_reconnect() is False
        m._openvpn_start.assert_not_called()


# ------------------------------------------------------------------ stop_all ordering

class TestStopAll:
    def test_stop_all_stops_qbittorrent_before_vpn(self):
        m = make_monitor()
        call_order = []
        m.is_qbittorrent_running = MagicMock(return_value=True)
        # stop_qbittorrent now reports whether the client is *confirmed* gone;
        # returning True is what lets the sequence advance.
        m.stop_qbittorrent = MagicMock(side_effect=lambda: call_order.append("qbt") or True)
        m.stop_vpn = MagicMock(side_effect=lambda: call_order.append("vpn"))
        m.stop_all()
        assert call_order == ["qbt", "vpn"]

    def test_stop_all_halts_when_qbittorrent_will_not_die(self):
        """Every later step is skipped — the kill switch must not come down
        while a torrent client is still alive."""
        m = make_monitor()
        m.stop_qbittorrent = MagicMock(return_value=False)
        m.stop_vpn = MagicMock()
        assert m.stop_all() is False
        m.stop_vpn.assert_not_called()

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


# ------------------------------------------------- monitor loop — kill switch

class TestMonitorLoopKillSwitch:
    def test_stops_when_kill_switch_disappears(self):
        """A UFW reset from another terminal must not go unnoticed."""
        m = make_monitor(home_ip="1.2.3.4", ip_interval=0)
        m.probe_openvpn_process = MagicMock(return_value=True)
        m.probe_vpn_interface = MagicMock(return_value=True)
        m.probe_default_route = MagicMock(return_value=True)
        m.probe_killswitch = MagicMock(return_value="inactive")
        m.get_external_ip = MagicMock(return_value="5.6.7.8")
        m.is_qbittorrent_running = MagicMock(return_value=True)
        m.stop_qbittorrent = MagicMock()
        m._stop_event.wait = MagicMock()
        with patch("subprocess.run"):
            m._run()
        m.stop_qbittorrent.assert_called()
        assert m.status["secure"] is False
        assert m.status["kill_switch_active"] is False

    def test_tolerates_two_inconclusive_kill_switch_checks(self):
        """Regression for 2026-08-14: a slow `ufw status` must not kill a healthy
        session. The tunnel was up and the exit IP correct; only the probe timed
        out, and the monitor tore down a working session over it."""
        m = make_monitor(home_ip="1.2.3.4", ip_interval=0)
        m.probe_openvpn_process = MagicMock(return_value=True)
        m.probe_vpn_interface = MagicMock(return_value=True)
        m.probe_default_route = MagicMock(return_value=True)
        m.probe_killswitch = MagicMock(side_effect=["unknown", "unknown", "active", "active"])
        m.get_external_ip = MagicMock(return_value="5.6.7.8")
        m.is_qbittorrent_running = MagicMock(return_value=True)
        m.stop_qbittorrent = MagicMock()

        call_count = 0
        def stop_after_recovery(*a, **kw):
            nonlocal call_count
            call_count += 1
            if call_count >= 3:
                m._stop_event.set()
        m._stop_event.wait = stop_after_recovery

        with patch("subprocess.run"):
            m._run()

        m.stop_qbittorrent.assert_not_called()

    def test_stops_after_three_inconclusive_kill_switch_checks(self):
        """Tolerance is bounded — a probe that never answers is still a reason
        to stop, just not on the first miss."""
        m = make_monitor(home_ip="1.2.3.4", ip_interval=0)
        m.probe_openvpn_process = MagicMock(return_value=True)
        m.probe_vpn_interface = MagicMock(return_value=True)
        m.probe_default_route = MagicMock(return_value=True)
        m.probe_killswitch = MagicMock(return_value="unknown")
        m.get_external_ip = MagicMock(return_value="5.6.7.8")
        m.is_qbittorrent_running = MagicMock(return_value=True)
        m.stop_qbittorrent = MagicMock()
        m._stop_event.wait = MagicMock()
        with patch("subprocess.run"):
            m._run()
        m.stop_qbittorrent.assert_called()
        assert m.status["secure"] is False

    def test_inconclusive_streak_resets_on_a_good_check(self):
        """Two misses, a success, then two more misses must not trip."""
        m = make_monitor(home_ip="1.2.3.4", ip_interval=0)
        m.probe_openvpn_process = MagicMock(return_value=True)
        m.probe_vpn_interface = MagicMock(return_value=True)
        m.probe_default_route = MagicMock(return_value=True)
        m.probe_killswitch = MagicMock(
            side_effect=["unknown", "unknown", "active", "unknown", "unknown", "active"]
        )
        m.get_external_ip = MagicMock(return_value="5.6.7.8")
        m.is_qbittorrent_running = MagicMock(return_value=True)
        m.stop_qbittorrent = MagicMock()

        call_count = 0
        def stop_at_end(*a, **kw):
            nonlocal call_count
            call_count += 1
            if call_count >= 5:
                m._stop_event.set()
        m._stop_event.wait = stop_at_end

        with patch("subprocess.run"):
            m._run()

        m.stop_qbittorrent.assert_not_called()

    def test_confirmed_teardown_kills_qbittorrent_urgently(self):
        """Kill switch confirmed down = UFW is passing traffic and the client is
        egressing on the ISP link. No grace period."""
        m = make_monitor(home_ip="1.2.3.4", ip_interval=0)
        m.probe_openvpn_process = MagicMock(return_value=True)
        m.probe_vpn_interface = MagicMock(return_value=True)
        m.probe_default_route = MagicMock(return_value=True)
        m.probe_killswitch = MagicMock(return_value="inactive")
        m.get_external_ip = MagicMock(return_value="5.6.7.8")
        m.is_qbittorrent_running = MagicMock(return_value=True)
        m.stop_qbittorrent = MagicMock(return_value=True)
        m._stop_event.wait = MagicMock()
        with patch("subprocess.run"):
            m._run()
        m.stop_qbittorrent.assert_called_once_with(urgent=True)

    def test_other_failures_stop_qbittorrent_gracefully(self):
        """Kill switch is still up on these paths, so UFW is blocking all
        outgoing traffic — the client can be allowed to exit cleanly."""
        m = make_monitor(home_ip="1.2.3.4", ip_interval=0)
        m.probe_openvpn_process = MagicMock(return_value=False)   # VPN down
        m.probe_vpn_interface = MagicMock(return_value=True)
        m.probe_default_route = MagicMock(return_value=True)
        m.probe_killswitch = MagicMock(return_value="active")
        m.is_qbittorrent_running = MagicMock(return_value=True)
        m.stop_qbittorrent = MagicMock(return_value=True)
        m._stop_event.wait = MagicMock()
        with patch("subprocess.run"):
            m._run()
        m.stop_qbittorrent.assert_called_once_with(urgent=False)

    def test_openvpn_is_not_stopped_until_qbittorrent_is_confirmed_gone(self):
        m = make_monitor(home_ip="1.2.3.4", ip_interval=0)
        m.probe_openvpn_process = MagicMock(return_value=True)
        m.probe_vpn_interface = MagicMock(return_value=True)
        m.probe_default_route = MagicMock(return_value=True)
        m.probe_killswitch = MagicMock(return_value="inactive")
        m.get_external_ip = MagicMock(return_value="5.6.7.8")
        m.is_qbittorrent_running = MagicMock(return_value=True)
        m.stop_qbittorrent = MagicMock(return_value=False)        # survives
        m._stop_event.wait = MagicMock()
        with patch("subprocess.run") as run:
            m._run()
        assert not any("openvpn" in str(c.args[0]) for c in run.call_args_list)

    def test_confirmed_teardown_still_trips_on_the_first_check(self):
        """The retry tolerance must not soften a real teardown — 'inactive' is a
        definite answer and gets no grace period."""
        m = make_monitor(home_ip="1.2.3.4", ip_interval=0)
        m.probe_openvpn_process = MagicMock(return_value=True)
        m.probe_vpn_interface = MagicMock(return_value=True)
        m.probe_default_route = MagicMock(return_value=True)
        m.probe_killswitch = MagicMock(return_value="inactive")
        m.get_external_ip = MagicMock(return_value="5.6.7.8")
        m.is_qbittorrent_running = MagicMock(return_value=True)
        m.stop_qbittorrent = MagicMock()
        m._stop_event.wait = MagicMock()
        with patch("subprocess.run"):
            m._run()
        assert m.probe_killswitch.call_count == 1
        m.stop_qbittorrent.assert_called()


# --------------------------------------------- server-side torrent start gate

class TestTorrentStartGate:
    """torrent_start_blocked() is the single choke point every path to starting
    the torrent client goes through. The UI also disables its button, but that
    is a client-side hint — curl, a stale tab, or a VPN drop between status
    polls all still reach the endpoint."""

    def _ready(self, m):
        m.status["running"] = True
        m.check_openvpn_process = MagicMock(return_value=True)
        m.check_vpn_interface = MagicMock(return_value=True)
        m.check_default_route = MagicMock(return_value=True)
        m.check_killswitch_active = MagicMock(return_value=True)
        m.get_external_ip = MagicMock(return_value="5.6.7.8")
        return m

    def test_allows_when_everything_is_up(self):
        m = self._ready(make_monitor(home_ip="1.2.3.4"))
        assert m.torrent_start_blocked() is None

    def test_blocks_when_monitor_is_not_running(self):
        """Starting the VPN no longer starts the monitor, so this is the only
        thing stopping a torrent from running with nothing watching for leaks."""
        m = self._ready(make_monitor(home_ip="1.2.3.4"))
        m.status["running"] = False
        assert m.torrent_start_blocked() is not None

    def test_blocks_when_openvpn_is_down(self):
        m = self._ready(make_monitor(home_ip="1.2.3.4"))
        m.check_openvpn_process = MagicMock(return_value=False)
        assert m.torrent_start_blocked() is not None

    def test_blocks_when_interface_is_down(self):
        m = self._ready(make_monitor(home_ip="1.2.3.4"))
        m.check_vpn_interface = MagicMock(return_value=False)
        assert m.torrent_start_blocked() is not None

    def test_blocks_when_traffic_bypasses_tunnel(self):
        m = self._ready(make_monitor(home_ip="1.2.3.4"))
        m.check_default_route = MagicMock(return_value=False)
        assert m.torrent_start_blocked() is not None

    def test_blocks_when_kill_switch_is_inactive(self):
        m = self._ready(make_monitor(home_ip="1.2.3.4"))
        m.check_killswitch_active = MagicMock(return_value=False)
        assert m.torrent_start_blocked() is not None

    def test_blocks_when_external_ip_is_unknown(self):
        m = self._ready(make_monitor(home_ip="1.2.3.4"))
        m.get_external_ip = MagicMock(return_value=None)
        assert m.torrent_start_blocked() is not None

    def test_blocks_when_external_ip_is_the_home_ip(self):
        m = self._ready(make_monitor(home_ip="1.2.3.4"))
        m.get_external_ip = MagicMock(return_value="1.2.3.4")
        assert m.torrent_start_blocked() is not None

    def test_start_qbittorrent_refuses_when_blocked(self):
        """Regression: the client must not launch on a bypassed tunnel."""
        m = self._ready(make_monitor(home_ip="1.2.3.4"))
        m.check_default_route = MagicMock(return_value=False)
        m.is_qbittorrent_running = MagicMock(return_value=False)
        m.apply_qbittorrent_config = MagicMock()
        with patch("subprocess.Popen") as popen:
            assert m.start_qbittorrent() is False
        popen.assert_not_called()
        m.apply_qbittorrent_config.assert_not_called()


# --------------------------------------------- monitor loop — fast-tier probes

class TestFastTierTolerance:
    """A fast-tier check that could not run says nothing about the tunnel.

    Regression for 2026-08-14 11:32:46: the monitor logged "VPN interface down"
    and tore everything down while OpenVPN's own log shows tun0 up continuously
    from 11:31:42 until our SIGTERM at 11:32:56. `ip link show tun0` had simply
    not answered inside its timeout under torrent load.
    """

    def _monitor(self, **probes):
        m = make_monitor(home_ip="1.2.3.4", ip_interval=9999)
        m.probe_openvpn_process = MagicMock(return_value=probes.get("proc", True))
        m.probe_vpn_interface = MagicMock(return_value=probes.get("iface", True))
        m.probe_default_route = MagicMock(return_value=probes.get("route", True))
        m.check_ipv6_leak = MagicMock(return_value=False)
        m.probe_killswitch = MagicMock(return_value="active")
        m.get_external_ip = MagicMock(return_value="5.6.7.8")
        m.is_qbittorrent_running = MagicMock(return_value=True)
        m.stop_qbittorrent = MagicMock(return_value=True)
        return m

    def test_single_unanswered_interface_check_does_not_stop(self):
        m = self._monitor()
        m.probe_vpn_interface = MagicMock(side_effect=[None, True, True, True])
        calls = {"n": 0}
        def wait(*a, **k):
            calls["n"] += 1
            if calls["n"] >= 3:
                m._stop_event.set()
        m._stop_event.wait = wait
        with patch("subprocess.run"):
            m._run()
        m.stop_qbittorrent.assert_not_called()

    def test_stops_after_three_unanswered_checks(self):
        m = self._monitor(iface=None)
        m._stop_event.wait = MagicMock()
        with patch("subprocess.run"):
            m._run()
        m.stop_qbittorrent.assert_called()
        assert m.status["secure"] is False

    def test_a_definite_interface_failure_still_trips_at_once(self):
        """The tolerance must not soften a real tunnel drop."""
        m = self._monitor(iface=False)
        m._stop_event.wait = MagicMock()
        with patch("subprocess.run"):
            m._run()
        assert m.probe_vpn_interface.call_count == 1
        m.stop_qbittorrent.assert_called()

    def test_unknown_streak_resets_after_a_good_check(self):
        m = self._monitor()
        m.probe_vpn_interface = MagicMock(
            side_effect=[None, None, True, None, None, True, True])
        calls = {"n": 0}
        def wait(*a, **k):
            calls["n"] += 1
            if calls["n"] >= 6:
                m._stop_event.set()
        m._stop_event.wait = wait
        with patch("subprocess.run"):
            m._run()
        m.stop_qbittorrent.assert_not_called()

    def test_route_is_unknown_when_the_interface_is_unknown(self):
        """Never claim the route is down just because tun0 could not be read."""
        m = self._monitor(iface=None)
        m.probe_default_route = MagicMock()
        m._stop_event.wait = MagicMock()
        with patch("subprocess.run"):
            m._run()
        m.probe_default_route.assert_not_called()


class TestFastProbeTriState:
    def test_timeout_is_unknown_not_down(self):
        import subprocess as _sp
        m = make_monitor()
        with patch("subprocess.run", side_effect=_sp.TimeoutExpired("ip", 5)):
            assert m.probe_vpn_interface() is None
            assert m.probe_openvpn_process() is None
            assert m.probe_default_route() is None

    def test_bool_wrappers_fail_closed_on_unknown(self):
        import subprocess as _sp
        m = make_monitor()
        with patch("subprocess.run", side_effect=_sp.TimeoutExpired("ip", 5)):
            assert m.check_vpn_interface() is False
            assert m.check_openvpn_process() is False
            assert m.check_default_route() is False

    def test_missing_interface_is_a_definite_false(self):
        m = make_monitor()
        with patch("subprocess.run", return_value=_proc(1)):
            assert m.probe_vpn_interface() is False


# ------------------------------------------------ qBittorrent bind via WebUI API

class TestApplyTunnelBind:
    """qBittorrent 4.2.5 ignores Session\\Interface / InterfaceAddress / Port
    when they are written to qBittorrent.conf — verified directly: it keeps the
    keys in the file, reports an empty interface, picks a random listen port and
    listens on every address. The bind has to go through the WebUI API."""

    def _monitor(self):
        m = make_monitor()
        m._qbt_webui_port = MagicMock(return_value=8080)
        m._qbt_peer_port = MagicMock(return_value=19806)
        return m

    def _session(self, prefs):
        s = MagicMock()
        post = MagicMock(); post.status_code = 200
        s.post.return_value = post
        get = MagicMock(); get.json.return_value = prefs
        s.get.return_value = get
        return s

    def test_applies_and_confirms(self):
        m = self._monitor()
        s = self._session({"current_network_interface": "tun0",
                           "current_interface_address": "10.211.1.29",
                           "listen_port": 19806})
        with patch("qbt_config.detect_tun0_ip", return_value="10.211.1.29"), \
             patch.object(VPNMonitor, "_qbt_api", return_value=(s, "http://127.0.0.1:8080")):
            assert m.apply_tunnel_bind() is True
        sent = s.post.call_args.kwargs["data"]["json"]
        assert "10.211.1.29" in sent and "tun0" in sent

    def test_fails_when_readback_shows_no_bind(self):
        """A 200 means accepted, not applied — that is precisely how the
        config-file approach failed silently."""
        m = self._monitor()
        s = self._session({"current_network_interface": "",
                           "current_interface_address": "",
                           "listen_port": 48688})
        with patch("qbt_config.detect_tun0_ip", return_value="10.211.1.29"), \
             patch.object(VPNMonitor, "_qbt_api", return_value=(s, "http://127.0.0.1:8080")):
            assert m.apply_tunnel_bind() is False

    def test_fails_when_bound_to_the_wrong_address(self):
        m = self._monitor()
        s = self._session({"current_network_interface": "eth0",
                           "current_interface_address": "10.0.0.66",
                           "listen_port": 19806})
        with patch("qbt_config.detect_tun0_ip", return_value="10.211.1.29"), \
             patch.object(VPNMonitor, "_qbt_api", return_value=(s, "http://127.0.0.1:8080")):
            assert m.apply_tunnel_bind() is False

    def test_fails_when_tun0_has_no_address(self):
        m = self._monitor()
        with patch("qbt_config.detect_tun0_ip", return_value=None):
            assert m.apply_tunnel_bind() is False

    def test_fails_when_the_webui_is_unreachable(self):
        m = self._monitor()
        with patch("qbt_config.detect_tun0_ip", return_value="10.211.1.29"), \
             patch.object(VPNMonitor, "_qbt_api", return_value=(None, "http://127.0.0.1:8080")), \
             patch("time.sleep"), patch("time.monotonic", side_effect=[0, 0, 999, 999]):
            assert m.apply_tunnel_bind() is False

    def test_start_stops_the_client_when_the_bind_cannot_be_applied(self):
        m = make_monitor()
        m.is_qbittorrent_running = MagicMock(return_value=False)
        m.torrent_start_blocked = MagicMock(return_value=None)
        m.apply_qbittorrent_config = MagicMock()
        m.apply_tunnel_bind = MagicMock(return_value=False)
        m.stop_qbittorrent = MagicMock(return_value=True)
        proc = MagicMock(); proc.poll.return_value = None; proc.pid = 1
        with patch("subprocess.Popen", return_value=proc), patch("builtins.open"), \
             patch("time.sleep"):
            assert m.start_qbittorrent() is False
        m.stop_qbittorrent.assert_called()


class TestSsScopeParsing:
    def test_scope_suffix_is_stripped(self):
        """ss prints '10.211.1.29%tun0' for an interface-bound socket; matching
        the raw string reported a correctly bound client as unbound."""
        m = make_monitor()
        r = _proc(0)
        r.stdout = ('LISTEN 0 30 10.211.1.29%tun0:19806 0.0.0.0:* '
                    'users:(("qbittorrent-nox",pid=1,fd=16))')
        with patch("qbt_config.detect_tun0_ip", return_value="10.211.1.29"), \
             patch.object(VPNMonitor, "_qbt_peer_port", return_value=19806), \
             patch("subprocess.run", return_value=r):
            assert m.verify_tunnel_bind() == "bound"
