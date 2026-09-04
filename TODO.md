# TODO

Open items, known gaps, and things deliberately left alone. What is *already
implemented* is described in `ENHANCEMENTS.md`, not here.

> **Testing status of the `full-repo-review-2026-09-04` branch:** the Python
> suite passes on the Pi (224 tests) and every shell script is `bash -n` clean.
> The shell changes have had **no** end-to-end testing. This branch adds a
> confirmation gate ahead of the firewall step in `stopvpn.sh`, `stop_web.sh`,
> `remove_killswitch.sh` and `startvpn.sh`'s Ctrl+C handler, and makes
> `ufw_killswitch.sh` / `ufw_base.sh` able to fail — so a teardown can now
> legitimately **stop early and leave the kill switch up**, which is the
> intended behaviour but will look like a hang if you are not expecting it.
> Exercise all four teardown paths, plus a Ctrl+C during a live session, on real
> hardware before trusting them in anger.

---

## Open — from the 2026-09-04 full review

Found by a five-agent review of the whole tree. The six critical and seven
high-severity findings were fixed on `full-repo-review-2026-09-04` (see
"Resolved" below). These are the medium and low ones, left deliberately so the
branch stays testable. Nothing here is a leak on its own.

**Web app**

- **`/` serves the home IP and save path unauthenticated** (`webapp/app.py:65`).
  `index()` is the one route with no `_auth()`, and it interpolates both into
  the HTML. The home IP is the value the entire leak check is built on. Fix:
  render placeholders and let the already-authenticated `/api/status` fill them.
- **A non-ASCII token returns 500, not 401** (`webapp/app.py:41` and `:52`).
  `secrets.compare_digest` raises `TypeError` on non-ASCII `str`, and Werkzeug
  decodes headers as latin-1. Unauthenticated and trivially triggerable; the
  500-vs-401 difference also confirms a token is configured. Compare bytes.
- **SSRF deny-list omits CGNAT `100.64.0.0/10`** in the web path's
  `_fetch_pinned`. Use `if not ip.is_global` rather than the explicit list.
  (The bash path already uses `is_global`.)
- **`.ovpn` URLs are logged verbatim** (`webapp/app.py:146`), including any
  credential in the query string, into the in-memory log the SSE stream serves.
  Log scheme + host + path only.
- **`_fetch_pinned` captures `real_getaddrinfo` outside `_dns_pin_lock`**
  (`webapp/monitor.py`). Two concurrent downloads can leave
  `socket.getaddrinfo` permanently patched, pinning one hostname process-wide.
- **`check_ipv6_leak()` fails open** (`webapp/monitor.py`). It is the one
  fast-tier check that still does `except: return False`, and it never checks
  `returncode`. Make it tri-state and fold into `consecutive_fast_unknown`.
- **`torrent_start_blocked()` gates on a cached kill-switch result** up to
  `KILLSWITCH_CACHE_TTL` (5s) old. Use `force=True` for gating decisions and
  keep the cache for `/api/status` display.
- **Job-record writes race.** `_jobs_lock` covers only payload construction;
  the copy thread and request handlers share one `.tmp` name and can truncate
  each other, and `_load_jobs()` swallows the resulting `ValueError` and drops
  *all* job records — which locks step 4 permanently. Hold the lock through
  `os.replace`, or use `tempfile.mkstemp` in the same directory.

**Organizer / config**

- **`move_file()` does not fsync the destination directory** before unlinking
  the source. Power loss on an SD card can leave neither copy.
- **Cross-job overwrite race**: the `os.path.exists(dst)` check and the
  `os.rename`/`open(dst,"wb")` are not atomic across concurrent move jobs.
- **`qbt_config.py` lowers `MaxActiveTorrents`** — absent keys read as 0, so
  `QBT_MAX_ACTIVE_DOWNLOADS=2` writes 2 over qBittorrent's implicit default of
  5. Use 5/3 as the `int_key` defaults. Contradicts the never-lowered rule in
  `CLAUDE.md`.
