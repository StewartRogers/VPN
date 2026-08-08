# TODO

From the full-repo review (2026-08-08). Fixes applied 2026-08-08, **uncommitted**.

> **None of the Python changes have been run.** No interpreter on the dev
> machine has pytest and package installs are blocked there, so the suite could
> not be executed. Everything below is byte-compile-clean and shell-syntax-clean
> only. Run `python -m pytest -v` on the Pi before trusting any of it.

---

# Done

## 1. `stop_web.sh` removed the kill switch but left torrents running — LEAK

`stop_web.sh` did two things: `pkill -f webapp/app.py`, then `ufw_base.sh`
(which restores `default allow outgoing`). Neither qBittorrent nor OpenVPN was
stopped, and since qBittorrent is a child of Flask, killing Flask **orphaned**
it. End state: kill switch off, torrents running, no monitor.

`pkill` also skipped the app's `restore_dns()` / `restore_ipv6()`, leaving
`/etc/resolv.conf` pinned to 1.1.1.1 with the immutable bit set.

**Fixed:** rewritten as an ordered teardown mirroring `stopvpn.sh` —
qBittorrent → OpenVPN → web app → DNS/IPv6 restore → **UFW reset last**. Does
not depend on the API, so it works even if Flask is wedged.

## 3. Kill switch opened a fail-open window every time it was applied

`ufw_killswitch.sh` called `ufw_base.sh`, which did `reset` →
`default allow outgoing` → `enable`, and only then flipped to
`default deny outgoing`. UFW was live with outgoing unrestricted in between —
on every kill switch application, including every reconnect.

**Fixed:** `ufw_base.sh` takes `UFW_OUT_POLICY` (default `allow`) and applies it
*before* `ufw --force enable`; `ufw_killswitch.sh` passes `deny`. Worst case is
now a brief total outage (fail-closed) rather than brief exposure (fail-open).

`attempt_reconnect()` also now stops qBittorrent before touching the kill
switch — it previously reset UFW with the client still running.

## 4. `remove_killswitch.sh` was dead code

Entirely iptables-based; restored `~/.vpn_backups/iptables.backup`, which
nothing has created since the UFW rewrite, and never called `ufw_base.sh`.

**Fixed:** rewritten for UFW. Stops the torrent client *first* (the script's
whole job is to reopen outbound traffic), restores base state, falls back to
`ufw --force disable` if that fails, then restores DNS and IPv6. `--disable`
forces the last-resort path.

## 5. Documented sudoers template could not run the kill switch

`CLAUDE.md` granted three **iptables** entries and no `ufw` entry, so
`setup_killswitch()` failed and `check_killswitch_active()` returned `False`.

**Fixed:** iptables entries replaced with `/usr/sbin/ufw` and the two
`bash ufw_*.sh` entries, plus a copy-paste verification snippet.

## 6. `detect_external_ip()` failed open on an HTTP error body

No `raise_for_status()`, and `ipv4.icanhazip.com` is parsed as
`r.text.strip()` — so a 502 HTML page became "the external IP", never matched
`home_ip`, and the leak check passed permanently.

**Fixed:** `raise_for_status()` plus `ipaddress.IPv4Address()` validation in
`webapp/monitor.py`. The same validation added to `vpn_active.py`, which also
fixes the dual-stack `api64.ipify.org` problem (item 12) — an IPv6 reply is now
rejected and falls through instead of always reading as "secure".

## 8. Kill switch was verified once and never rechecked

**Fixed** in `VPNMonitor._run()` — re-checked on the IP-check cadence (not the
2s fast cadence, to avoid a `sudo` call every two seconds). A UFW reset from
another terminal now stops everything.

Not done in `checkip.sh` — see *Deliberately skipped*.

## 12. Smaller items

- `_fetch_pinned` now passes `proxies={"http": None, "https": None}` — `requests`
  honours `HTTPS_PROXY` and a proxy resolves the hostname itself, bypassing the
  DNS pin. Also capped at 1 MB and returns `bytes` rather than a streamed
  response.
- `download_ovpn` rejects a payload with no `remote` line, so an error page
  saved as `.ovpn` fails at download time instead of as a tunnel that won't come
  up.
- `_openvpn_start` picks the newest `.ovpn` by mtime, matching `startvpn.sh`,
  instead of an arbitrary `glob` entry.

## Web path parity (raised separately)

The web UI required three separate clicks with nothing tying them together, and
the qBittorrent gate was **client-side only**:

```js
document.getElementById('btn-qbt-start').disabled = !(s.running && s.secure);
```

`POST /api/qbt/start` itself had no checks at all. `curl`, a stale tab, or a VPN
drop between status polls would start torrents with the tunnel down.

