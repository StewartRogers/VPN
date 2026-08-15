# Troubleshooting Guide

The firewall is **UFW exclusively**. If you find yourself reaching for
`iptables` here, you are in the wrong place — UFW keeps its rules in the
`ufw-user-output` chain, so `iptables -L OUTPUT` shows nothing useful and
flushing it breaks UFW without disabling it.

## Emergency recovery

Kill switch on, no internet, web app unreachable:

```bash
./remove_killswitch.sh             # normal recovery: restore UFW base state
./remove_killswitch.sh --disable   # last resort: disable UFW entirely
```

It stops the torrent client first, restores the base firewall state, then
restores DNS and IPv6. If even that fails:

```bash
sudo pkill -f qbittorrent-nox
sudo pkill -f openvpn
sudo pkill -f checkip.sh
sudo ufw --force disable
sudo chattr -i /etc/resolv.conf
sudo cp ~/.vpn_backups/resolv.conf.backup /etc/resolv.conf   # if it exists
sudo sysctl -w net.ipv6.conf.all.disable_ipv6=0
sudo sysctl -w net.ipv6.conf.default.disable_ipv6=0
```

Re-enable the firewall afterwards with `sudo bash ufw_base.sh`.

## First-line diagnosis

```bash
sudo ufw status verbose        # "deny (outgoing)" = kill switch on
ip route get 8.8.8.8           # should say "dev tun0"
ip addr show tun0              # tunnel interface and its IP
curl -s https://api.ipify.org  # should NOT be your home IP
pgrep -x openvpn && pgrep -f qbittorrent-nox && pgrep -f checkip.sh
cat /etc/resolv.conf && lsattr /etc/resolv.conf   # 'i' flag = locked
```

Logs:

| Path | Contents |
| --- | --- |
| `vpn_logs/latest.log` | Newest `checkip.sh` session (symlink) |
| `vpn_logs/vpn.log` | `startvpn.sh` / `stopvpn.sh` events |
| `/var/log/openvpn.log` | OpenVPN daemon (`sudo tail -f`) |
| `qbit.log` | qBittorrent stdout |

The web app streams its own log to the browser and keeps it in memory only.

---

## VPN connection

### VPN won't connect

1. **Read the OpenVPN log — it almost always says why:**
   ```bash
   sudo tail -50 /var/log/openvpn.log
   ```
2. **Check the config was installed:**
   ```bash
   ls -lt /etc/openvpn/client/*.ovpn
   ```
   Both paths use the **newest** file by mtime. If you have several, an old one
   is not the problem, but a stale one you thought you replaced might be.
3. **Verify credentials** with your provider. `--auth-nocache` means they are
   never cached, so a bad credential fails every attempt identically.
4. **Try a different server** — download another `.ovpn`. During the startup
   retry loop, both paths let you swap the config between attempts.
5. **Test the plain internet path** with the kill switch down:
   ```bash
   sudo bash ufw_base.sh
   ping -c3 1.1.1.1
   ```

### Kill switch blocks the connection itself

`ufw_killswitch.sh` allows out only to the VPN server IP/port parsed from the
`remote` line of the config it picks. Two things break this:

- **A hostname that resolves to a rotating pool.** The rule pins one IP,
  resolved once. If the provider hands OpenVPN a different address, the
  connection is denied. Re-run `ufw_killswitch.sh` to re-resolve.
- **A config with multiple `remote` lines.** Only the first is whitelisted.

Check what was actually allowed:

```bash
sudo ufw status verbose | grep "VPN server"
grep "^remote " /etc/openvpn/client/*.ovpn
```

### Connected, but no internet

1. **Is the default route on the tunnel?**
   ```bash
   ip route get 8.8.8.8      # want "dev tun0"
   ```
   `tun0` existing is not enough — the monitor requires the route too, and so
   should you.
2. **DNS:**
   ```bash
   cat /etc/resolv.conf       # web path pins 1.1.1.1 / 1.0.0.1
   nslookup google.com
   ```
   If `/etc/resolv.conf` is empty or wrong and locked, unlock it first:
   `sudo chattr -i /etc/resolv.conf`.
