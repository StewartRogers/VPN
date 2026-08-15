"""Tests for vpn_active.py — process/interface checks and IP leak detection."""
from unittest.mock import MagicMock, patch

import vpn_active


# ------------------------------------------------------------------ helpers

def _proc(returncode=0):
    m = MagicMock()
    m.returncode = returncode
    return m


def _response(json_body=None, text="", status=200):
    m = MagicMock()
    m.status_code = status
    m.text = text
    m.json.return_value = json_body or {}
    m.raise_for_status = MagicMock()
    return m


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
        with patch("subprocess.run", return_value=_proc(0)):
            assert vpn_active.check_vpn_interface() is True

    def test_returns_false_when_tun0_missing(self):
        with patch("subprocess.run", return_value=_proc(1)):
            assert vpn_active.check_vpn_interface() is False

    def test_returns_false_on_exception(self):
        with patch("subprocess.run", side_effect=OSError):
            assert vpn_active.check_vpn_interface() is False


# ------------------------------------------------------------------ get_external_ip

class TestGetExternalIp:
    def test_returns_ip_from_first_service(self):
        resp = _response({"ip": "1.2.3.4"})
        with patch("requests.get", return_value=resp):
            assert vpn_active.get_external_ip() == "1.2.3.4"

    def test_falls_through_to_second_service_on_failure(self):
        good = _response({"ip": "5.6.7.8"})
        with patch("requests.get", side_effect=[Exception("timeout"), good]):
            assert vpn_active.get_external_ip() == "5.6.7.8"

    def test_returns_none_when_all_services_fail(self):
        with patch("requests.get", side_effect=Exception("timeout")):
            assert vpn_active.get_external_ip() is None

    def test_skips_service_returning_empty_ip_key(self):
        # A service that responds 200 but with the wrong JSON shape should not
        # return "" — it must fall through so the next service is tried.
        empty_resp = _response({"wrong_key": "ignored"})
        good_resp = _response({"ip": "5.6.7.8"})
        with patch("requests.get", side_effect=[empty_resp, good_resp]):
            assert vpn_active.get_external_ip() == "5.6.7.8"

    def test_empty_ip_does_not_report_secure(self):
        # Regression: "" must not slip past the None check in main() and
        # falsely report the VPN as secure.
        empty_resp = _response({"wrong_key": "ignored"})
        with patch("requests.get", return_value=empty_resp):
            with (
                patch("vpn_active.check_openvpn_running", return_value=True),
                patch("vpn_active.check_vpn_interface", return_value=True),
            ):
                assert vpn_active.main("1.2.3.4") == 2  # error, not secure

    def test_strips_proxy_comma_from_httpbin(self):
        # First two services (ipify, icanhazip) fail; httpbin returns comma-separated IPs
        httpbin_resp = _response({"origin": " 9.10.11.12 , 203.0.113.5"})
        with patch("requests.get", side_effect=[Exception("timeout"), Exception("timeout"), httpbin_resp]):
            assert vpn_active.get_external_ip() == "9.10.11.12"


# ------------------------------------------------------------------ main

class TestMain:
    def test_secure_when_vpn_up_and_ip_differs(self):
        with (
            patch("vpn_active.check_openvpn_running", return_value=True),
            patch("vpn_active.check_vpn_interface", return_value=True),
            patch("vpn_active.get_external_ip", return_value="5.6.7.8"),
        ):
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

    def test_leak_when_external_ip_matches_home_ip(self):
        with (
            patch("vpn_active.check_openvpn_running", return_value=True),
            patch("vpn_active.check_vpn_interface", return_value=True),
            patch("vpn_active.get_external_ip", return_value="1.2.3.4"),
        ):
            assert vpn_active.main("1.2.3.4") == 1

    def test_error_when_ip_service_unreachable(self):
        with (
            patch("vpn_active.check_openvpn_running", return_value=True),
            patch("vpn_active.check_vpn_interface", return_value=True),
            patch("vpn_active.get_external_ip", return_value=None),
        ):
            assert vpn_active.main("1.2.3.4") == 2

    def test_leak_detection_ignores_whitespace(self):
        with (
            patch("vpn_active.check_openvpn_running", return_value=True),
            patch("vpn_active.check_vpn_interface", return_value=True),
            patch("vpn_active.get_external_ip", return_value=" 1.2.3.4 "),
        ):
            assert vpn_active.main(" 1.2.3.4") == 1


