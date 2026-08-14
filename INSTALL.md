# Installation Guide

## Requirements

- Debian/Ubuntu-based Linux (developed on Raspberry Pi OS)
- `sudo` access
- An OpenVPN provider that issues `.ovpn` config files

Packages: `openvpn`, `qbittorrent-nox`, `ufw`, `python3` (3.9 or newer),
`python3-pip`, `curl`. The web app additionally needs `flask` and `requests`.

There is no `iptables` dependency — the firewall is UFW exclusively.

## 1. Clone and make executable

```bash
git clone https://github.com/StewartRogers/VPN.git
cd VPN
chmod +x *.sh
```

## 2. Install dependencies

`startvpn.sh` can do it for you — answer `y` at the
`Check software installation?` prompt on first run. It installs and updates
`qbittorrent-nox`, `openvpn`, `ufw`, `python3`, `python3-pip`, and
`python3-requests`.

Or do it by hand:

```bash
sudo apt-get update
sudo apt-get install -y openvpn qbittorrent-nox ufw python3 python3-pip \
                        python3-requests curl
pip3 install -r webapp/requirements.txt      # web app only
```

## 3. First qBittorrent run

Run it once on its own to accept the legal disclaimer and note the generated
admin password:

```bash
qbittorrent-nox
# accept the disclaimer, note the temporary password, Ctrl+C to stop
```

Then set a permanent password through the WebUI at `http://localhost:8080`.

This project manages `~/.config/qBittorrent/qBittorrent.conf` at start time
through `qbt_config.py`, which both start paths call. It owns exactly four
things: the `tun0` bind (by name and live IP), `QBT_SAVE_PATH`, the
`QBT_MAX_ACTIVE_DOWNLOADS` queue limit, and the listen port 19806 that matches
the UFW rules. Everything else in that file is left alone — it is a merge, not
an overwrite, so WebUI credentials, categories and speed limits survive. The
tracked `qBittorrent.conf` in the repo is only a seed for a first run, when no
config exists yet.

qBittorrent reads this file **once at startup** and rewrites all of it on exit,
so settings only take effect on the next start — and editing it underneath a
running client achieves nothing. Stop qBittorrent before changing them.

## 4. VPN configuration file

Get an `.ovpn` file from your provider. Any of these work:

- Let `startvpn.sh` download it — answer `y` to `Download a new OVPN file?` and
  paste the URL
- Pass `--ovpn-url https://…` on the command line
- Drop the file in the project directory and let the script install it
- Place it in `/etc/openvpn/client/` yourself

Both paths select the **newest** `.ovpn` in `/etc/openvpn/client/` by mtime.
Downloads must be HTTPS and are validated: private, loopback and reserved
addresses are rejected, the resolved IP is pinned for the fetch, and a payload
with no `remote` line is refused.

## 5. Configuration file (optional)

`~/.vpn_config.conf` takes precedence over `./vpn_config.conf`. See the
Configuration section of [README.md](README.md) for the keys that are actually
read; anything else in the file is inert.

## 6. sudo requirements (web app only)

The CLI path prompts for sudo as needed. The web app cannot, so the user
running it needs passwordless sudo for these operations. Create
`/etc/sudoers.d/vpn-webapp` with `sudo visudo -f /etc/sudoers.d/vpn-webapp`,
substituting your username and the path where you checked this repo out:

```
# OpenVPN and process control
pi ALL=(ALL) NOPASSWD: /usr/sbin/openvpn
pi ALL=(ALL) NOPASSWD: /usr/bin/pkill
pi ALL=(ALL) NOPASSWD: /bin/mv
pi ALL=(ALL) NOPASSWD: /bin/rm
pi ALL=(ALL) NOPASSWD: /bin/chmod
pi ALL=(ALL) NOPASSWD: /bin/chown
pi ALL=(ALL) NOPASSWD: /bin/cat /var/log/openvpn.log
pi ALL=(ALL) NOPASSWD: /bin/cat /etc/openvpn/client/*.ovpn

# Kill switch. Both entries are required:
#   ufw           - check_killswitch_active() runs 'sudo ufw status verbose'
#   bash <script> - setup_killswitch()/teardown_killswitch() run the ufw_*.sh
#                   scripts, which must be root to change firewall rules
pi ALL=(ALL) NOPASSWD: /usr/sbin/ufw
pi ALL=(ALL) NOPASSWD: /bin/bash /home/pi/VPN/ufw_base.sh
pi ALL=(ALL) NOPASSWD: /bin/bash /home/pi/VPN/ufw_killswitch.sh

# IPv6 disable/restore
pi ALL=(ALL) NOPASSWD: /sbin/sysctl

# DNS leak prevention
pi ALL=(ALL) NOPASSWD: /usr/bin/tee /etc/resolv.conf
pi ALL=(ALL) NOPASSWD: /usr/bin/chattr
pi ALL=(ALL) NOPASSWD: /bin/cp /etc/resolv.conf *
```

