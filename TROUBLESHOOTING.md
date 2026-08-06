# Troubleshooting Guide

## Common Issues and Solutions

### VPN Connection Issues

#### VPN Won't Connect

**Symptoms:** OpenVPN fails to establish connection

**Solutions:**

1. **Check VPN config file:**
   ```bash
   ls /etc/openvpn/client/*.ovpn
   sudo cat /var/log/openvpn.log
   ```

2. **Verify credentials:** Ensure your VPN provider credentials are correct

3. **Check firewall:** Temporarily disable UFW to test
   ```bash
   sudo ufw disable
   ./startvpn.sh
   ```

4. **Try different server:** Download a different .ovpn file from your VPN provider

5. **Check internet connection:**
   ```bash
   ping 8.8.8.8
   ```

#### VPN Connects But No Internet

**Symptoms:** VPN connects but can't browse internet

**Solutions:**

1. **Check tun0 interface:**
   ```bash
   ip addr show tun0
   ip route show
   ```

2. **Verify DNS:**
   ```bash
   cat /etc/resolv.conf
   nslookup google.com
   ```

3. **Test connectivity:**
   ```bash
   ping 1.1.1.1  # Cloudflare DNS
   curl https://api.ipify.org  # Should show VPN IP
   ```

4. **Restart with DNS fix:**
   ```bash
   ./stopvpn.sh --shutdown-only
   # Manually set DNS
   echo "nameserver 1.1.1.1" | sudo tee /etc/resolv.conf
   ./startvpn.sh
   ```

#### IP Leak Detected

**Symptoms:** External IP matches home IP instead of VPN IP

**Solutions:**

1. **Check VPN status:**
   ```bash
   ./vpn_status.sh
   ```

2. **Verify kill switch:**
   ```bash
   sudo ufw status verbose | grep "deny (outgoing)"
   ```

3. **Check for DNS leaks:**
   ```bash
   # Visit https://dnsleaktest.com in browser
   # OR
   dig +short myip.opendns.com @resolver1.opendns.com
   ```

4. **Manual reconnection:**
   ```bash
   ./stopvpn.sh --shutdown-only
   sleep 5
   ./startvpn.sh
   ```

### BitTorrent Client Issues

#### qBittorrent Won't Start

**Symptoms:** Torrent client fails to start

**Solutions:**

1. **Check if already running:**
   ```bash
   pgrep -f qbittorrent-nox
   killall qbittorrent-nox
   ```

2. **Run manually to see errors:**
   ```bash
   qbittorrent-nox
   # Look for error messages
   # Press Ctrl+C to stop
   ```

3. **Check port availability:**
   ```bash
   sudo netstat -tulpn | grep 8080
   ```

4. **Reset configuration:**
   ```bash
   mv ~/.config/qBittorrent ~/.config/qBittorrent.backup
   ```

#### BitTorrent Traffic Not Through VPN

**Symptoms:** Torrents downloading but concerned about binding

**Solutions:**

1. **Verify binding in qBittorrent web UI:**
   - Access http://localhost:8080
   - Go to Settings → Advanced → Network Interface
   - Ensure "tun0" is selected

2. **Check active connections:**
   ```bash
   sudo netstat -tulpn | grep qbittorrent
   # Should show tun0 or VPN IP
   ```

3. **Force rebind:**
   ```bash
   ./stopvpn.sh --shutdown-only
   rm -f ~/.config/qBittorrent/qBittorrent.conf
   ./startvpn.sh
   ```

### Monitoring Issues

#### Monitoring Script Not Running

**Symptoms:** checkip.sh not active

**Solutions:**

1. **Check if running:**
   ```bash
   pgrep -f checkip.sh
   cat /tmp/vpn_pids/checkip.pid
   ```

2. **Check logs:**
   ```bash
   cat vpn_logs/latest.log
   tail -f vpn_logs/latest.log
   ```

3. **Restart monitoring:**
   ```bash
   ./checkip.sh YOUR_HOME_IP &
   ```