- **`qbt_config.py` reads without `encoding="utf-8"`.** Under `LC_ALL=C` a
  config with non-ASCII category names raises `UnicodeDecodeError`, which
  `read_ini`'s `FileNotFoundError`-only handler does not catch. (The *write*
  side is now utf-8 and atomic.)
- **`qbt_config.py` `return default` sits inside the config-search loop**, so a
  minimal `~/.vpn_config.conf` shadows every key in `./vpn_config.conf` and
  `QBT_SAVE_PATH` silently becomes `""`.
- **`vpn_active.py` fails open on an empty home IP** — `main("")` never matches
  and reports secure. Validate with `ipaddress.IPv4Address` and exit 2.
- **`CLAUDE.md` documents `vpn_active.py`'s exit codes inverted** (the code
  returns 0 for secure).

**Shell**

- **`ufw_killswitch.sh` sources a group-writable config as root** (`:16-20`).
  Under `sudo`, `HOME=/root`, so the documented `~/.vpn_config.conf` precedence
  is silently ignored *and* it falls through to a `664 pi:pi` file. Under the
  restricted sudoers in INSTALL.md that is arbitrary root code execution.
  Resolve via `SUDO_USER`'s home as `stop_web.sh:24-28` does, and parse
  `KEY=value` rather than `source`.
- **`VPN_API_TOKEN` is placed in the process argument list** by
  `start_web.sh:190` (`exec env VPN_API_TOKEN=… …`), so it is readable in
  `/proc/<pid>/cmdline` and any `ps` output. `export` it and `exec` instead —
  `/proc/<pid>/environ` is owner-only.
- **`stopvpn.sh` `pkill -f "$service"` is a bare substring** — `vim checkip.sh`
  or `less qbittorrent.conf` are killed as root.
- **`stopvpn.sh` PID handling is unquoted and unvalidated** — a truncated PID
  file word-splits into several `kill` targets, and a literal `0` signals the
  whole process group.
- **`startvpn.sh` assumes `VPN_CLIENT_HOME` ends in `/`** (`:528`), so the
  `chmod 600`/`chown` on the downloaded `.ovpn` silently miss and it is left
  world-readable at curl's default 0644.
- **`checkip.sh:86` pipes `ls -t` into `xargs rm -f`** — word-splits on a
  `LOG_DIR` containing a space.
- **`PID_DIR` defaults to `/tmp/vpn_pids`**, a predictable path another local
  user can pre-create.

**Path drift (add to the list below)**

- **The CLI path never applies or verifies the tunnel bind.**
  `checkip.sh:255-272` calls `qbt_config.py` only, and the config file provably
  cannot bind the client — `apply_tunnel_bind()`/`verify_tunnel_bind()` are
  web-path only. In the CLI flow the kill switch is the sole layer.

**Infrastructure**

- **CI (`.github/workflows/ci.yml`)**: no `permissions:` block, so
  `GITHUB_TOKEN` gets the repo default; actions pinned to floating tags
  (`@v4`, `@v5`) rather than SHAs; `pip install` unpinned; runs Python 3.9
  while the Pi runs 3.13; `organize.py` and `qbt_config.py` are tracked Python
  but absent from the flake8 file list.
- **Repo files are group-writable** (umask 002). Combined with a `NOPASSWD`
  sudoers entry for `ufw_killswitch.sh`, any member of group `pi` could replace
  it. `chmod -R g-w` on the shell scripts closes it.
- **The documented sudoers file was never installed.**
  `/etc/sudoers.d/vpn-webapp` does not exist on RPI5; `pi` has
  `(ALL) NOPASSWD: ALL` from the stock `90-cloud-init-users`. The web app works
  via unrestricted root rather than the narrow grant INSTALL.md describes, so
  the least-privilege design is currently theatre. Deployment fix, not a code
  change.

Clean, for the record: no secrets in the working tree or git history, and no
known CVEs in the installed dependency set (Flask 3.1.3, Werkzeug 3.1.8,
Jinja2 3.1.6, requests 2.34.2, urllib3 2.7.0).

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

### From the 2026-09-04 full review

