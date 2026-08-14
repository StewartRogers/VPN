# Implemented behaviour

What this project does and why it does it that way. Open items and future ideas
live in `TODO.md`, not here.

## Security

- **UFW kill switch** — `ufw_killswitch.sh` calls `ufw_base.sh` with
  `UFW_OUT_POLICY=deny`, so the deny-outgoing default is set *before* UFW is
  enabled. It then allows the VPN server endpoint (parsed from the `.ovpn`
  `remote` line), `out on tun0`, DNS on the physical NIC, and the LAN. Applied
  before OpenVPN starts; torn down only by an explicit Stop VPN, `stopvpn.sh`,
  `stop_web.sh`, or `remove_killswitch.sh`.
- **Fail-closed ordering** — every teardown path stops qBittorrent and OpenVPN
  *before* relaxing the firewall, and every startup path raises the firewall
  before starting OpenVPN. There is no window where traffic can egress on the
  ISP link. Both stops are polled to confirmation with a SIGKILL escalation
  rather than assumed: if qBittorrent outlives both signals the firewall is
  left locked down and the operator is told, since opening UFW under a live
  client is the leak the ordering exists to prevent.
- **DNS leak prevention** — `/etc/resolv.conf` replaced with Cloudflare
  1.1.1.1/1.0.0.1 and locked with `chattr +i`; the original is backed up to
  `~/.vpn_backups/resolv.conf.backup` and restored on shutdown. Web path only.
- **IPv6 leak prevention** — disabled via `sysctl` before OpenVPN starts.
  `ufw_base.sh` also checks `/etc/default/ufw` on every run and corrects
  `IPV6=no` to `IPV6=yes`, because UFW silently ignores IPv6 otherwise — every
  kill-switch guarantee in this project depends on that one line.
- **qBittorrent interface binding** — bound to `tun0` by name and by its live
  IP at start time (`Session\InterfaceAddress`), a hard socket-level bind. A
  name bind is a preference; an address bind is enforced by the kernel, which
  refuses the send once that address is gone. If tun0 has no address the stale
  key is removed rather than left pointing at an IP the tunnel no longer holds.
- **One qBittorrent config step for both paths** — `qbt_config.py` is called by
  `checkip.sh` and `webapp/monitor.py` alike, so the bind, the save path
  (`QBT_SAVE_PATH`) and the concurrent-download limit
  (`QBT_MAX_ACTIVE_DOWNLOADS`) cannot drift between them. It merges those keys
  into the live config and leaves everything qBittorrent owns untouched, and it
  warns rather than reporting success if the client is already running — a
  write under a running client is ignored at once and overwritten on exit.
- **SSRF protection** — `.ovpn` download rejects non-HTTPS URLs, private,
  loopback and reserved addresses, and unresolvable hosts. The resolved IP is
  pinned for the duration of the fetch, proxies are disabled (a proxy would
  re-resolve the hostname and defeat the pin), redirects are re-validated, and
  the body is capped at 1 MB.
- **Payload validation** — a downloaded config with no `remote` line is
  rejected at download time rather than becoming a tunnel that never comes up.

## The torrent start gate

`VPNMonitor.torrent_start_blocked()` is the single choke point. It requires, in
order: the monitor running, the OpenVPN process alive, `tun0` up, the default
route on `tun0`, the kill switch active, and an external IP that resolves and
differs from the home IP. `start_qbittorrent()` calls it, so *every* path goes
through it, and `POST /api/qbt/start` calls it to return a 409 with the reason.

The UI disables its Start button on the same conditions, but a disabled button
is a hint and not a control — `curl`, a stale browser tab, or a VPN drop
between status polls all still reach the endpoint.

Consequence worth knowing: qBittorrent refuses to start if the external-IP
lookup fails, even on a healthy tunnel (~9s of timeouts first). Starting is
strict; a *running* monitor tolerates three consecutive IP failures before
shutting down.

## Fail-stop, not auto-reconnect

Neither monitor reconnects on its own. Both watch for VPN process death, `tun0`
disappearing, the default route leaving the tunnel, a newly appeared global
IPv6 address, an exit IP matching the home IP, and three consecutive failed IP
lookups. On any of those they stop qBittorrent and exit **with the kill switch
still active** — the web monitor also stops OpenVPN. The machine goes offline
rather than silently reconnecting, which is precisely when leaks slip through.

`attempt_reconnect()` exists in `webapp/monitor.py` but only runs when you press
**Force Reconnect**. There is no `MAX_RECONNECT_ATTEMPTS`; `MAX_STARTUP_ATTEMPTS`
governs retries during initial connection in both paths, and both offer a way
out early (an interactive prompt in `startvpn.sh`, a Cancel Retry button and
`/api/vpn/cancel-retry` in the web app).

The kill switch is re-verified on the IP-check cadence in the web monitor, so a
UFW reset from another terminal stops everything. `checkip.sh` still checks it
only at startup — see `TODO.md`.

## Manual step ordering (web path)

In the web app, starting the VPN starts *only* the VPN. The monitor (step 3)
and qBittorrent (step 4) are separate, deliberate clicks. An earlier revision
chained all three off one click; that took the ordering away from the operator,
which matters when the tunnel comes up on the wrong exit or a config needs
swapping. The safety chaining was there to provide now lives in the start gate
above, which refuses a torrent start unless the monitor is already running.

The CLI path is unchanged and still chains: `startvpn.sh` launches `checkip.sh`
once the tunnel verifies, and `checkip.sh` starts qBittorrent after its own
startup verification. That flow is a single foreground session where the
operator is watching the output, so the ordering is visible as it happens.

## Configuration

`~/.vpn_config.conf` takes precedence over `./vpn_config.conf`. Both paths use
the same search order. See README.md for the live keys.

## Code quality

- **Test suite** — 79 pytest unit tests covering the monitor loop, kill-switch
  state machine, IP leak detection, the SSRF guard, logging, the torrent start
  gate, and the qBittorrent config merge.
- **CI** — GitHub Actions runs `flake8 --select=E9,F` and pytest on every push
  and PR to `master`. No `shellcheck` yet, on a mostly-bash project — see
  `TODO.md`.

## Documentation map

- `README.md` — overview, both front ends, configuration, logs
- `INSTALL.md` — installation, sudoers, first run
- `TROUBLESHOOTING.md` — diagnosis and recovery
- `TODO.md` — open items, known gaps, deliberate omissions
- `CLAUDE.md` — orientation for AI-assisted work
