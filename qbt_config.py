#!/usr/bin/env python3
"""Apply the project's qBittorrent settings to ~/.config/qBittorrent/qBittorrent.conf.

Both start paths (`checkip.sh` and `webapp/monitor.py`) call this so the two
implementations cannot drift on what qBittorrent is actually told to do.

The settings this file owns, and nothing else:

  Session\\Interface          tun0      } written for a future qBittorrent that
  Session\\InterfaceName      tun0      } honours them; 4.2.5 does NOT - see below
  Session\\InterfaceAddress   the live tun0 IP
  WebUI\\LocalHostAuth        false - lets the monitor apply the real bind over
                              the local API without storing a password
  Session\\DefaultSavePath    QBT_SAVE_PATH
  Session\\QueueingSystemEnabled / Session\\MaxActiveDownloads /
  Session\\MaxActiveTorrents  the concurrent-download limit

Everything else in the file is left exactly as qBittorrent wrote it. This is a
merge, not a copy - qBittorrent rewrites its whole config on exit, so anything
set from the Web UI (credentials, categories, speed limits) lives only in that
file, and overwriting it with the repo template silently reset it on every
start.

**The interface keys do not bind anything on qBittorrent 4.2.5.** Tested
directly: the client keeps them in the file, then reports
`current_network_interface = ''`, picks a random listen port and listens on
every address. `apply_tunnel_bind()` in `webapp/monitor.py` applies the real
bind through the WebUI API after startup and reads it back to confirm. They are
still written here so a later qBittorrent that does honour them gets the right
values, but writing them is not the same as having bound anything.

The keys above are the qBittorrent 4.x names (`BitTorrent/Session/*`). The
`[Preferences] Queueing\\*` and `Downloads\\SavePath` entries still present in
older config files are pre-4.0 leftovers that qBittorrent migrated once and no
longer reads; they are deliberately not touched.

Usage:
    python3 qbt_config.py [--save-path DIR] [--max-active N] [--tun0-ip IP]
                          [--config PATH] [--template PATH]

Prints one status line per applied group to stdout; warnings to stderr.
Exit status is 0 unless the file could not be written.
"""

import argparse
import os
import re
import shutil
import sys

_VPN_DIR = os.path.dirname(os.path.abspath(__file__))

DEFAULT_CONFIG = os.path.expanduser("~/.config/qBittorrent/qBittorrent.conf")
DEFAULT_TEMPLATE = os.path.join(_VPN_DIR, "qBittorrent.conf")


def read_ini(path):
    """Parse an ini file into [(section, [raw lines])], preserving order and text.

    qBittorrent values contain backslashes and @Variant(...) blobs that
    configparser mangles on round-trip, so keys are edited as raw lines.
    """
    sections = []
    current = (None, [])
    try:
        with open(path) as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        return sections
    for line in lines:
        if line.startswith("[") and line.rstrip().endswith("]"):
            if current[0] is not None or current[1]:
                sections.append(current)
            current = (line.rstrip(), [])
        else:
            current[1].append(line)
    if current[0] is not None or current[1]:
        sections.append(current)
    return sections


def write_ini(path, sections):
    out = []
    for name, lines in sections:
        if name is not None:
            out.append(name)
        out.extend(lines)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("\n".join(out).rstrip("\n") + "\n")


def get_key(sections, section, key):
    for name, lines in sections:
        if name != f"[{section}]":
            continue
        for line in lines:
            if line.startswith(f"{key}="):
                return line[len(key) + 1:]
    return None


def set_key(sections, section, key, value):
    """Set section/key to value, creating either if absent."""
    header = f"[{section}]"
    for name, lines in sections:
        if name != header:
            continue
        for i, line in enumerate(lines):
            if line.startswith(f"{key}="):
                lines[i] = f"{key}={value}"
                return
        # Insert after the last key line so trailing blank lines stay at the end
        pos = len(lines)
        while pos > 0 and not lines[pos - 1].strip():
            pos -= 1
        lines.insert(pos, f"{key}={value}")
        return
    sections.append((header, [f"{key}={value}"]))


def del_key(sections, section, key):
    for name, lines in sections:
        if name == f"[{section}]":
            lines[:] = [ln for ln in lines if not ln.startswith(f"{key}=")]