Without the `ufw` and `bash` entries the web app cannot apply the kill switch:
`setup_killswitch()` fails, `check_killswitch_active()` returns `False`, and the
monitor refuses to start.

Verify:

```bash
sudo -n ufw status verbose >/dev/null && echo "ufw: ok" || echo "ufw: MISSING"
sudo -n bash /home/pi/VPN/ufw_base.sh --help >/dev/null 2>&1 \
  && echo "scripts: ok" || echo "scripts: check the bash paths above"
```

## Running it

### CLI

```bash
./startvpn.sh
```

The interactive flow: optional software check → qBittorrent save path →
download or select a config → apply the kill switch → start OpenVPN → confirm
the tunnel → hand off to `checkip.sh`, which verifies and then starts
qBittorrent.

Non-interactive:

```bash
./startvpn.sh --non-interactive --ovpn-url https://example.com/config.ovpn
```

| Flag | Effect |
| --- | --- |
| `--non-interactive` | Run without prompts |
| `--ovpn-url URL` | Download the config from URL |
| `--no-killswitch` | Skip the kill switch — **the monitor will not start** |
| `--help` | Usage |

Stop with `./stopvpn.sh`, which prompts to shut down services and then to run
the file organiser. `./stopvpn.sh --shutdown-only` does the teardown with no
prompts at all.

### Web app

```bash
./start_web.sh
# open http://<pi-ip>:5000
```

Configure it with environment variables or `webapp/.env` — see the Web app
section of [README.md](README.md). Stop it with `bash stop_web.sh`, which runs
the same ordered teardown as `stopvpn.sh`; do **not** just kill the Flask
process, or qBittorrent is orphaned with the firewall left open.

## Verifying

```bash
sudo ufw status verbose        # kill switch: look for "deny (outgoing)"
ip route get 8.8.8.8           # should say "dev tun0"
pgrep -x openvpn && pgrep -f qbittorrent-nox
curl -s https://api.ipify.org  # should NOT be your home IP

tail -f vpn_logs/latest.log    # monitor session log
```

The web app's status panel shows all of this live.

**IPv6 note:** every "outgoing is blocked" guarantee above assumes UFW is
managing IPv6 as well as IPv4. `ufw_base.sh` checks `/etc/default/ufw` on every
run and corrects `IPV6=no` (or a missing `IPV6=` line) to `IPV6=yes`
automatically. A firewall with `IPV6=no` silently ignores all IPv6 traffic,
which is also why IPv6 is disabled at the kernel level before OpenVPN starts.

## Recovery

If the kill switch locks you out and the web app is unreachable:

```bash
./remove_killswitch.sh             # restore UFW base state (normal recovery)
./remove_killswitch.sh --disable   # last resort: disable UFW entirely
```

It stops the torrent client first, then restores the base firewall state, DNS,
and IPv6. See [TROUBLESHOOTING.md](TROUBLESHOOTING.md) for more.

## Uninstalling

```bash
./stopvpn.sh --shutdown-only
sudo apt-get remove openvpn qbittorrent-nox      # optional
rm -rf ~/.vpn_config.conf ~/.vpn_backups ./vpn_logs /tmp/vpn_pids
```

Confirm the firewall is back to normal afterwards with `sudo ufw status verbose` —
outgoing should read `allow`.

## Support

- GitHub Issues: https://github.com/StewartRogers/VPN/issues
- [TROUBLESHOOTING.md](TROUBLESHOOTING.md) · [ENHANCEMENTS.md](ENHANCEMENTS.md)
