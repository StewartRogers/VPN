# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with
code in this repository.

## Project Overview

VPN monitoring and kill-switch system for a Raspberry Pi running OpenVPN +
qBittorrent. It monitors VPN connectivity, detects IP leaks, and shuts
torrenting down if the tunnel is compromised.

## Two paths, one job

There are two independent implementations of the same flow. Changing one
usually means checking the other.

| | CLI | Web |
| --- | --- | --- |
| Entry point | `startvpn.sh` | `start_web.sh` → `webapp/app.py` |
| Monitor | `checkip.sh` (bash) | `VPNMonitor._run()` in `webapp/monitor.py` |
| Leak check | `vpn_active.py` | `VPNMonitor.get_external_ip()` |
| Teardown | `stopvpn.sh` | `stop_web.sh` |
| qBittorrent config | `qbt_config.py` | `qbt_config.py` |

Known drift between them is tracked in `TODO.md` — read it before assuming a
difference is a bug you just found.

## Running

```bash
./startvpn.sh                  # CLI: config → kill switch → VPN → monitor → qbit
./start_web.sh                 # web dashboard on :5000
./checkip.sh <home_ip>         # monitor alone (requires kill switch already up)
./setup_venv.sh                # create ./.venv from webapp/requirements.txt

.venv/bin/python vpn_active.py <home_ip>   # one-shot; exit 1 = secure, 0 = not
.venv/bin/python qbt_config.py    # apply the qBittorrent settings by hand
.venv/bin/python -m pytest -q     # 213 tests
```

The shell scripts resolve their interpreter by sourcing `py_env.sh`, which sets
`VPN_PYTHON` to `./.venv/bin/python` when that exists and to `python3`
otherwise. Call Python through `"$VPN_PYTHON"` in shell, never `python3`
directly. Nothing in this project runs Python under `sudo` — every privileged
call is a bash-level `sudo` of `ufw`/`openvpn`/`pkill`/`sysctl`. Keep it that
way: `sudo` resets `PATH` and drops the venv, which would force absolute
interpreter paths into `/etc/sudoers.d/vpn-webapp`.

Live state, without any of the above:

```bash
sudo ufw status verbose        # kill switch: look for "deny (outgoing)"
ip route get 8.8.8.8           # should say "dev tun0"
curl -s https://api.ipify.org  # should NOT be your home IP
```

## Architecture

### Monitoring loop

Both monitors run the same two-tier loop:

- **Fast tier** (`FAST_CHECK_INTERVAL`, default 2s) — OpenVPN process, `tun0`
  presence, default route on `tun0`, no global IPv6 address. All cheap local
  checks, no network calls.
- **Slow tier** (`IP_CHECK_INTERVAL`, default 10s) — external IP lookup
  compared against the home IP. The web monitor also re-verifies the kill
  switch on this cadence (a `sudo` call, hence not on the 2s tier);
  `checkip.sh` still checks it only at startup.

The kill-switch check goes through `probe_killswitch()`, which is **tri-state**:
`"active"`, `"inactive"`, or `"unknown"` when UFW does not answer (timeout,
non-zero exit, missing sudo). Keep the third state. `ufw status verbose` is a
Python program that shells out to iptables — ~0.5s on an idle Pi, slower under
load — and when it was collapsed into a bool, a call that merely ran long was
indistinguishable from a torn-down firewall and fail-stopped a healthy session
(2026-08-14: tunnel up, exit IP correct, killed anyway). Results are cached for
`KILLSWITCH_CACHE_TTL` and shared under a lock, because `/api/status` calls this
on every 3s browser poll and concurrent `ufw` runs contend on the xtables lock.

`check_killswitch_active()` is the bool wrapper: it returns True only for
`"active"`, so everything gated on it (`torrent_start_blocked()`, the monitor's
startup check) still fails closed on an inconclusive probe.

#The fast-tier checks are tri-state for the same reason
(`probe_openvpn_process`, `probe_vpn_interface`, `probe_default_route` return
True/False/None). `except: return False` meant a command that merely timed out
under torrent load read as "the tunnel is gone" — on 2026-08-14 11:32 the
monitor logged "VPN interface down" and tore everything down while OpenVPN's
log shows tun0 up continuously. `FAST_MAX_UNKNOWN` retries an unanswered probe;
a definite False still trips at once. When tun0 cannot be read the route is
unknown, never "bypassing the tunnel".

