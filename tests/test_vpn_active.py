"""Tests for vpn_active.py — process/interface checks and IP leak detection."""
from unittest.mock import MagicMock, patch

import vpn_active


# ------------------------------------------------------------------ helpers

def _proc(returncode=0, stdout=""):
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    return m


def _link(state="UP"):
    """A plausible 'ip -o link show tun0' line."""
    return _proc(0, f"9: tun0: <POINTOPOINT,MULTICAST,NOARP,UP,LOWER_UP> mtu 1500 state {state} mode DEFAULT")


def _response(json_body=None, text="", status=200):
    m = MagicMock()
    m.status_code = status
    m.text = text
    m.json.return_value = json_body or {}
    m.raise_for_status = MagicMock()
    return m


def _vpn_up():
    """Patches for "the tunnel is genuinely up" — process, interface, routing."""
    return (
        patch("vpn_active.check_openvpn_running", return_value=True),
        patch("vpn_active.check_vpn_interface", return_value=True),
        patch("vpn_active.check_routing", return_value=True),
    )


# ------------------------------------------------------------------ check_openvpn_running

class TestCheckOpenvpnRunning:
    def test_returns_true_when_process_found(self):
        with patch("subprocess.run", return_value=_proc(0)):
            assert vpn_active.check_openvpn_running() is True

    def test_returns_false_when_process_missing(self):
        with patch("subprocess.run", return_value=_proc(1)):
            assert vpn_active.check_openvpn_running() is False

    def test_returns_false_on_exception(self):
        with patch("subprocess.run", side_effect=OSError("no pgrep")):
            assert vpn_active.check_openvpn_running() is False


# ------------------------------------------------------------------ check_vpn_interface

class TestCheckVpnInterface:
    def test_returns_true_when_tun0_up(self):
        with patch("subprocess.run", return_value=_link("UP")):
            assert vpn_active.check_vpn_interface() is True

    def test_returns_false_when_tun0_missing(self):
        with patch("subprocess.run", return_value=_proc(1)):
            assert vpn_active.check_vpn_interface() is False

    def test_returns_false_when_tun0_exists_but_is_down(self):
        # 'ip link show tun0' exits 0 for a DOWN device, so a tun0 left behind
        # by --persist-tun or a SIGKILLed openvpn must not read as protected.
        down = _proc(0, "9: tun0: <POINTOPOINT,NOARP> mtu 1500 state DOWN mode DEFAULT")
        with patch("subprocess.run", return_value=down):
            assert vpn_active.check_vpn_interface() is False

    def test_returns_false_on_exception(self):
        with patch("subprocess.run", side_effect=OSError):
            assert vpn_active.check_vpn_interface() is False


# ------------------------------------------------------------------ check_routing

class TestCheckRouting:
    def test_returns_true_when_route_uses_tun0(self):
        out = _proc(0, "8.8.8.8 via 10.8.0.1 dev tun0 src 10.8.0.6")
        with patch("subprocess.run", return_value=out):
            assert vpn_active.check_routing() is True

    def test_returns_false_when_route_bypasses_tunnel(self):
        out = _proc(0, "8.8.8.8 via 192.168.1.1 dev eth0 src 192.168.1.50")
        with patch("subprocess.run", return_value=out):
            assert vpn_active.check_routing() is False

    def test_returns_false_on_exception(self):
        with patch("subprocess.run", side_effect=OSError):
            assert vpn_active.check_routing() is False


# ------------------------------------------------------------------ get_external_ip