- **`is_qbittorrent_running()` failed open, under every teardown gate.**
  `pgrep` with a 2s timeout and `except: return False`, so a timeout under
  torrent load read as "confirmed gone": `stop_qbittorrent()` returned True
  without sending a signal, both gates in `stop_vpn()` passed, and
  `teardown_killswitch()` opened UFW with a live client holding peer sockets.
  Now `probe_qbittorrent()` is tri-state on the 5s budget and the bool wrapper
  treats "could not ask" as *still running* — the opposite direction to the
  tunnel probes, deliberately.
- **The delete step walked into the destination library.** `CLAUDE.md` claimed
  guard 3 existed; it did not. `files_cleanup()` only rejected a folder *inside*
  a destination, never one *containing* one, so cleaning `/mnt/media/Library`
  with Movies at `/mnt/media/Library/Movies` stripped the library of every
  `.nfo`, `.jpg` and `.txt` and removed the emptied folders. The guard now
  lives in `cleanup_source(exclude_roots=...)` where the caller cannot forget
  it, with four regression tests.
- **`organize.py` could `rmtree` the destination library.** With the normal
  nested layout the scan returns the destination as a source subfolder, and the
  delete prompt *defaulted to yes* when nothing was kept. Now
  `_touches_destination()` refuses it, the prompt never defaults to yes, and it
  counts every surviving file rather than only skipped videos — subtitles and
  artwork were being deleted unmentioned.
- **`organize.py`'s blank Movies/TV prompt resolved to the working directory**
  (`realpath("") == cwd`), filing everything into the repo checkout. Same bug
  already fixed on the web path; now rejected on the raw string.
- **`ufw_killswitch.sh` and `ufw_base.sh` could not fail.** Every `ufw` call
  silenced and unchecked, both scripts ending on an `echo`, so exit status was
  always 0 — which `startvpn.sh`'s `KS_RC` and `monitor.py`'s
  `setup_killswitch()` both take as proof. Both now assert against
  `ufw status verbose` and exit non-zero.
- **`stopvpn.sh` opened the firewall without confirming anything stopped.** It
  was the only teardown path missing the "confirmed" rule: `kill`, `sleep 1`,
  `kill -9`, remove the PID file unconditionally, `reset_ufw` regardless — and
  its plain kills are not under sudo, so they fail silently into `2>/dev/null`.
  Now `confirm_qbittorrent_stopped()` gates the firewall step and both call
  sites propagate the failure.
- **Ctrl+C raced the firewall open.** SIGINT reaches the whole process group, so
  `startvpn.sh`'s `cleanup_on_error` ran `ufw_base.sh` within ~2s while
  `checkip.sh` was inside its 30s qBittorrent grace window — up to 31s of a live
  client on the ISP link. It now waits for the client to exit, and leaves the
  kill switch up if it survives SIGKILL.
- **`stop_web.sh` and `remove_killswitch.sh` printed "Stopped." unconditionally**
  after SIGKILL and continued into the firewall step. Both now re-check and halt.
- **`_openvpn_start()` tore down the kill switch with no qBittorrent check** on
  both failure paths, unlike `attempt_reconnect()`. A fail-stop halted at step 1
  leaves the client alive; a failed Start VPN then relaxed UFW underneath it.
  Now gated by `_revert_killswitch_after_failed_start()`.
- **A race could skip the entire fail-stop teardown.** The teardown keyed off
  `not self._stop_event.is_set()`, so a `stop()` landing between a `break` and
  that test made it vanish — including the urgent path, where UFW is confirmed
  open. Now latched in `fail_stop` at all six exit points.
- **`startvpn.sh --ovpn-url` accepted `http://`** with no size cap, no
  private-address check and no payload validation, while the web path enforced
  all of them. This URL picks the tunnel endpoint *and* what the firewall
  whitelists. Now HTTPS-only, scheme pinned across redirects, 1 MB cap,
  `reject_private_host()`, and a `remote`-line check.
- **`qbt_config.py` truncated the live qBittorrent config in place.** `open(w)`
  on the only copy of everything set through the WebUI. Now a temp file in the
  same directory, fsynced, then `os.replace`.

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