## Failure response: fail-stop

**Neither monitor reconnects on its own.** On any fast-tier failure, an exit IP
matching the home IP, a *confirmed* inactive kill switch, three consecutive
failed IP lookups, or three consecutive inconclusive kill-switch probes, the
monitor stops qBittorrent and exits **with the kill switch still active**. The
web monitor additionally stops OpenVPN. The box goes offline until the user
intervenes.

Note the asymmetry, and preserve it: a *confirmed* answer trips on the first
check, an *unanswered* one gets `KILLSWITCH_MAX_UNKNOWN` attempts first. The
retry tolerance applies only to "I could not ask" — never soften it into a
grace period for "the kill switch is gone".

There is no `MAX_RECONNECT_ATTEMPTS`. Do not reintroduce one, and do not
"fix" the fail-stop into a retry loop — a silent reconnect loop is exactly
when leaks slip through. `MAX_STARTUP_ATTEMPTS` governs retries during
*initial* connection only. `attempt_reconnect()` in `webapp/monitor.py` exists
solely for the manual **Force Reconnect** button.

### Ordering invariants

These are the load-bearing rules of the project. Any change that reorders them
is a leak:

0. **Teardown is step-by-step, and each step must be *confirmed* before the
   next one starts**: qBittorrent → monitor → OpenVPN → restore (UFW, IPv6,
   DNS). `stop_qbittorrent()` returns True only when the process is actually
   gone — it used to return `None` and set `status["qbittorrent"] = False`
   even after a SIGKILL that failed, so callers advanced on an assumption. Any
   step that cannot be confirmed **halts the sequence** with the kill switch
   left up and a CRITICAL log; it never proceeds on faith. `stop_all()` also
   `join()`s the monitor thread rather than just setting the stop event.

   How qBittorrent is stopped depends on whether traffic is currently exposed:

   - **Kill switch confirmed inactive** → `stop_qbittorrent(urgent=True)`,
     straight to SIGKILL. UFW is passing traffic, so the client is egressing on
     the ISP link right now and unsaved settings are the cheaper loss.
   - **Every other trigger** (VPN down, tun0 gone, IP leak, inconclusive probe)
     → SIGTERM and up to `QBT_STOP_GRACE` (30s) to exit cleanly, SIGKILL only as
     a backstop. The kill switch is still up on these paths, so nothing can leak
     while we wait, and qBittorrent needs those seconds to rewrite
     `qBittorrent.conf` — the only copy of everything set via its WebUI.

   Do not shorten the grace period to make teardown feel snappier. A 5s window
   is what truncated the config write on 2026-08-14.

1. **The kill switch goes up before OpenVPN starts** and comes down only after
   qBittorrent and OpenVPN are confirmed stopped. Every teardown path
   (`stopvpn.sh`, `stop_web.sh`, `remove_killswitch.sh`, `stop_vpn()`) stops
   clients first, relaxes the firewall last. *Confirmed* is the operative word:
   issuing the stops in the right order is not the same as them having worked,
   so `stop_vpn()` polls both processes to exit (escalating to SIGKILL) and
   skips `teardown_killswitch()` entirely if qBittorrent is still alive —
   opening UFW under a live client hands it the ISP link. It logs CRITICAL and
   leaves the kill switch up instead.
2. **`ufw_base.sh` applies its outgoing policy before `ufw --force enable`.**
   `UFW_OUT_POLICY=deny` (passed by `ufw_killswitch.sh`) must never be applied
   after enabling — that was a fail-open window on every kill-switch
   application.
3. **Torrenting starts only through `torrent_start_blocked()`**, which requires
   monitor running → OpenVPN alive → `tun0` up → default route on `tun0` → kill
   switch active → external IP resolves and differs from home IP.
4. **Tunnel verification is route-based, not interface-based.** `tun0`
   existing proves nothing; require the default route on it *and* a non-home
   exit IP.

### Web step ordering

The web UI has four separate steps: config, VPN, monitor, qBittorrent. Starting
the VPN starts **only** the VPN — it does not chain into the monitor or the
client. That is deliberate; an earlier revision chained all three and it took
the ordering away from the operator. The safety that chaining provided lives in
`torrent_start_blocked()`'s monitor-running check instead.

The CLI path does still chain (`startvpn.sh` → `checkip.sh` → qBittorrent),
because it is one foreground session the operator is watching.

