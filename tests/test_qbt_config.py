"""Tests for qbt_config.py - the shared qBittorrent settings applier."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import qbt_config


LIVE = """[AutoRun]
enabled=false

[BitTorrent]
Session\\DefaultSavePath=/old/path
Session\\Interface=eth0
Session\\InterfaceAddress=192.168.1.50
Session\\InterfaceName=eth0
Session\\Port=19806
Session\\QueueingSystemEnabled=false

[Meta]
MigrationVersion=4

[Preferences]
WebUI\\Username=admin
WebUI\\Password_PBKDF2="@ByteArray(secret)"
Downloads\\ScanDirsV2=@Variant(\\0\\0\\0\\x1c\\0\\0\\0\\0)
"""


@pytest.fixture
def conf(tmp_path):
    p = tmp_path / "qBittorrent.conf"
    p.write_text(LIVE)
    return str(p)


def write_conf(tmp_path, extra_lines):
    """A live config with extra [BitTorrent] keys."""
    p = tmp_path / "qBittorrent.conf"
    p.write_text(LIVE.replace("Session\\Port=19806",
                              "Session\\Port=19806\n" + extra_lines))
    return str(p)


def apply(conf, **kw):
    kw.setdefault("save_path", "/mnt/hdddisk")
    kw.setdefault("max_active", 5)
    kw.setdefault("tun0_ip", "10.211.1.99")
    return qbt_config.apply_config(config=conf, **kw)


def keys(conf, section="BitTorrent"):
    for name, lines in qbt_config.read_ini(conf):
        if name == "[%s]" % section:
            return dict(ln.split("=", 1) for ln in lines if "=" in ln)
    return {}


def test_binds_to_tun0_by_name_and_address(conf):
    apply(conf)
    k = keys(conf)
    assert k["Session\\Interface"] == "tun0"
    assert k["Session\\InterfaceName"] == "tun0"
    assert k["Session\\InterfaceAddress"] == "10.211.1.99"


def test_stale_address_is_dropped_when_tun0_is_down(conf):
    _, warnings = apply(conf, tun0_ip=None)
    assert "Session\\InterfaceAddress" not in keys(conf)
    assert any("tun0 IP unavailable" in w for w in warnings)


def test_save_path_is_applied(conf, tmp_path):
    dest = str(tmp_path / "downloads")
    apply(conf, save_path=dest)
    assert keys(conf)["Session\\DefaultSavePath"] == dest
    assert os.path.isdir(dest)


def test_blank_save_path_leaves_existing_one_alone(conf):
    apply(conf, save_path="")
    assert keys(conf)["Session\\DefaultSavePath"] == "/old/path"


def test_max_active_downloads_enables_queueing(conf):
    apply(conf, max_active=5)
    k = keys(conf)
    assert k["Session\\QueueingSystemEnabled"] == "true"
    assert k["Session\\MaxActiveDownloads"] == "5"


def test_total_active_cap_leaves_room_for_seeding(tmp_path):
    """MaxActiveTorrents caps downloads and seeds together, so a cap of 5 with
    3 seeding slots never lets 5 torrents download at once."""
    path = write_conf(tmp_path, "Session\\MaxActiveTorrents=5\nSession\\MaxActiveUploads=3")
    apply(path, max_active=5)
    assert keys(path)["Session\\MaxActiveTorrents"] == "8"


def test_higher_existing_total_cap_is_left_alone(tmp_path):
    path = write_conf(tmp_path, "Session\\MaxActiveTorrents=20\nSession\\MaxActiveUploads=3")
    apply(path, max_active=5)
    assert keys(path)["Session\\MaxActiveTorrents"] == "20"


def test_no_configured_limit_leaves_queueing_untouched(conf):
    apply(conf, max_active=None)
    k = keys(conf)
    assert k["Session\\QueueingSystemEnabled"] == "false"
    assert "Session\\MaxActiveDownloads" not in k


def test_existing_settings_are_preserved(conf):
    """The point of merging: qBittorrent's own settings survive a start."""
    before = open(conf).read()
    apply(conf)
    after = open(conf).read()
    for line in before.splitlines():
        if line.startswith(("WebUI", "enabled=", "MigrationVersion", "Downloads\\ScanDirsV2")):
            assert line in after
    assert "[AutoRun]" in after and "[Preferences]" in after


def test_seeds_from_template_when_no_config_exists(tmp_path):
    dst = str(tmp_path / "nested" / "qBittorrent.conf")
    status, _ = apply(dst)
    assert os.path.exists(dst)
    assert any("seeded" in s for s in status)
    assert keys(dst)["Session\\InterfaceName"] == "tun0"


def test_configured_max_active_reads_the_config_file(monkeypatch):
    """No limit is baked into the code: an unset or nonsense value means
    'leave qBittorrent's queueing alone'."""
    for raw in ("", "0", "-1", "lots"):
        monkeypatch.setattr(qbt_config, "read_shell_config", lambda *a, **k: raw)
        assert qbt_config.configured_max_active() is None
    monkeypatch.setattr(qbt_config, "read_shell_config", lambda *a, **k: "5")
    assert qbt_config.configured_max_active() == 5
