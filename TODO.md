# TODO

Open items, known gaps, and things deliberately left alone. What is *already
implemented* is described in `ENHANCEMENTS.md`, not here.

> **Testing status of the `fix/vpn-leak-paths` branch:** the Python suite passes
> on the Pi (79 tests). The shell changes are syntax-clean but have had only
> limited end-to-end testing. Exercise `stopvpn.sh`, `stop_web.sh`, and
> `remove_killswitch.sh` on real hardware before trusting them in anger.

---

## Open — security relevant

### 1. Only the first `remote` line is whitelisted, resolved once

`ufw_killswitch.sh`. A config with several `remote` lines, or a hostname behind
a rotating pool, gets one pinned IP. If the provider hands OpenVPN a different
address the connection is denied until the kill switch is re-applied.

### 2. DNS narrowing (`ufw_killswitch.sh:94-95`)

Plaintext DNS to any server is still allowed out over the physical NIC, so the
ISP still sees every tracker hostname. Commit `a6c29db` shows narrowing this
broke systemd-resolved once already. Needs testing against the pre-tunnel
window, not a blind edit.

### 3. Leak detection rests on a dynamic IP

Both paths only test `external_ip != home_ip`. If the ISP rotates your address
while the tunnel is up and the tunnel then drops, neither monitor notices.
Fixing it properly means asserting the exit IP *matches the VPN server* rather
than *differs from home* — a design decision, not a patch.

### 4. `/api/files/scan` takes an arbitrary `dir`

`webapp/app.py:311`. Low risk on a LAN-only box with no inbound access, and it
only reads, but it is an unbounded filesystem read for anyone who can reach the
port. Bounding it to a configured root would cost little.

---

## Open — `checkip.sh` gaps

These all live in the shell monitor. The web monitor has the equivalent fixes;
`checkip.sh` was left alone because it is the flow `startvpn.sh` drives and you
asked not to change `startvpn.sh`.

- **`checkip.sh:59-73` checks the kill switch only at startup.** A UFW reset
  from another terminal goes unnoticed while it carries on reporting healthy.
  The web monitor re-checks on the IP-check cadence.
- **`checkip.sh:101` traps `EXIT` only.** An untrapped `SIGTERM` skips
  `_exit_handler`, leaving qBittorrent running. Add `trap _exit_handler EXIT
  TERM INT`.
- **No `torrent_start_blocked()` equivalent.** Startup verification is inline
  and correct, but there is no single gate the way the web path has one.

---

## Open — housekeeping

- **Dead config keys.** `vpn_config.conf` ships `SETUP_KILLSWITCH`,
  `PREVENT_DNS_LEAK`, `DISABLE_IPV6`, `BIND_TO_VPN_INTERFACE`,
  `DEFAULT_VIDEO_DEST`, and `VPN_HOME`, none of which are read by any script.
  `SETUP_KILLSWITCH=false` and its comment about an "iptables-based killswitch"
  are worse than dead — they describe the opposite of current behaviour. Delete
  them from the file.
- **`BACKUP_DIR` is honoured inconsistently.** `vpn_config.conf` sets
  `$HOME/.vpn_backups`; `stopvpn.sh:23` defaults to `/tmp/vpn_backups` if unset;
  `webapp/monitor.py:28` and `stop_web.sh:29` hardcode `~/.vpn_backups` and
  ignore the config entirely. Pick one source of truth.
- **`qBittorrent.conf` tracked in the repo root** is a seed config, which reads
  as confusing. Move it to `examples/` or document it in place. It is now only
  used on a first run, when no `~/.config/qBittorrent/qBittorrent.conf` exists;
  after that `qbt_config.py` merges into the live file.
- **Stale `checkvpn.log` in the repo root.** Nothing writes it anymore — session
  logs go to `vpn_logs/`. Delete it and add it to `.gitignore`.
- **No `shellcheck` in CI** on a mostly-bash project. No coverage at all for
  `checkip.sh`, `ufw_killswitch.sh`, or `ufw_base.sh`.
- **No lockfile.** `pip freeze > requirements-lock.txt` for reproducible
  installs.
- **No API documentation** for the 21 endpoints in `webapp/app.py`.

---

## Path drift (item 10, partly closed)

Closed: stop paths now match, IPv4 validation now matches, all three config
selections now take the newest `.ovpn` by mtime, and the whole qBittorrent
config step is now *shared* rather than mirrored - both paths call
`qbt_config.py`, so the tun0 bind, save path and queue limit cannot drift.

Still differing:

- The web path disables IPv6 and rewrites `/etc/resolv.conf`; the shell path
  only *checks* IPv6 and leaves DNS alone.
- The web path fail-stops OpenVPN too; `checkip.sh` leaves it running.

---

## Deliberately not doing

- **Network-namespace migration.** Reverted at your request. Not reattempted.
- **A systemd unit for auto-start on boot.** This tool is meant to be
  started and stopped manually.
- **Auto-reconnect in the monitors.** Fail-stop is the design. See `CLAUDE.md`.
- **Removing `--script-security 0`.** Commit `4c165a2` claimed it breaks the
  tunnel; the confirmed-working tree still contains it, so the theory does not
  hold. On OpenVPN 2.6 interface setup goes through netlink, not external `ip`
  calls.

---

## Resolved on this branch

Kept for context on why the code looks the way it does.

