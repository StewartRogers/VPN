# VPN BitTorrent Project — Enhancements

## Implemented

All items below are complete and in production.

### Security
- **UFW kill switch** — blocks all outgoing traffic except the VPN tunnel and LAN; applied before OpenVPN starts, torn down only on user-initiated Stop VPN (`ufw_killswitch.sh` / `ufw_base.sh`)
- **DNS leak prevention** — replaces `/etc/resolv.conf` with Cloudflare 1.1.1.1/1.0.0.1 and locks it with `chattr +i`; restored on shutdown
- **IPv6 leak prevention** — disables IPv6 system-wide via `sysctl` during VPN session
- **qBittorrent interface binding** — binds to `tun0` by name and by live IP address at start time (hard socket-level bind via `Session\InterfaceAddress`)
- **SSRF protection** — `.ovpn` download rejects non-HTTPS URLs, private/loopback/reserved IPs, and unresolvable hosts

### Reliability
- **Auto-reconnect** — up to 3 attempts on VPN failure before emergency shutdown; configurable via `MAX_RECONNECT_ATTEMPTS`
- **Two-tier monitoring loop** — fast process/interface checks (2s) + periodic external IP checks (10s) with 3-consecutive-failure tolerance before shutdown
- **Kill switch on failure** — kill switch stays active when the monitor exits due to VPN failure (traffic cannot leak while waiting for user to intervene)

### Configuration
- **Config file** — `vpn_config.conf` / `~/.vpn_config.conf` for intervals, paths, and feature flags

### Usability
- **Non-interactive mode** — `--non-interactive`, `--ovpn-url` flags on `startvpn.sh`
- **Status script** — `vpn_status.sh` shows OpenVPN, tun0, qBittorrent, monitor, and iptables state
- **Web dashboard** — Flask app (`webapp/`) with live log streaming, one-click VPN/qBittorrent control, and a video file organiser

### Code quality
- **Test suite** — 58 pytest unit tests covering the monitoring loop, kill-switch state machine, IP leak detection, SSRF guard, and logging
- **CI** — GitHub Actions runs flake8 (`E9,F`) and pytest on every push and PR

### Documentation
- `README.md` — quick-start, CLI and web app usage
- `INSTALL.md` — full installation guide including sudoers config
- `TROUBLESHOOTING.md` — common issues
- `CLAUDE.md` — developer guide for AI-assisted work

---

## Open / Future

- **`qBittorrent.conf` in repo** — currently tracked as a template; consider documenting this explicitly in `INSTALL.md` or moving it to an `examples/` directory to avoid confusion
- **Systemd service file** — would allow auto-start on boot without manual intervention
- **Lockfile for `requirements.txt`** — `pip freeze > requirements-lock.txt` for reproducible installs
- **Flask API documentation** — no formal docs for the 20+ endpoints in `app.py`