There is no status script. `vpn_status.sh` was removed — nothing called it, and
it reported the kill switch by grepping `iptables -L OUTPUT`, which never
matches because UFW keeps its rules in `ufw-user-output`.

## qBittorrent configuration

`qbt_config.py` is the one place either path touches
`~/.config/qBittorrent/qBittorrent.conf`, and it is shared rather than
duplicated — this is the only part of the two-path split that does not drift.

It **merges**; it does not install the repo template over the live file. That
matters for two reasons: qBittorrent rewrites its entire config on exit, so
everything set through its WebUI (credentials, categories, limits) exists only
in that file; and the client reads the file *once at startup*, so a write under
a running instance is ignored now and discarded later. Apply before launching,
never after. `qbt_config.py` warns when the client is already running.

The keys it owns, and nothing else:

- `Session\Interface`, `Session\InterfaceName`, `Session\InterfaceAddress` —
  the tunnel bind. The address bind is the load-bearing one: a name bind is a
  preference, an address bind is enforced by the kernel. When tun0 has no
  address the stale key is removed rather than left pointing at a dead IP.
- `Session\DefaultSavePath` — from `QBT_SAVE_PATH`.
- `Session\QueueingSystemEnabled`, `Session\MaxActiveDownloads`,
  `Session\MaxActiveTorrents` — from `QBT_MAX_ACTIVE_DOWNLOADS`. `MaxActiveTorrents`
  caps downloads *and* seeds together, so it is raised to downloads + the
  existing upload slots; it is never lowered. With no configured value,
  queueing is left untouched — no limit is hardcoded here.

The `[Preferences] Queueing\*` and `Downloads\SavePath` entries in older config
files are pre-4.0 leftovers that qBittorrent migrated once and no longer reads.
Leave them alone; writing there does nothing.

## Firewall

**UFW exclusively.** There are no `iptables` calls in this project;
`ufw_base.sh` and `ufw_killswitch.sh` are the only things that touch the
firewall. Do not add `iptables` commands, including in documentation or
troubleshooting steps — they read as authoritative and are actively harmful
here (flushing `OUTPUT` breaks UFW without disabling it).

UFW only covers IPv6 if `/etc/default/ufw` has `IPV6=yes`. `ufw_base.sh` checks
and auto-corrects this on every run — it is the single line every kill-switch
guarantee depends on. IPv6 is also disabled at the kernel level
(`sysctl net.ipv6.conf.*.disable_ipv6=1`) before OpenVPN starts, as defense in
depth.

## DNS placement

`redirect-gateway def1` moves the default route onto `tun0`, so any nameserver
**outside `LAN_CIDRS` is reached through the tunnel** and must be willing to
answer a query arriving from the VPN's exit IP. Public anycast resolvers
(8.8.8.8, 1.1.1.1) are; an ISP's resolver is not — it drops the query, nothing
comes back, and `getaddrinfo` blocks for its full retry budget rather than
returning an error. Three lookups then take ~60s and surface as "could not
reach IP services" while the tunnel is provably healthy (2026-08-14, RPI5 on
Shaw's resolvers: ICMP through `tun0` fine at 1400 bytes, every hostname dead).

A nameserver inside `LAN_CIDRS` is safe by construction — `ufw_killswitch.sh`
allows it out on the physical interface, so it never enters the tunnel. That
is also a DNS leak: those queries go to the ISP in the clear while torrenting.

`startvpn.sh` warns about off-LAN, non-public resolvers before starting, via
`vpn_active.py --check-resolvers`. It is **advisory only and must stay that
way** — 8.8.8.8 is off-LAN too and is exactly what you want. Note that testing
DNS *before* the VPN starts proves nothing: those queries leave on the physical
interface and an ISP resolver answers them happily. Only the routing
arrangement is diagnosable up front.

When an IP check fails, `vpn_active.diagnose_ip_failure()` resolves a name and
connects to a literal address to say which layer broke. Both monitors call it —
`checkip.sh` on every `error` result, the web monitor only on the third
consecutive failure, since it costs ~10s and would otherwise stall the loop.

## Configuration

`~/.vpn_config.conf` takes precedence over `./vpn_config.conf`; both paths use
that search order. **Several keys in the tracked `vpn_config.conf` are read by
nothing** (`SETUP_KILLSWITCH`, `PREVENT_DNS_LEAK`, `DISABLE_IPV6`,
`BIND_TO_VPN_INTERFACE`, `DEFAULT_VIDEO_DEST`, `VPN_HOME`) — see `TODO.md`.
`SETUP_KILLSWITCH=false` in particular is a leftover from the iptables era and
describes the opposite of current behaviour: the kill switch is mandatory.