class TestGetExternalIp:
    def test_returns_ip_from_first_service(self):
        resp = _response({"ip": "1.2.3.4"})
        with patch("requests.get", return_value=resp):
            assert vpn_active.get_external_ip() == "1.2.3.4"

    def test_falls_through_to_second_service_on_failure(self):
        # The fallback is httpbin, which reports under "origin" not "ip".
        good = _response({"origin": "5.6.7.8"})
        with patch("requests.get", side_effect=[Exception("timeout"), good]):
            assert vpn_active.get_external_ip() == "5.6.7.8"

    def test_returns_none_when_all_services_fail(self):
        with patch("requests.get", side_effect=Exception("timeout")):
            assert vpn_active.get_external_ip() is None

    def test_skips_service_returning_empty_ip_key(self):
        # A service that responds 200 but with the wrong JSON shape should not
        # return "" — it must fall through so the next service is tried.
        empty_resp = _response({"wrong_key": "ignored"})
        good_resp = _response({"origin": "5.6.7.8"})
        with patch("requests.get", side_effect=[empty_resp, good_resp]):
            assert vpn_active.get_external_ip() == "5.6.7.8"

    def test_empty_ip_does_not_report_secure(self):
        # Regression: "" must not slip past the None check in main() and
        # falsely report the VPN as secure.
        empty_resp = _response({"wrong_key": "ignored"})
        proc, iface, route = _vpn_up()
        with patch("requests.get", return_value=empty_resp):
            with proc, iface, route:
                assert vpn_active.main("1.2.3.4") == 2  # error, not secure

    def test_strips_proxy_comma_from_httpbin(self):
        # ipify fails; httpbin (the only fallback) returns comma-separated IPs
        httpbin_resp = _response({"origin": " 9.10.11.12 , 203.0.113.5"})
        with patch("requests.get", side_effect=[Exception("timeout"), httpbin_resp]):
            assert vpn_active.get_external_ip() == "9.10.11.12"

    def test_http_error_response_is_not_treated_as_an_ip(self):
        # Fail-open regression: an error body must not become "the external IP".
        # It would never equal home_ip, so every leak check would read "secure".
        bad = _response(text="<html>502 Bad Gateway</html>")
        bad.raise_for_status.side_effect = Exception("502 Server Error")
        good = _response({"origin": "9.10.11.12"})
        with patch("requests.get", side_effect=[bad, good]):
            assert vpn_active.get_external_ip() == "9.10.11.12"

    def test_non_ipv4_response_is_rejected(self):
        # A dual-stack service answering with IPv6, or an HTML body that slipped
        # through, can never equal the IPv4 home IP — so it must not be trusted.
        junk = _response({"ip": "2001:db8::1"})
        good = _response({"origin": "9.10.11.12"})
        with patch("requests.get", side_effect=[junk, good]):
            assert vpn_active.get_external_ip() == "9.10.11.12"

    def test_returns_none_when_every_service_returns_junk(self):
        junk = _response({"ip": "not-an-ip"})
        with patch("requests.get", return_value=junk):
            assert vpn_active.get_external_ip() is None


# ------------------------------------------------------------------ main

class TestMain:
    """Exit-code contract: 0 = secure, 1 = confirmed leak, 2 = undetermined."""

    def test_secure_when_vpn_up_and_ip_differs(self):
        proc, iface, route = _vpn_up()
        with proc, iface, route, patch("vpn_active.get_external_ip", return_value="5.6.7.8"):
            assert vpn_active.main("1.2.3.4") == 0

    def test_leak_when_openvpn_not_running(self):
        with patch("vpn_active.check_openvpn_running", return_value=False):
            assert vpn_active.main("1.2.3.4") == 1

    def test_leak_when_tun0_missing(self):
        with (
            patch("vpn_active.check_openvpn_running", return_value=True),
            patch("vpn_active.check_vpn_interface", return_value=False),
        ):
            assert vpn_active.main("1.2.3.4") == 1

    def test_leak_when_traffic_bypasses_the_tunnel(self):
        # openvpn is up and tun0 exists, but the kernel routes around it.
        with (
            patch("vpn_active.check_openvpn_running", return_value=True),
            patch("vpn_active.check_vpn_interface", return_value=True),
            patch("vpn_active.check_routing", return_value=False),
        ):
            assert vpn_active.main("1.2.3.4") == 1

    def test_leak_when_external_ip_matches_home_ip(self):
        proc, iface, route = _vpn_up()
        with proc, iface, route, patch("vpn_active.get_external_ip", return_value="1.2.3.4"):
            assert vpn_active.main("1.2.3.4") == 1

    def test_error_when_ip_service_unreachable(self):
        proc, iface, route = _vpn_up()
        with proc, iface, route, patch("vpn_active.get_external_ip", return_value=None):
            assert vpn_active.main("1.2.3.4") == 2

    def test_leak_detection_ignores_whitespace(self):
        proc, iface, route = _vpn_up()
        with proc, iface, route, patch("vpn_active.get_external_ip", return_value=" 1.2.3.4 "):
            assert vpn_active.main(" 1.2.3.4") == 1

    def test_malformed_home_ip_is_an_error_not_secure(self):
        # A truncated or non-IP argument can never equal the external IP, so
        # without validation every check would report "secure" forever.
        for bad in ("", "1.2.3.", "192.168.1", "myhost.dyndns.org", "not-an-ip"):
            assert vpn_active.main(bad) == 2, f"{bad!r} should be undetermined"

    def test_junk_external_ip_is_an_error_not_secure(self):
        proc, iface, route = _vpn_up()
        with proc, iface, route, patch("vpn_active.get_external_ip", return_value="<html>oops</html>"):
            assert vpn_active.main("1.2.3.4") == 2