3. **Is the kill switch allowing the tunnel out?**
   ```bash
   sudo ufw status verbose | grep tun0    # want "ALLOW OUT ... on tun0"
   ```

### IP leak detected

The monitor reports this when the external IP equals the home IP it was given.
Before assuming the worst, check the home IP is still correct — if your ISP
rotated your address, the comparison is meaningless in both directions.

```bash
ip route get 8.8.8.8
curl -s https://api.ipify.org
dig +short myip.opendns.com @resolver1.opendns.com   # DNS-level check
```

Also test at https://dnsleaktest.com from a browser on the same box.

To restart cleanly:

```bash
./stopvpn.sh --shutdown-only
sleep 5
./startvpn.sh
```

---

## Monitoring

### The monitor keeps shutting everything down

**This is the designed behaviour, not a fault.** Neither monitor reconnects on
its own. Any of the following stops qBittorrent and exits with the kill switch
left active:

- OpenVPN process gone
- `tun0` gone
- default route no longer on `tun0`
- a global IPv6 address appeared
- external IP matches the home IP
- three consecutive failed external-IP lookups

Read `vpn_logs/latest.log` — the `CRITICAL` line names which one fired. There
is no `MAX_RECONNECT_ATTEMPTS` setting to raise; it does not exist in this
codebase. `MAX_STARTUP_ATTEMPTS` only governs retries during initial connection.

If the trigger is repeated IP-lookup failures on an otherwise healthy tunnel,
the lookup services are being blocked or are slow. Widen the check interval:

```bash
# vpn_config.conf
IP_CHECK_INTERVAL=30
```

### Monitor won't start

`checkip.sh` refuses to run unless the kill switch is active and IPv6 is
disabled. It says so and exits:

```bash
sudo ufw status verbose | grep "deny (outgoing)"   # must match
sudo bash ufw_killswitch.sh                        # if not
./checkip.sh <your_home_ip>
```

If you started with `--no-killswitch`, the monitor is intentionally not started.

### Web app: "Refusing to start qBittorrent"

`POST /api/qbt/start` returns 409 with the reason. Each maps to one
precondition:

| Reason | Fix |
| --- | --- |
| the monitor is not running | Start step 3 before step 4 |
| OpenVPN is not running | Start the VPN (step 2) |
| VPN interface (tun0) is down | Tunnel did not come up — check the OpenVPN log |
| Traffic is not routing through tun0 | Route did not move; restart the VPN |
| Kill switch is not active | Check sudoers, then re-apply |
| Could not confirm the external IP | IP services unreachable; retry |
| External IP … is the home IP | Tunnel is not carrying traffic — do not override |

The last one is a real leak indication. Everything else is a precondition.

### Web app can't apply the kill switch

Almost always missing sudoers entries. Verify:

```bash
sudo -n ufw status verbose >/dev/null && echo "ufw: ok" || echo "ufw: MISSING"
sudo -n bash /home/pi/VPN/ufw_base.sh --help >/dev/null 2>&1 \
  && echo "scripts: ok" || echo "scripts: MISSING"
```

The `bash` paths in `/etc/sudoers.d/vpn-webapp` must be the **absolute paths to
this checkout**. See [INSTALL.md](INSTALL.md).

---

## qBittorrent

### Won't start

1. **Already running?**
   ```bash
   pgrep -f qbittorrent-nox
   ```
   Both paths treat an already-running client as success and will not start a
   second one.
2. **Run it in the foreground to see the error:**
   ```bash
   qbittorrent-nox     # Ctrl+C to stop
   ```
3. **WebUI port taken?**
   ```bash
   sudo ss -tulpn | grep 8080
   ```
4. **Reset its config** (the settings this project owns are re-applied on the
   next start; anything you set in the WebUI is not):
   ```bash
   mv ~/.config/qBittorrent ~/.config/qBittorrent.backup
   ```

### Traffic might not be going through the tunnel

The session is bound to `tun0` by name *and* by its live IP. Confirm:

```bash
grep -E "Interface|InterfaceAddress|Session.Port" ~/.config/qBittorrent/qBittorrent.conf
sudo ss -tunp | grep qbittorrent    # source addresses should be the tun0 IP
ip addr show tun0
```

