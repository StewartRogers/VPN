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
   sudo iptables -L OUTPUT -n
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
   cat checkvpn.log
   tail -f ~/.vpn_logs/vpn.log
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

3. **Increase reconnect attempts:**
   ```bash
   MAX_RECONNECT_ATTEMPTS=5
   ```

### Security Issues

#### Kill Switch Not Working

**Symptoms:** Internet accessible when VPN drops

**Solutions:**

1. **Verify iptables rules:**
   ```bash
   sudo iptables -L OUTPUT -n -v
   ```

2. **Manual kill switch setup:**
   ```bash
   sudo iptables -A OUTPUT -o lo -j ACCEPT
   sudo iptables -A OUTPUT -d 192.168.0.0/16 -j ACCEPT
   sudo iptables -A OUTPUT -o tun+ -j ACCEPT
   sudo iptables -A OUTPUT -j DROP
   ```

3. **Check for conflicting rules:**
   ```bash
   sudo iptables -L OUTPUT -n --line-numbers
   # Remove conflicting rules if needed
   sudo iptables -D OUTPUT <line-number>
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
   sudo iptables -L OUTPUT -n | grep "192.168\|10.0\|172.16"
   ```

2. **Add missing rules:**
   ```bash
   sudo iptables -I OUTPUT -d 192.168.0.0/16 -j ACCEPT
   sudo iptables -I OUTPUT -d 10.0.0.0/8 -j ACCEPT
   sudo iptables -I OUTPUT -d 172.16.0.0/12 -j ACCEPT
   ```

3. **Restart without kill switch:**
   ```bash
   ./stopvpn.sh --shutdown-only
   ./startvpn.sh --no-killswitch
   ```

#### System Won't Return to Normal After Shutdown

**Symptoms:** Internet not working after stopping VPN

**Solutions:**

1. **Check if backups exist:**
   ```bash
   ls -la /tmp/vpn_backups/
   ```

2. **Manual restore:**
   ```bash
   # Restore iptables
   if [ -f /tmp/vpn_backups/iptables.backup ]; then
       sudo iptables-restore < /tmp/vpn_backups/iptables.backup
   else
       sudo iptables -F OUTPUT
       sudo iptables -P OUTPUT ACCEPT
   fi
   
   # Restore DNS
   sudo chattr -i /etc/resolv.conf
   if [ -f /tmp/vpn_backups/resolv.conf.backup ]; then
       sudo mv /tmp/vpn_backups/resolv.conf.backup /etc/resolv.conf
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
- Application log: `~/.vpn_logs/vpn.log`
- Monitoring log: `./checkvpn.log`
- qBittorrent log: `./qbit.log`

### Diagnostic Commands

```bash
# Check all status
./vpn_status.sh

# View VPN log
sudo tail -f /var/log/openvpn.log

# View monitoring log
tail -f checkvpn.log

# View application log
tail -f ~/.vpn_logs/vpn.log

# Check network interfaces
ip addr show

# Check routing table
ip route show

# Check iptables rules
sudo iptables -L -n -v

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
   sudo iptables -L -n -v >> diagnostic.txt
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

# Flush iptables
sudo iptables -F OUTPUT
sudo iptables -P OUTPUT ACCEPT

# Restore DNS
sudo chattr -i /etc/resolv.conf
echo "nameserver 8.8.8.8" | sudo tee /etc/resolv.conf

# Enable IPv6
sudo sysctl -w net.ipv6.conf.all.disable_ipv6=0

# Reboot if needed
sudo reboot
```