**Fixed:**
- New `VPNMonitor.torrent_start_blocked()` — one gate checking OpenVPN process,
  tun0, default route, kill switch, and exit-IP ≠ home-IP. Enforced inside
  `start_qbittorrent()`, so *every* path goes through it, and called by
  `/api/qbt/start` to return a 409 with the reason.
- `_openvpn_start()` no longer reports success when tun0 merely exists — it now
  requires the default route to be on tun0 and the exit IP to differ from the
  home IP.
- `start_vpn()` chains: OpenVPN → monitor → qBittorrent, matching
  `startvpn.sh` → `checkip.sh`. Nothing downstream runs on an unverified tunnel.

**Behaviour change worth knowing:** qBittorrent will now refuse to start if the
external-IP lookup fails, even with a healthy tunnel (~9s of timeouts first).
Starting is strict; the running monitor still tolerates 3 consecutive IP
failures before shutting down.

---

# Deliberately skipped

**Item 2 — network-namespace migration.** Reverted at your request. Not
reattempted.

**Item 9 — DNS narrowing.** `ufw_killswitch.sh:75-76` still allows plaintext DNS
to any server over the physical NIC, so the ISP still sees every tracker
hostname. Commit `a6c29db` shows narrowing this broke systemd-resolved once
already. Needs testing on the Pi against the pre-tunnel window, not a blind edit.

**Items 7, 11, and the `checkip.sh` half of 8 and 12** — all live in
`checkip.sh`, the shell monitor `startvpn.sh` launches. You said not to change
`startvpn.sh`; `checkip.sh` is the same flow, so I left it alone. Still open:

- `checkip.sh:216-220` starts qBittorrent when the IP check returns "error".
- `checkip.sh:66-68` checks the kill switch only at startup.
- `checkip.sh:95` traps `EXIT` only — an untrapped `SIGTERM` skips cleanup and
  leaves qBittorrent running.
- `vpn_active.py` is only used by `checkip.sh`; its IPv4 validation is in, but
  the shell path still has no `torrent_start_blocked()` equivalent.

**Item 11 — leak detection rests on a dynamic IP.** Both paths only test
`external_ip != home_ip`. If your ISP rotates your address while the tunnel is
up and the tunnel then drops, neither monitor notices. Fixing it properly means
asserting the exit IP *matches the VPN server* rather than *differs from home* —
a design decision, not a patch.

---

# Still open (unchanged)

**Item 10 — the two paths have drifted.** Partly closed (stop paths now match,
config selection now matches). Still differing: the web path disables IPv6 and
rewrites `resolv.conf`; the shell path only checks IPv6 and leaves DNS alone.
The web path binds qBittorrent to tun0 via `apply_qbittorrent_config()`; the
shell path does not.

**Item 12 leftovers:**
- ~~`vpn_status.sh`~~ — deleted. Nothing called it, and it reported the kill
  switch by grepping `iptables -L OUTPUT`, which never matches under UFW. Doc
  references replaced with the equivalent `ufw`/`ip route` commands.
- Torrent port mismatch: `qBittorrent.conf:4` uses `56422`, `ufw_base.sh:44-45`
  opens `19806`.
- LAN hardcoded to `10.0.0.0/24` (`ufw_killswitch.sh:77-78`).
- `ufw_killswitch.sh:28` whitelists only the first `remote` line, resolved once.
- `webapp/app.py:289` `/api/files/scan` takes an arbitrary `dir`. Low risk on a
  LAN-only box with no inbound access.
- CI runs flake8 and pytest but no `shellcheck`, on a mostly-bash project. No
  coverage for `checkip.sh`, `ufw_killswitch.sh`, or `ufw_base.sh`.

## Ideas (migrated from ENHANCEMENTS.md)

- **`qBittorrent.conf` in the repo** — tracked as a template, which is
  confusing. Document it in `INSTALL.md` or move it to `examples/`. Note only
  the web path installs it (`monitor.py:apply_qbittorrent_config`); the shell
  path never does.
- **Systemd service file** — auto-start on boot without manual intervention.
- **Lockfile for `requirements.txt`** — `pip freeze > requirements-lock.txt`
  for reproducible installs.
- **Flask API documentation** — no formal docs for the 20 endpoints in
  `app.py`.

---

# Not a defect

`--script-security 0` (`webapp/monitor.py:392`): commit `4c165a2` claimed it
breaks the tunnel. The confirmed-working tree reverted *to* still contains it,
so the theory does not hold — on OpenVPN 2.6 interface setup goes through
netlink, not external `ip` calls. Keep it.
