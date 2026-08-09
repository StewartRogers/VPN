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

Known drift between them is tracked in `TODO.md` — read it before assuming a
difference is a bug you just found.

## Running

```bash
./startvpn.sh                  # CLI: config → kill switch → VPN → monitor → qbit
./start_web.sh                 # web dashboard on :5000
./checkip.sh <home_ip>         # monitor alone (requires kill switch already up)
python3 vpn_active.py <home_ip>   # one-shot check; exit 1 = secure, 0 = not

python3 -m pytest -q           # 68 tests
```

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

### Failure response: fail-stop

**Neither monitor reconnects on its own.** On any fast-tier failure, an exit IP
matching the home IP, or three consecutive failed IP lookups, the monitor stops
qBittorrent and exits **with the kill switch still active**. The web monitor
additionally stops OpenVPN. The box goes offline until the user intervenes.

There is no `MAX_RECONNECT_ATTEMPTS`. Do not reintroduce one, and do not
"fix" the fail-stop into a retry loop — a silent reconnect loop is exactly
when leaks slip through. `MAX_STARTUP_ATTEMPTS` governs retries during
*initial* connection only. `attempt_reconnect()` in `webapp/monitor.py` exists
solely for the manual **Force Reconnect** button.

### Ordering invariants

These are the load-bearing rules of the project. Any change that reorders them
is a leak:

1. **The kill switch goes up before OpenVPN starts** and comes down only after
   qBittorrent and OpenVPN are confirmed stopped. Every teardown path
   (`stopvpn.sh`, `stop_web.sh`, `remove_killswitch.sh`, `stop_vpn()`) stops
   clients first, relaxes the firewall last.
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

## Configuration

`~/.vpn_config.conf` takes precedence over `./vpn_config.conf`; both paths use
that search order. **Several keys in the tracked `vpn_config.conf` are read by
nothing** (`SETUP_KILLSWITCH`, `PREVENT_DNS_LEAK`, `DISABLE_IPV6`,
`BIND_TO_VPN_INTERFACE`, `DEFAULT_VIDEO_DEST`, `VPN_HOME`) — see `TODO.md`.
`SETUP_KILLSWITCH=false` in particular is a leftover from the iptables era and
describes the opposite of current behaviour: the kill switch is mandatory.

Live keys: `FAST_CHECK_INTERVAL`, `IP_CHECK_INTERVAL`, `MAX_STARTUP_ATTEMPTS`,
`MAX_SESSIONS`, `VPN_CLIENT_HOME`, `VPN_LOG_FILE`, `PID_DIR`, `LOG_DIR`,
`BACKUP_DIR`, `QBT_SAVE_PATH`, `LAN_CIDRS`.

## Logs

- `vpn_logs/session_<timestamp>.log` — one `checkip.sh` run; `latest.log`
  symlinks the newest; pruned to `MAX_SESSIONS`
- `vpn_logs/vpn.log` — `startvpn.sh` / `stopvpn.sh`, rotated to `vpn.log.1`
- `/var/log/openvpn.log` — OpenVPN daemon
- `qbit.log` — qBittorrent stdout

The web app keeps its log in memory and streams it over SSE.

## Dependencies

`python3` (3.9+) with `requests` and `flask`; `openvpn`, `qbittorrent-nox`,
`ufw`; and `pgrep`, `ip`, `curl`, `ss`, `sudo`.

## sudo requirements (web app)

The user running the web app needs passwordless sudo for OpenVPN, `pkill`,
`ufw`, both `ufw_*.sh` scripts, `sysctl`, and the DNS tools. The full
`/etc/sudoers.d/vpn-webapp` template lives in **INSTALL.md** — keep it there,
not duplicated here, and adjust the two `bash` paths to this checkout.

Without the `ufw` and `bash` entries, `setup_killswitch()` fails,
`check_killswitch_active()` returns `False`, and the monitor refuses to start.

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