def int_key(sections, section, key, default):
    raw = get_key(sections, section, key)
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def apply_config(config=DEFAULT_CONFIG, template=DEFAULT_TEMPLATE,
                 save_path=None, max_active=None, tun0_ip=None):
    """Apply the owned settings. Returns (status_lines, warning_lines)."""
    status, warnings = [], []

    # qBittorrent reads this file once at startup and rewrites all of it on
    # exit, so anything written underneath a running instance is both ignored
    # now and thrown away later. Say so rather than reporting a false success.
    if qbittorrent_running():
        warnings.append("qBittorrent is already running - it will not pick these settings up, "
                        "and will overwrite them when it exits. Stop it and start it again.")

    if not os.path.exists(config):
        # First run: seed from the repo template rather than qBittorrent's
        # defaults, so the tun0 binding is in place before it ever starts.
        if os.path.exists(template):
            os.makedirs(os.path.dirname(config), exist_ok=True)
            shutil.copy2(template, config)
            status.append(f"seeded {config} from repo template")
        else:
            warnings.append(f"no qBittorrent config at {config} and no template to seed from")

    sections = read_ini(config)

    # --- bind to the tunnel ------------------------------------------------
    set_key(sections, "BitTorrent", r"Session\Interface", "tun0")
    set_key(sections, "BitTorrent", r"Session\InterfaceName", "tun0")
    if tun0_ip:
        set_key(sections, "BitTorrent", r"Session\InterfaceAddress", tun0_ip)
        status.append(f"bound to tun0 ({tun0_ip})")
    else:
        # A stale address would keep qBittorrent bound to an IP the tunnel no
        # longer has. Drop it and fall back to the name bind.
        del_key(sections, "BitTorrent", r"Session\InterfaceAddress")
        warnings.append("tun0 IP unavailable - bound by interface name only")

    # --- local API access --------------------------------------------------
    # The keys above do not actually bind anything. Verified on qBittorrent
    # 4.2.5: Session\Interface, Session\InterfaceAddress and Session\Port are
    # preserved in this file but never applied — the client reports an empty
    # interface, picks a random listen port and listens on every address. The
    # real bind is applied over the WebUI API by apply_tunnel_bind() in
    # webapp/monitor.py, and this is what lets it do that without storing a
    # password: requests from 127.0.0.1 skip authentication.
    #
    # The trade-off is deliberate and worth knowing: any local process or local
    # user can then drive qBittorrent's API without credentials. Set
    # QBT_WEBUI_USER / QBT_WEBUI_PASS in vpn_config.conf instead if that
    # matters on this box — apply_tunnel_bind() prefers the localhost path but
    # falls back to logging in.
    set_key(sections, "Preferences", r"WebUI\LocalHostAuth", "false")
    status.append("local WebUI API access enabled (for the tunnel bind)")

    # --- download location -------------------------------------------------
    if save_path:
        try:
            os.makedirs(save_path, exist_ok=True)
        except OSError as e:
            warnings.append(f"could not create save path {save_path} - {e}")
        set_key(sections, "BitTorrent", r"Session\DefaultSavePath", save_path)
        status.append(f"save path: {save_path}")

    # --- concurrent downloads ----------------------------------------------
    # Only touched when QBT_MAX_ACTIVE_DOWNLOADS says so; with no configured
    # value qBittorrent's own queueing settings are left exactly as they are.
    if max_active is not None:
        set_key(sections, "BitTorrent", r"Session\QueueingSystemEnabled", "true")
        set_key(sections, "BitTorrent", r"Session\MaxActiveDownloads", str(max_active))
        # MaxActiveTorrents caps downloads *and* seeds together, so it has to
        # leave room for both or the download limit is never reached. Seeding
        # slots are qBittorrent's business — read whatever it has rather than
        # imposing a number, and only raise the cap if it is too low.
        uploads = int_key(sections, "BitTorrent", r"Session\MaxActiveUploads", 0)
        torrents = int_key(sections, "BitTorrent", r"Session\MaxActiveTorrents", 0)
        if torrents < max_active + uploads:
            set_key(sections, "BitTorrent", r"Session\MaxActiveTorrents",
                    str(max_active + uploads))
        status.append(f"max active downloads: {max_active}")

    try:
        write_ini(config, sections)
    except OSError as e:
        warnings.append(f"could not write {config} - {e}")
        return status, warnings

    return status, warnings


def read_shell_config(key, default=""):
    """Read KEY="value" from ~/.vpn_config.conf or ./vpn_config.conf, in that order."""
    for path in (os.path.expanduser("~/.vpn_config.conf"),
                 os.path.join(_VPN_DIR, "vpn_config.conf")):
        if not os.path.isfile(path):
            continue
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith(f"{key}="):
                        return line[len(key) + 1:].strip().strip('"').strip("'")
        except OSError:
            pass
        return default
    return default


def configured_max_active():
    """QBT_MAX_ACTIVE_DOWNLOADS from vpn_config.conf, or None if unset/invalid.

    None means "leave qBittorrent's queueing alone" — the limit lives in the
    config file, not in this code.
    """
    raw = read_shell_config("QBT_MAX_ACTIVE_DOWNLOADS", "")
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def qbittorrent_running():
    import subprocess
    try:
        return subprocess.run(["pgrep", "-f", "qbittorrent-nox"],
                              capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def detect_tun0_ip():
    import subprocess
    try:
        out = subprocess.run(["ip", "-4", "addr", "show", "tun0"],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", out)
    return m.group(1) if m else None


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--save-path", default=None,
                   help="download location (default: QBT_SAVE_PATH from vpn_config.conf)")
    p.add_argument("--max-active", type=int, default=None,
                   help="concurrent downloads (default: QBT_MAX_ACTIVE_DOWNLOADS from "
                        "vpn_config.conf; unset leaves qBittorrent's queueing alone)")
    p.add_argument("--tun0-ip", default=None,
                   help="tun0 address to bind to (default: read from `ip addr`)")
    p.add_argument("--config", default=DEFAULT_CONFIG)
    p.add_argument("--template", default=DEFAULT_TEMPLATE)
    args = p.parse_args(argv)

    save_path = args.save_path if args.save_path is not None else read_shell_config("QBT_SAVE_PATH")
    max_active = args.max_active if args.max_active is not None else configured_max_active()
    tun0_ip = args.tun0_ip if args.tun0_ip is not None else detect_tun0_ip()

    status, warnings = apply_config(config=args.config, template=args.template,
                                    save_path=save_path, max_active=max_active,
                                    tun0_ip=tun0_ip)
    for line in status:
        print(line)
    for line in warnings:
        print(line, file=sys.stderr)
    return 1 if any("could not write" in w for w in warnings) else 0


if __name__ == "__main__":
    sys.exit(main())
