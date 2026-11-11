"""Tests for the server-side start/stop ordering gate in webapp/app.py.

The UI disables buttons that would break the order, but that is a hint and not
a control — curl, a stale tab, or a state change between 3s status polls all
still reach these endpoints. These tests cover the enforcement, not the hint.
"""
from unittest.mock import MagicMock, patch

import pytest

import app as webapp


def _monitor(vpn=False, monitor_running=False, qbt=False, starting=False):
    m = MagicMock()
    m.status = {"vpn_starting": starting, "running": monitor_running}
    m.check_openvpn_process.return_value = vpn
    m.is_qbittorrent_running.return_value = qbt
    return m


def _violation(step, **state):
    with patch.object(webapp, "monitor", _monitor(**state)):
        return webapp._ordering_violation(step)


class TestStartOrdering:
    def test_vpn_start_refused_when_vpn_already_running(self):
        assert _violation("vpn_start", vpn=True) is not None

    def test_vpn_start_refused_while_still_starting(self):
        assert _violation("vpn_start", starting=True) is not None

    def test_vpn_start_allowed_when_nothing_is_up(self):
        assert _violation("vpn_start") is None

    def test_vpn_start_refused_while_qbittorrent_runs(self):
        """Starting the VPN reapplies the kill switch, and `ufw --force reset`
        disables UFW until it is re-enabled — a live client would egress on
        the ISP link. Reachable after a fail-stop left the client up."""
        assert _violation("vpn_start", qbt=True) is not None


class TestStopOrdering:
    """'If I start qbit, then I cannot stop VPN or monitor.'"""

    def test_vpn_stop_refused_while_qbittorrent_runs(self):
        assert _violation("vpn_stop", vpn=True, monitor_running=True, qbt=True) is not None

    def test_vpn_stop_refused_while_monitor_runs(self):
        assert _violation("vpn_stop", vpn=True, monitor_running=True) is not None

    def test_vpn_stop_allowed_once_the_layers_above_are_down(self):
        assert _violation("vpn_stop", vpn=True) is None

    def test_monitor_stop_refused_while_qbittorrent_runs(self):
        assert _violation("monitor_stop", vpn=True, monitor_running=True, qbt=True) is not None

    def test_monitor_stop_allowed_when_qbittorrent_is_down(self):
        assert _violation("monitor_stop", vpn=True, monitor_running=True) is None

    def test_reconnect_refused_while_qbittorrent_runs(self):
        """Reconnect reapplies the kill switch, which briefly restores the
        default allow-outgoing policy."""
        assert _violation("reconnect", vpn=True, qbt=True) is not None

    def test_reconnect_allowed_with_no_client(self):
        assert _violation("reconnect", vpn=True) is None


class TestEndpointsReturn409:
    """The gate must surface as a real HTTP error, not a silent no-op."""

    @pytest.fixture
    def client(self):
        webapp.app.config["TESTING"] = True
        return webapp.app.test_client()

    def test_vpn_stop_returns_409_when_out_of_order(self, client):
        with patch.object(webapp, "monitor",
                          _monitor(vpn=True, monitor_running=True, qbt=True)), \
             patch.object(webapp, "_auth", return_value=None), \
             patch.object(webapp, "_require_monitor", return_value=None):
            r = client.post("/api/vpn/stop")
        assert r.status_code == 409
        assert "qBittorrent" in r.get_json()["error"]

    def test_configure_returns_409_while_qbittorrent_runs(self, client):
        """Configure replaces (and stops) the monitor — never under a live client."""
        m = _monitor(vpn=True, monitor_running=True, qbt=True)
        with patch.object(webapp, "monitor", m), \
             patch.object(webapp, "_auth", return_value=None):
            r = client.post("/api/configure", json={"home_ip": "203.0.113.7"})
        assert r.status_code == 409
        m.stop.assert_not_called()


class TestStartInProgress:
    """While Start VPN is running its attempt/reset loop, anything that would
    race it is refused. Cancel & Reset is the way out of that state."""

    def test_stop_all_refused_while_starting(self):
        """The loop's next attempt would re-apply the kill switch and relaunch
        OpenVPN behind a Stop All that ran alongside it."""
        assert "Cancel" in _violation("stop_all", starting=True)

    def test_stop_all_allowed_otherwise(self):
        assert _violation("stop_all", vpn=True, monitor_running=True, qbt=True) is None

    def test_reconnect_refused_while_starting(self):
        assert _violation("reconnect", starting=True) is not None

    def test_config_change_refused_while_starting(self):
        """Installing a config deletes the old .ovpn while an attempt may be
        choosing one."""
        assert _violation("config_change", starting=True) is not None

    def test_config_change_allowed_after_a_reset(self):
        assert _violation("config_change") is None

    def test_configure_refused_while_starting(self):
        assert _violation("configure", starting=True) is not None

    def test_configure_keeps_the_qbittorrent_rule(self):
        assert _violation("configure", vpn=True, monitor_running=True, qbt=True) is not None

    def test_configure_allowed_when_idle(self):
        assert _violation("configure", vpn=True, monitor_running=True) is None


class TestStartInProgressEndpoints:
    @pytest.fixture
    def client(self):
        webapp.app.config["TESTING"] = True
        return webapp.app.test_client()

    def _post(self, client, m, url, **kw):
        with patch.object(webapp, "monitor", m), \
             patch.object(webapp, "_auth", return_value=None), \
             patch.object(webapp, "_require_monitor", return_value=None):
            return client.post(url, **kw)

    def test_stop_all_returns_409_while_starting(self, client):
        m = _monitor(vpn=True, starting=True)
        with patch.object(webapp.threading, "Thread") as thread:
            r = self._post(client, m, "/api/stop-all")
        assert r.status_code == 409
        thread.assert_not_called()

    def test_download_returns_409_while_starting(self, client):
        m = _monitor(starting=True)
        r = self._post(client, m, "/api/vpn/download-config",
                       json={"url": "https://example.com/a.ovpn"})
        assert r.status_code == 409
        m.download_ovpn.assert_not_called()

    def test_upload_returns_409_while_starting(self, client):
        import io
        m = _monitor(starting=True)
        r = self._post(client, m, "/api/vpn/upload-config",
                       data={"file": (io.BytesIO(b"remote x 1194\n"), "a.ovpn")},
                       content_type="multipart/form-data")
        assert r.status_code == 409
        m.upload_ovpn.assert_not_called()

    def test_download_allowed_when_idle(self, client):
        m = _monitor()
        r = self._post(client, m, "/api/vpn/download-config",
                       json={"url": "https://example.com/a.ovpn"})
        assert r.status_code == 200
        m.download_ovpn.assert_called_once()

    def test_cancel_returns_409_when_nothing_is_starting(self, client):
        m = _monitor()
        m.cancel_retry.return_value = False
        r = self._post(client, m, "/api/vpn/cancel-retry")
        assert r.status_code == 409

    def test_cancel_accepted_while_starting(self, client):
        m = _monitor(starting=True)
        m.cancel_retry.return_value = True
        r = self._post(client, m, "/api/vpn/cancel-retry")
        assert r.status_code == 200
        m.cancel_retry.assert_called_once()

    def test_configure_returns_409_while_starting(self, client):
        m = _monitor(starting=True)
        with patch.object(webapp, "monitor", m), \
             patch.object(webapp, "_auth", return_value=None):
            r = client.post("/api/configure", json={"home_ip": "203.0.113.7"})
        assert r.status_code == 409
        m.stop.assert_not_called()
