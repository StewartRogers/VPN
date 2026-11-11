"""Tests for webapp/torrent_cleanup.py — auto-removal of finished torrents."""
from unittest.mock import MagicMock, patch

import pytest

import torrent_cleanup as tc
from torrent_cleanup import TorrentCleaner

BASE = "http://127.0.0.1:8080"


# ------------------------------------------------------------------ helpers

def _resp(status=200, payload=None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload if payload is not None else []
    return r


def _torrent(name, h, state="uploading", progress=1.0):
    return {"name": name, "hash": h, "state": state, "progress": progress}


def _session(torrents, delete_status=200):
    s = MagicMock()
    s.get.return_value = _resp(200, torrents)
    s.post.return_value = _resp(delete_status)
    return s


def _settings(enabled="true", interval="30"):
    values = {"QBT_AUTO_REMOVE": enabled, "QBT_AUTO_REMOVE_INTERVAL": interval}
    return patch.object(tc, "read_config_value",
                        side_effect=lambda key, default="": values.get(key, default))


def _cleaner(session=None, running=True):
    log = MagicMock()
    api = MagicMock(return_value=(session, BASE))
    c = TorrentCleaner(log, api=api, is_running=MagicMock(return_value=running))
    return c, log, api


def _messages(log):
    return [c.args[0] for c in log.call_args_list]


def _warnings(log):
    return [c for c in log.call_args_list if c.kwargs.get("level") == "WARNING"]


# ------------------------------------------------------------------ removal

class TestRemoval:
    def test_completed_torrents_removed_one_per_call(self):
        s = _session([_torrent("Debian ISO", "aaa"), _torrent("Other", "bbb")])
        c, log, _ = _cleaner(s)
        with _settings():
            assert c.run_once() == 2
        s.get.assert_called_once()
        assert s.get.call_args.kwargs["params"] == {"filter": "completed"}
        assert s.post.call_count == 2
        assert [p.kwargs["data"]["hashes"] for p in s.post.call_args_list] == ["aaa", "bbb"]
        msgs = _messages(log)
        assert "Removed torrent (files kept): Debian ISO" in msgs
        assert "Removed torrent (files kept): Other" in msgs

    def test_every_delete_carries_delete_files_false(self):
        s = _session([_torrent("a", "1"), _torrent("b", "2"), _torrent("c", "3")])
        c, _, _ = _cleaner(s)
        with _settings():
            c.run_once()
        for call in s.post.call_args_list:
            assert call.args[0] == f"{BASE}/api/v2/torrents/delete"
            assert call.kwargs["data"]["deleteFiles"] == "false"

    @pytest.mark.parametrize("state", ["moving", "checkingUP", "checkingResumeData"])
    def test_busy_states_skipped(self, state):
        s = _session([_torrent("busy", "aaa", state=state), _torrent("done", "bbb")])
        c, log, _ = _cleaner(s)
        with _settings():
            assert c.run_once() == 1
        assert [p.kwargs["data"]["hashes"] for p in s.post.call_args_list] == ["bbb"]
        assert not any("busy" in m for m in _messages(log))

    def test_partial_progress_never_removed(self):
        s = _session([_torrent("half", "aaa", progress=0.5)])
        c, _, _ = _cleaner(s)
        with _settings():
            assert c.run_once() == 0
        s.post.assert_not_called()

    def test_entry_without_hash_skipped(self):
        s = _session([{"name": "nohash", "state": "uploading", "progress": 1}])
        c, _, _ = _cleaner(s)
        with _settings():
            assert c.run_once() == 0
        s.post.assert_not_called()

    def test_control_characters_stripped_from_name(self):
        s = _session([_torrent("evil\nname\x1b[31m", "aaa")])
        c, log, _ = _cleaner(s)
        with _settings():
            c.run_once()
        msg = _messages(log)[-1]
        assert "\n" not in msg and "\x1b" not in msg

    def test_nothing_completed_is_quiet(self):
        s = _session([])
        c, log, _ = _cleaner(s)
        with _settings():
            c.run_once()                  # first cycle logs the "is on" line
            log.reset_mock()
            c.run_once()
        log.assert_not_called()

    def test_failed_delete_not_logged_as_removed(self):
        s = _session([_torrent("stuck", "aaa")], delete_status=500)
        c, log, _ = _cleaner(s)
        with _settings():
            assert c.run_once() == 0
        assert not any(m.startswith("Removed torrent") for m in _messages(log))


# ------------------------------------------------------------------ disabled / off

class TestDisabled:
    @pytest.mark.parametrize("value", ["false", "", "no", "maybe"])
    def test_disabled_makes_no_api_calls(self, value):
        c, _, api = _cleaner(_session([_torrent("a", "1")]))
        with _settings(enabled=value):
            assert c.run_once() == 0
        api.assert_not_called()
        c._is_running.assert_not_called()

    @pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes", "on"])
    def test_enabled_values(self, value):
        with _settings(enabled=value):
            assert tc.auto_remove_enabled() is True

    def test_qbittorrent_not_running_is_silent(self):
        c, log, api = _cleaner(None, running=False)
        with _settings():
            c.run_once()
            log.reset_mock()
            for _ in range(5):
                c.run_once()
        api.assert_not_called()
        log.assert_not_called()

    def test_unknown_probe_still_tries_the_api(self):
        s = _session([_torrent("a", "1")])
        c, _, api = _cleaner(s, running=None)
        with _settings():
            assert c.run_once() == 1
        api.assert_called_once()

    def test_toggle_logged_once_each_way(self):
        c, log, _ = _cleaner(_session([]))
        with _settings(enabled="true"):
            c.run_once(); c.run_once()
        with _settings(enabled="false"):
            c.run_once(); c.run_once()
        msgs = _messages(log)
        assert sum("is on" in m for m in msgs) == 1
        assert sum("is off" in m for m in msgs) == 1

    def test_starting_disabled_logs_nothing(self):
        c, log, _ = _cleaner(_session([]))
        with _settings(enabled="false"):
            c.run_once()
        log.assert_not_called()


# ------------------------------------------------------------------ outage

class TestOutage:
    def test_outage_logs_one_warning_not_one_per_cycle(self):
        c, log, _ = _cleaner(None)        # API unreachable / login fails
        with _settings():
            for _ in range(10):
                c.run_once()
        assert len(_warnings(log)) == 1

    def test_single_failure_after_launch_not_reported(self):
        c, log, api = _cleaner(None)
        with _settings():
            c.run_once()                  # WebUI still starting
            api.return_value = (_session([]), BASE)
            c.run_once()
        assert _warnings(log) == []
        assert not any("reachable again" in m for m in _messages(log))

    def test_recovery_logged_once(self):
        c, log, api = _cleaner(None)
        with _settings():
            c.run_once(); c.run_once()
            api.return_value = (_session([]), BASE)
            c.run_once(); c.run_once()
        assert sum("reachable again" in m for m in _messages(log)) == 1

    def test_qbittorrent_stopping_clears_outage_silently(self):
        c, log, api = _cleaner(None)
        with _settings():
            c.run_once(); c.run_once()    # outage reported
            c._is_running.return_value = False
            c.run_once()
            c._is_running.return_value = True
            api.return_value = (_session([]), BASE)
            c.run_once()
        assert not any("reachable again" in m for m in _messages(log))

    def test_http_error_on_list_counts_as_outage(self):
        s = MagicMock()
        s.get.return_value = _resp(403)
        c, log, _ = _cleaner(s)
        with _settings():
            for _ in range(4):
                c.run_once()
        assert len(_warnings(log)) == 1
        s.post.assert_not_called()

    def test_exceptions_never_escape(self):
        s = MagicMock()
        s.get.side_effect = RuntimeError("boom")
        c, _, api = _cleaner(s)
        with _settings():
            assert c.run_once() == 0
        api.side_effect = RuntimeError("worse")
        with _settings():
            assert c.run_once() == 0


# ------------------------------------------------------------------ interval

class TestInterval:
    @pytest.mark.parametrize("raw,expected", [
        ("30", 30), ("60", 60), ("5", 5), ("4", 5), ("0", 5), ("-10", 5),
        ("", tc.DEFAULT_INTERVAL), ("abc", tc.DEFAULT_INTERVAL),
        ("1.5", tc.DEFAULT_INTERVAL), (" 45 ", 45),
    ])
    def test_parsing_and_clamping(self, raw, expected):
        with _settings(interval=raw):
            assert tc.auto_remove_interval() == expected


# ------------------------------------------------------------------ thread

class TestThread:
    def test_start_twice_runs_one_thread(self):
        c, _, _ = _cleaner(None, running=False)
        with _settings(enabled="false"):
            assert c.start() is True
            assert c.start() is False
            c.stop(timeout=2)
        assert not c._thread.is_alive()