#### False Positive Disconnections

**Symptoms:** VPN keeps reconnecting unnecessarily

**Solutions:**

1. **Adjust monitoring intervals:**
   Edit `vpn_config.conf`:
   ```bash
   FAST_CHECK_INTERVAL=5
   IP_CHECK_INTERVAL=30
   ```

2. **Check network stability:**
   ```bash
   ping -c 100 8.8.8.8
   ```

3. **Note there is no auto-reconnect.**

   `checkip.sh` stops qBittorrent and exits on failure, leaving the kill
   switch active. Restart with `./startvpn.sh`, or use the web UI's
   "Force Reconnect" button.

### Security Issues

#### Kill Switch Not Working

**Symptoms:** Internet accessible when VPN drops

**Solutions:**

The kill switch is **UFW-based**. Manage it only with `ufw` and the scripts
here — raw iptables rules will be silently out of step with what UFW reports.

1. **Verify the rules:**
   ```bash
   sudo ufw status verbose
   sudo ufw status numbered
   ```

2. **Re-apply the kill switch:**
   ```bash
   sudo bash ufw_killswitch.sh
   ```

   It parses the installed `.ovpn` for the server address and whitelists only
   that endpoint, the tun0 tunnel, your LAN, and your DNS resolvers. It now
   verifies its own result and exits non-zero if any rule was rejected.

3. **Check what it allowed:**
   ```bash
   sudo ufw status numbered
   ```

#### DNS Leaks Persist

**Symptoms:** DNS queries going to ISP

**Solutions:**

1. **Check DNS configuration:**
   ```bash
   cat /etc/resolv.conf
   ```

2. **Verify immutability:**
   ```bash
   lsattr /etc/resolv.conf
   # Should show 'i' flag
   ```

3. **Manual DNS lock:**
   ```bash
   echo "nameserver 1.1.1.1" | sudo tee /etc/resolv.conf
   echo "nameserver 1.0.0.1" | sudo tee -a /etc/resolv.conf
   sudo chattr +i /etc/resolv.conf
   ```

### System Issues

#### Can't Access Local Network

**Symptoms:** Can't reach file shares, printers, or SSH

**Solutions:**

1. **Verify local network exceptions:**
   ```bash
   sudo ufw status | grep LAN
   ```

2. **Re-apply with the right subnet:**

   `ufw_killswitch.sh` auto-detects your LAN from the physical interfaces'
   link routes. If detection picks the wrong one, override it:
   ```bash
   sudo LAN_SUBNETS="192.168.1.0/24" bash ufw_killswitch.sh
   ```

   Likewise `DNS_SERVERS` overrides which resolvers are reachable before the
   tunnel is up:
   ```bash
   sudo DNS_SERVERS="192.168.1.1 1.1.1.1" bash ufw_killswitch.sh
   ```

3. **Restart without kill switch:**
   ```bash
   ./stopvpn.sh --shutdown-only
   ./startvpn.sh --no-killswitch
   ```

#### System Won't Return to Normal After Shutdown

**Symptoms:** Internet not working after stopping VPN

**Solutions:**

1. **Use the recovery script — it does all of the below:**
   ```bash
   ./remove_killswitch.sh
   ```

   It restores the UFW base state, re-enables IPv6, unlocks and restores
   `/etc/resolv.conf`, and then verifies the switch is actually gone.

2. **Check backups exist** (only if you want to restore by hand):
   ```bash
   ls -la ~/.vpn_backups/
   ```

   Backups live under `$HOME/.vpn_backups`, not `/tmp` — a world-writable
   location would let any local user swap a backup before it is restored
   with sudo.