- **The qBittorrent config was installed by copying the repo template over
  `~/.config/qBittorrent/qBittorrent.conf`**, then patching it with `sed` /
  `re.sub`. That wiped everything qBittorrent owns (WebUI credentials,
  categories, speed limits) on every single start, since the client rewrites
  that whole file when it exits. Replaced by `qbt_config.py`, which merges only
  the keys this project owns and leaves the rest byte-for-byte.
- **No concurrent-download limit existed, and could not be set.** The template
  shipped `Session\QueueingSystemEnabled=false`, which disables qBittorrent's
  queue outright, so no max-active value could take effect. Now driven by
  `QBT_MAX_ACTIVE_DOWNLOADS`, which also raises `Session\MaxActiveTorrents` -
  that cap counts seeds as well, so leaving it at the default would keep the
  download limit permanently out of reach.
- **`stop_vpn()` tore down the kill switch without confirming anything had
  stopped.** It fired `pkill -f openvpn` and logged "OpenVPN stopped"
  unconditionally, then reset UFW. A qBittorrent that survived SIGTERM would
  find the ISP link wide open. Both processes are now polled to exit with a
  SIGKILL escalation, and teardown is skipped entirely (kill switch left up,
  CRITICAL logged) if qBittorrent is still alive.
- **`stop_web.sh` removed the kill switch but left torrents running.** It did
  `pkill -f webapp/app.py` then `ufw_base.sh`. Neither qBittorrent nor OpenVPN
  was stopped, and since qBittorrent was a child of Flask, killing Flask
  *orphaned* it. End state: kill switch off, torrents running, no monitor. It
  also skipped `restore_dns()`/`restore_ipv6()`, leaving `/etc/resolv.conf`
  pinned and immutable. Rewritten as an ordered teardown mirroring `stopvpn.sh`,
  independent of the API so it works even if Flask is wedged.
- **The kill switch opened a fail-open window every time it was applied.**
  `ufw_base.sh` did `reset` → `default allow outgoing` → `enable`, and only then
  flipped to `deny`. UFW was live with outgoing unrestricted in between, on
  every application. Now `UFW_OUT_POLICY` is applied *before* `enable`; worst
  case is a brief outage rather than brief exposure.
- **`remove_killswitch.sh` was dead code.** Entirely iptables-based, restoring a
  backup nothing had created since the UFW rewrite. Rewritten for UFW, stops the
  torrent client first, `--disable` forces the last-resort path.
- **The documented sudoers template could not run the kill switch.** It granted
  three *iptables* entries and no `ufw` entry. Replaced with `/usr/sbin/ufw` and
  the two `bash ufw_*.sh` entries, plus a verification snippet.
- **`detect_external_ip()` failed open on an HTTP error body.** No
  `raise_for_status()`, so a 502 HTML page became "the external IP", never
  matched the home IP, and the leak check passed permanently. Now validated with
  `ipaddress.IPv4Address()` in both `webapp/monitor.py` and `vpn_active.py` —
  which also fixes dual-stack `api64.ipify.org` returning an IPv6 address that
  always read as "secure".
- **The qBittorrent gate was client-side only.** `POST /api/qbt/start` had no
  checks at all. Now `torrent_start_blocked()` is the single gate every path
  goes through, returning 409 with the reason.
- **`_openvpn_start()` reported success when `tun0` merely existed.** Now
  requires the default route on `tun0` and an exit IP that differs from home.
- **`start_vpn()` chained VPN → monitor → qBittorrent.** Now brings up the
  tunnel and stops there; steps 3 and 4 are deliberate clicks. The safety
  chaining provided moved into `torrent_start_blocked()`'s monitor-running
  check.
- **`checkip.sh` started qBittorrent on an IP-check *error*.** Startup now
  aborts on an unverifiable IP rather than proceeding.
- **The kill switch was verified once and never rechecked** (web monitor only).
- **`ufw_killswitch.sh` picked the `.ovpn` alphabetically** (`ls | head -1`)
  while `startvpn.sh` and `monitor.py` both picked the newest by mtime. With
  more than one config present the firewall whitelisted one server's endpoint
  while OpenVPN dialled another. Now `ls -t`.
- **`vpn_status.sh`** — deleted. Nothing called it, and it grepped
  `iptables -L OUTPUT`, which never matches under UFW.
- **Torrent port mismatch** — `qBittorrent.conf` now uses `19806`, matching
  `ufw_base.sh`.
- **LAN hardcoded to `10.0.0.0/24`** — now all of RFC1918, overridable via
  `LAN_CIDRS`.
- **`_fetch_pinned` honoured `HTTPS_PROXY`**, and a proxy resolves the hostname
  itself, bypassing the DNS pin. Now `proxies={"http": None, "https": None}`,
  capped at 1 MB, returning `bytes`.
- **`download_ovpn` accepted any payload.** Now rejects one with no `remote`
  line, so an error page saved as `.ovpn` fails at download instead of as a
  tunnel that never comes up.
- **`QBT_SAVE_PATH` could not be edited once set** — `startvpn.sh` now prefills
  the current value with `read -e -i`.
- **The retry loop burned all `MAX_STARTUP_ATTEMPTS` with no way to bail.**
  `startvpn.sh` now asks "Retry? [y/N]"; the web app gained a cancellable wait,
  `/api/vpn/cancel-retry`, and a Cancel Retry button.