# ------------------------------------------------------------------ resolver_addresses

LAN = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]


class TestResolverAddresses:
    def test_parses_nameserver_lines(self, tmp_path):
        conf = tmp_path / "resolv.conf"
        conf.write_text(
            "# Generated by NetworkManager\n"
            "nameserver 8.8.8.8\n"
            "nameserver 1.1.1.1\n"
            "search lan\n"
        )
        assert vpn_active.resolver_addresses(str(conf)) == ["8.8.8.8", "1.1.1.1"]

    def test_missing_file_is_not_fatal(self, tmp_path):
        assert vpn_active.resolver_addresses(str(tmp_path / "nope")) == []


# ------------------------------------------------------------------ check_resolvers

class TestCheckResolvers:
    def test_isp_resolvers_are_flagged(self):
        # The RPI5 case: Shaw's resolvers stop answering once queries reach
        # them from the VPN's exit IP.
        with patch("vpn_active.resolver_addresses",
                   return_value=["64.59.144.100", "64.59.150.143"]):
            assert vpn_active.check_resolvers(LAN) == ["64.59.144.100", "64.59.150.143"]

    def test_known_public_resolvers_are_not_flagged(self):
        with patch("vpn_active.resolver_addresses", return_value=["8.8.8.8", "1.1.1.1"]):
            assert vpn_active.check_resolvers(LAN) == []

    def test_lan_resolver_is_not_flagged(self):
        # The wired Pi: the router is inside LAN_CIDRS, so ufw_killswitch.sh
        # lets it out on the physical interface and it never enters the tunnel.
        with patch("vpn_active.resolver_addresses", return_value=["10.0.0.1"]):
            assert vpn_active.check_resolvers(LAN) == []

    def test_flags_only_the_off_lan_one(self):
        with patch("vpn_active.resolver_addresses",
                   return_value=["10.0.0.1", "64.59.144.100"]):
            assert vpn_active.check_resolvers(LAN) == ["64.59.144.100"]

    def test_narrower_lan_cidrs_are_honoured(self):
        with patch("vpn_active.resolver_addresses", return_value=["10.0.0.1"]):
            assert vpn_active.check_resolvers(["192.168.1.0/24"]) == ["10.0.0.1"]

    def test_malformed_entries_are_skipped(self):
        with patch("vpn_active.resolver_addresses", return_value=["not-an-ip", "8.8.8.8"]):
            assert vpn_active.check_resolvers(LAN) == []

    def test_malformed_cidr_does_not_crash(self):
        with patch("vpn_active.resolver_addresses", return_value=["10.0.0.1"]):
            assert vpn_active.check_resolvers(["garbage"]) == ["10.0.0.1"]


# ------------------------------------------------------------------ diagnose_ip_failure

class TestDiagnoseIpFailure:
    def test_dns_dead_but_tunnel_alive_names_dns(self):
        with (
            patch("vpn_active._resolves", return_value=False),
            patch("vpn_active._reachable", return_value=True),
            patch("vpn_active.resolver_addresses", return_value=["64.59.144.100"]),
        ):
            msg = vpn_active.diagnose_ip_failure()
        assert "DNS resolution failed" in msg
        assert "64.59.144.100" in msg

    def test_dns_fine_but_no_route_names_connectivity(self):
        with (
            patch("vpn_active._resolves", return_value=True),
            patch("vpn_active._reachable", return_value=False),
        ):
            assert "no connectivity" in vpn_active.diagnose_ip_failure()

    def test_both_dead_says_so(self):
        with (
            patch("vpn_active._resolves", return_value=False),
            patch("vpn_active._reachable", return_value=False),
        ):
            assert "nothing is leaving the tunnel" in vpn_active.diagnose_ip_failure()

    def test_both_healthy_blames_the_services(self):
        with (
            patch("vpn_active._resolves", return_value=True),
            patch("vpn_active._reachable", return_value=True),
        ):
            assert "did not answer" in vpn_active.diagnose_ip_failure()

    def test_hung_lookup_is_bounded(self):
        # getaddrinfo ignores socket timeouts; _resolves must not inherit that.
        import time

        def _hang(*_a, **_kw):
            time.sleep(30)

        with patch("socket.getaddrinfo", side_effect=_hang):
            started = time.monotonic()
            assert vpn_active._resolves("example.invalid", 0.5) is False
            assert time.monotonic() - started < 5