Live keys: `FAST_CHECK_INTERVAL`, `IP_CHECK_INTERVAL`, `MAX_STARTUP_ATTEMPTS`,
`MAX_SESSIONS`, `VPN_CLIENT_HOME`, `VPN_LOG_FILE`, `PID_DIR`, `LOG_DIR`,
`BACKUP_DIR`, `QBT_SAVE_PATH`, `QBT_MAX_ACTIVE_DOWNLOADS`, `LAN_CIDRS`.

## Logs

- `vpn_logs/session_<timestamp>.log` — one `checkip.sh` run; `latest.log`
  symlinks the newest; pruned to `MAX_SESSIONS`
- `vpn_logs/vpn.log` — `startvpn.sh` / `stopvpn.sh`, rotated to `vpn.log.1`
- `/var/log/openvpn.log` — OpenVPN daemon
- `qbit.log` — qBittorrent stdout

The web app keeps its log in memory and streams it over SSE.

## Dependencies

`python3` (3.9+) and `python3-venv`; `openvpn`, `qbittorrent-nox`, `ufw`; and
`pgrep`, `ip`, `curl`, `ss`, `sudo`.

`requests` and `flask` are **not** system packages — they live in `./.venv`,
created by `setup_venv.sh` from `webapp/requirements.txt`. The venv is created
without `--system-site-packages` on purpose, so `requirements.txt` is the one
source of truth rather than the venv half-inheriting apt's `python3-requests`.

## sudo requirements (web app)

The user running the web app needs passwordless sudo for OpenVPN, `pkill`,
`ufw`, both `ufw_*.sh` scripts, `sysctl`, and the DNS tools. The full
`/etc/sudoers.d/vpn-webapp` template lives in **INSTALL.md** — keep it there,
not duplicated here, and adjust the two `bash` paths to this checkout.

Without the `ufw` and `bash` entries, `setup_killswitch()` fails,
`check_killswitch_active()` returns `False`, and the monitor refuses to start.

### Binding qBittorrent to the tunnel

**The config file cannot do this.** Verified directly on qBittorrent 4.2.5:
values written to `Session\Interface`, `Session\InterfaceAddress` and
`Session\Port` are *preserved in the file* but never applied. The client
reports `current_network_interface = ''`, picks a random listen port, and
listens on every address. This was silently true for the whole life of the
project — the kill switch was the only layer actually working, and the UFW
rules for the configured peer port never matched the port in use.

`apply_tunnel_bind()` in `webapp/monitor.py` sets it over the WebUI API
(`/api/v2/app/setPreferences`) after startup, then **reads the preferences back
and compares** — a 200 means the request was accepted, not that the bind took,
which is exactly how the file approach failed unnoticed. Auth tries the
unauthenticated localhost path first (`WebUI\LocalHostAuth=false`) and falls
back to `QBT_WEBUI_USER` / `QBT_WEBUI_PASS` from `vpn_config.conf`. If the bind
cannot be applied and confirmed, qBittorrent is stopped.

`qbt_config.py` still writes those keys in case a later qBittorrent honours
them, but do not treat writing them as having bound anything.

### Verifying the tunnel bind

`verify_tunnel_bind()` checks **only the BitTorrent peer port** (`Session\Port`,
via `_qbt_peer_port()`). qBittorrent has two listeners and they have opposite
requirements:

- `Session\Port` (19806) — peer traffic, **must** be on the tun0 address.
- `WebUI\Port` (8080) with `WebUI\Address=*` — the dashboard, **correctly** on
  `0.0.0.0` so it is reachable over the LAN.

Judging every qbittorrent socket flags the healthy WebUI listener and kills a
correctly bound client (2026-08-14 11:18: config applied and bound to
10.211.1.225, killed 0.5s later anyway).

The probe is tri-state for the same reason as the kill-switch one. libtorrent
opens its peer socket seconds after the process starts, so a socket that is not
there yet is `"unknown"` and gets retried for `QBT_BIND_CONFIRM`; only a socket
that is open on the wrong address is `"unbound"`. Absent is not the same as
wrong.

## Web UI step ordering

