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