3. **Manual restore:**
   ```bash
   # Restore the firewall with UFW. Never flush the iptables OUTPUT chain:
   # that removes UFW's jump rules so nothing filters, while UFW still
   # reports itself active and every check here believes you are protected.
   sudo bash ufw_base.sh

   # Restore DNS
   sudo chattr -i /etc/resolv.conf
   if [ -f ~/.vpn_backups/resolv.conf.backup ]; then
       sudo mv ~/.vpn_backups/resolv.conf.backup /etc/resolv.conf
   fi

   # Restore IPv6
   sudo sysctl -w net.ipv6.conf.all.disable_ipv6=0
   sudo sysctl -w net.ipv6.conf.default.disable_ipv6=0
   ```

3. **Reboot system:**
   ```bash
   sudo reboot
   ```

#### Permission Denied Errors

**Symptoms:** Script fails with permission errors

**Solutions:**

1. **Make scripts executable:**
   ```bash
   chmod +x startvpn.sh stopvpn.sh checkip.sh vpn_status.sh
   ```

2. **Check sudo access:**
   ```bash
   sudo -v
   ```

3. **Fix ownership:**
   ```bash
   sudo chown $USER:$USER *.sh *.py *.conf
   ```

### Performance Issues

#### High CPU Usage

**Symptoms:** System slow, high CPU from monitoring

**Solutions:**

1. **Increase check intervals:**
   ```bash
   # Edit vpn_config.conf
   FAST_CHECK_INTERVAL=5
   IP_CHECK_INTERVAL=30
   ```

2. **Check for runaway processes:**
   ```bash
   top
   # Look for multiple checkip.sh or qbittorrent processes
   ```

#### Slow VPN Speed

**Symptoms:** Downloads very slow through VPN

**Solutions:**

1. **Try different VPN server:** Use different .ovpn file

2. **Disable IPv6 leak prevention:**
   ```bash
   # Edit vpn_config.conf
   DISABLE_IPV6=false
   ```

3. **Check qBittorrent settings:**
   - Reduce number of connections
   - Disable uTP in qBittorrent settings

## Logs and Diagnostics

### Important Log Locations

- VPN connection log: `/var/log/openvpn.log`
- Monitoring log: `./vpn_logs/latest.log` (symlink to the current `session_*.log`)
- qBittorrent log: `./qbit.log`

### Diagnostic Commands

```bash
# Check all status
./vpn_status.sh

# View VPN log
sudo tail -f /var/log/openvpn.log

# View monitoring log
tail -f vpn_logs/latest.log

# View application log
ls -t vpn_logs/session_*.log | head

# Check network interfaces
ip addr show

# Check routing table
ip route show

# Check firewall rules
sudo ufw status verbose

# Check DNS
cat /etc/resolv.conf
nslookup google.com

# Check processes
pgrep -a openvpn
pgrep -a qbittorrent
pgrep -a checkip

# Test external IP
curl https://api.ipify.org
```

## Getting More Help

If you still have issues:

1. **Enable debug mode:**
   ```bash
   # Run with more verbose output
   sudo tail -f /var/log/openvpn.log
   ```

2. **Collect diagnostic information:**
   ```bash
   ./vpn_status.sh > diagnostic.txt
   sudo ufw status verbose >> diagnostic.txt
   ip route show >> diagnostic.txt
   cat /etc/resolv.conf >> diagnostic.txt
   ```

3. **Report issue on GitHub:**
   - Include diagnostic output
   - Describe expected vs actual behavior
   - Include relevant log snippets

## Emergency Recovery

If something goes wrong and you need to restore system immediately:

```bash
# Kill all VPN processes
sudo pkill -f openvpn
sudo pkill -f qbittorrent
sudo pkill -f checkip

# Restore the firewall. Drive UFW — do NOT flush the OUTPUT chain: that
# removes UFW's jump rules so nothing filters, while every status check in
# this project still reports the kill switch as active.
sudo bash ufw_base.sh

# Restore DNS
sudo chattr -i /etc/resolv.conf
echo "nameserver 8.8.8.8" | sudo tee /etc/resolv.conf

# Enable IPv6
sudo sysctl -w net.ipv6.conf.all.disable_ipv6=0

# Reboot if needed
sudo reboot
```