The listen port is 19806, matching the UFW rules. If you change one, change the
other — `qBittorrent.conf` and `ufw_base.sh`.

If the binding is stale after a reconnect (tun0 got a new IP), restart the
client; the bind is applied at start time from the address live at that moment.

### The settings in the file are right, but qBittorrent's UI disagrees

If the config says `Session\Interface=tun0` while **Tools → Options → Advanced**
shows *Network Interface: Any* and *Optional IP address to bind to: All
addresses*, the running client never read that file. qBittorrent loads its
config **once at startup** and rewrites the whole thing on exit, so:

- a client started before the config was applied keeps the old settings in
  memory, ignores the file, and overwrites it on shutdown;
- a client started while `tun0` was down cannot bind to it and falls back to
  listening on everything — the kill switch is what keeps that contained.

Either way it is not bound to the tunnel. Stop qBittorrent, then start it again
through the web app's qBittorrent step (or `checkip.sh`), which applies the
config *before* launching the process:

```bash
sudo pkill -f qbittorrent-nox
.venv/bin/python qbt_config.py   # apply now; prints what it set
```

`qbt_config.py` warns on stderr if qBittorrent is already running, precisely
because that write would be ignored and then discarded.

---

## System

### Can't reach the LAN

The kill switch allows all of RFC1918 out on `eth0`/`wlan0` by default. Check
what is actually allowed and narrow or widen it via config, not by hand:

```bash
sudo ufw status verbose | grep -E "10\.|172\.|192\.168"
```

```bash
# vpn_config.conf
LAN_CIDRS="10.0.0.0/8 172.16.0.0/12 192.168.0.0/16"
```

Re-apply with `sudo bash ufw_killswitch.sh`. Rules added by hand are wiped the
next time the kill switch is applied — it resets UFW first.

### Internet still broken after shutdown

```bash
sudo ufw status verbose        # outgoing should read "allow"
lsattr /etc/resolv.conf        # 'i' means still locked
cat /etc/resolv.conf
```

Restore:

```bash
sudo bash ufw_base.sh
sudo chattr -i /etc/resolv.conf
sudo cp ~/.vpn_backups/resolv.conf.backup /etc/resolv.conf
sudo sysctl -w net.ipv6.conf.all.disable_ipv6=0
sudo sysctl -w net.ipv6.conf.default.disable_ipv6=0
```

The usual cause is killing the web app directly instead of running
`bash stop_web.sh` — `pkill` skips the app's `restore_dns()` and
`restore_ipv6()` and orphans qBittorrent.

### IPv6 concerns

```bash
grep IPV6 /etc/default/ufw           # must be IPV6=yes
sysctl net.ipv6.conf.all.disable_ipv6  # 1 while the VPN is up
ip -6 addr show scope global         # should be empty while the VPN is up
```

`ufw_base.sh` corrects `IPV6=no` automatically on every run. If a global IPv6
address appears while the monitor is running, that is one of its shutdown
triggers.

### Permission denied

```bash
chmod +x *.sh
sudo -v
```

---

## Performance

### High CPU from monitoring

Widen the intervals in `vpn_config.conf`:

```bash
FAST_CHECK_INTERVAL=5
IP_CHECK_INTERVAL=30
```

Also check for duplicates — a stale monitor from a previous session:

```bash
pgrep -af checkip.sh
pgrep -af qbittorrent-nox
```

### Slow throughput

Try a different VPN server first; it is the usual answer. Then reduce
qBittorrent's connection limits and try disabling µTP. Do **not** disable the
IPv6 or DNS protections to chase speed — they are not the bottleneck.

---

## Reporting a problem

```bash
{
  sudo ufw status verbose
  ip route show
  ip route get 8.8.8.8
  ip addr show tun0
  pgrep -ax openvpn
  curl -s https://api.ipify.org; echo
  cat /etc/resolv.conf
  sudo tail -30 /var/log/openvpn.log
  tail -30 vpn_logs/latest.log
} > diagnostic.txt 2>&1
```

Redact your home IP and the VPN server address before posting. Open an issue at
https://github.com/StewartRogers/VPN/issues with expected vs actual behaviour.