Start order is VPN -> monitor -> qBittorrent; stop order is the reverse, and a
layer may only be stopped once everything above it is already stopped. This is
enforced in two places, and both are required:

- `applyButtonState()` in `templates/index.html` disables the buttons and puts
  the reason in the tooltip.
- `_ordering_violation()` in `app.py` returns 409 on `/api/vpn/start`,
  `/api/vpn/stop`, `/api/stop` and `/api/reconnect`.

The server check is the real control. A disabled button is a hint that curl, a
stale tab, or a state change between 3s status polls all bypass.

## File organizer

Four steps, each unlocked only by the previous one finishing: scan -> rename ->
move -> delete.

There are **two destinations, Movies and TV**, mirroring `organize.py`'s
interactive prompt, and every scanned row is tagged with one of them (or Skip).
`/api/files/move` takes a `destinations` map of label -> folder plus a `dest`
label per operation, and resolves every path against that map: an operation can
only land in a folder the request declared up front, so a crafted `dest` cannot
write outside the chosen roots. The older single `output_dir` form is still
accepted and becomes the label `output`. Rows set to Skip are simply left out
of the operations list, which is also what keeps their source folders out of
step 4 — the delete step only visits folders the job reported as `moved`.

The move job reports `done_bytes`/`total_bytes` **and** `current_bytes`/
`current_total`/`done_files`/`total_files`, because the UI draws two bars. On a
single 20GB file the overall bar does not move for several minutes, which reads
as a hang; the per-file bar is what shows it is alive.

Job records are mirrored to `organizer_jobs.json` (gitignored; override with
`ORGANIZER_JOBS_FILE`) so a move outlives the browser tab and the web app.
`GET /api/files/move` lists them newest first and the page reattaches to a
running one on load — before that the job id lived only in a JavaScript
variable, so a reload during a 20GB copy left the copy running with nothing
able to see it and step 4 permanently locked. Writes happen per file, not per
chunk, and go through a temp file plus `os.replace` so a poll never reads a
half-written record.

A job still marked `running` at load time reloads as **`interrupted`, never
`complete`** — its copy thread died with the old process. That keeps the step-4
gate (`state == "complete"`) honest; the operator rescans and moves again, and
the files that did finish are recorded as `moved` and skip as duplicates on the
second pass.

The copy is deliberately in-process rather than `rsync`: `move_file()` verifies
the destination size before unlinking the source, and that verification is what
step 4 is gated on. Shelling out to `rsync` would mean parsing its progress
output and trusting its exit status for the same guarantee.

`move_file()` cannot use `os.rename` alone: the output folder is normally on a
different mount (`/mnt/hdddisk` -> `/mnt/bluedrive`) and rename fails with
EXDEV across filesystems. The fallback copies, fsyncs, verifies the destination
size, and only then unlinks the source — so a failed or partial copy leaves the
source intact. That verification is what makes the delete step safe, and it is
why the delete step is gated on the move job reporting `state == "complete"`
rather than on the request having returned.

`scan_directory(skip_junk=True)` and `cleanup_source()` share one definition of
junk (`_JUNK_EXTS`, `_JUNK_DIRS`, `_JUNK_NAME_RE`). They must agree: when the
scan picked up `Sample/sample.mkv`, the move put a 30-second sample in the
output folder and the cleanup then deleted the folder it came from. Anything
the scan returns must never be classified as junk.

`cleanup_source()` never touches the source root itself and refuses any path
outside it. A folder holding unrecognised files is kept, not forced.

The folder browser (`/api/files/browse`) intentionally browses anywhere the web
app user can read — the user chose that over an allowlist. It is still behind
`VPN_API_TOKEN`; do not add a path that skips `_auth()`.

## Notes for changes

- `--script-security 0` is intentional. Commit `4c165a2` claimed it breaks the
  tunnel; the confirmed-working tree still contains it, so the theory does not
  hold. On OpenVPN 2.6 interface setup goes through netlink, not external `ip`
  calls. Keep it.
- Leak detection in both paths rests on `external_ip != home_ip`. If the ISP
  rotates the home address mid-session, neither monitor notices a later drop.
  Fixing that properly means asserting the exit IP *matches the VPN server*, a
  design change rather than a patch.
- The `.ovpn` fetch has an SSRF guard (HTTPS only, private/reserved addresses
  rejected, DNS pinned, proxies disabled, redirects re-validated, 1 MB cap).
  Do not loosen it for convenience.
