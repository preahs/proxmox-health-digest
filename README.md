# Proxmox Health Digest

A weekly phone digest of every LXC on a Proxmox VE node. It shows each container's state, CPU, memory, disk and uptime, and publishes the summary to [ntfy](https://ntfy.sh) (`ntfy.your.domain`).

- **Standard library only.** It needs just `python3`, so it runs on the PVE host itself or in any small LXC.
- **Read-only API token.** It needs only the `PVEAuditor` role.
- **Flags problems.** When something needs attention, the digest is sent at high priority and those containers are listed at the top:
  - a disk at or above `DISK_WARN_PCT`
  - memory at or above `MEM_WARN_PCT`
  - a container that is stopped but set to start on boot (`onboot=1`)
- **Reports its own failures.** If the PVE API can't be reached, you get a "digest failed" alert instead of nothing.

Example notification:

```
r730: 2/4 LXCs running, 2 ⚠

Host · up 3d 0h · CPU 7% · RAM 40G/128G (31%) · root 20%

Needs attention:
⚠ 102 nextcloud: disk 91%
⚠ 103 vault: stopped but set to start on boot

🟢 101 pihole · up 1d 1h
   CPU 3% avg/5% pk · RAM 200M/512M · Disk 2G/8G (25%)
🟢 102 nextcloud · up 1h 0m
   CPU 12% avg/64% pk · RAM 3G/4G · Disk 29G/32G (91%)
🔴 103 vault · stopped
⚪ 105 old · stopped
```

Where the CPU numbers come from:

- **avg/pk:** the average and highest 30-minute average CPU over the past week, taken from PVE's RRD history. A single snapshot wouldn't be as helpful when covering the span of a week. I set my backups to keep the past seven, so this method works perfectly.
- **Snapshot fallback:** if the RRD history isn't available, the digest shows the CPU usage at the moment it ran instead.

Memory and disk are always point-in-time values. Stopped containers show only their state, because PVE doesn't report their disk usage.

## 1. Create a read-only API token

On the PVE host:

```bash
pveum user add digest@pve --comment "health digest"
pveum acl modify / --users digest@pve --roles PVEAuditor
pveum user token add digest@pve health --privsep 1
pveum acl modify / --tokens 'digest@pve!health' --roles PVEAuditor
```

Save the token secret that the last `token add` prints. It is shown only once. With privilege separation on (`--privsep 1`), the token needs its own ACL, which is why `acl modify` runs twice.

## 2. Install

On the PVE host, or any machine that can reach `https://<host>:8006`:

```bash
git clone <this repo> /opt/pve-digest
install -d -m 700 /etc/pve-digest
install -m 600 /opt/pve-digest/pve-digest.env.example /etc/pve-digest/pve-digest.env
$EDITOR /etc/pve-digest/pve-digest.env
```

**TLS.** PVE uses a self-signed certificate by default. Pick one of these:

- Copy `/etc/pve/pve-root-ca.pem` to `/etc/pve-digest/` and set `PVE_CA_BUNDLE=/etc/pve-digest/pve-root-ca.pem`.
- Set `PVE_VERIFY_TLS=false`. This is fine on a trusted LAN.

**ntfy auth.** If your topic is protected, create a token and put it in `NTFY_TOKEN`:

```bash
ntfy token add <user>
```

## 3. Test it

```bash
set -a; . /etc/pve-digest/pve-digest.env; set +a
python3 /opt/pve-digest/pve_digest.py --dry-run
python3 /opt/pve-digest/pve_digest.py
```

The first run prints the digest without sending it. The second sends it to your phone.

## 4. Schedule it

```bash
cp /opt/pve-digest/systemd/pve-digest.{service,timer} /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now pve-digest.timer
systemctl list-timers pve-digest.timer
```

- **Schedule.** The timer fires Sundays at 09:00. Edit `OnCalendar=` in the timer to change that.
- **Missed runs.** `Persistent=true` catches up on a run missed while the host was off.
- **Sandboxing.** The service runs as a sandboxed `DynamicUser`, so it can't read files outside the env file and CA that systemd passes it.

To trigger a run by hand and see its output:

```bash
systemctl start pve-digest.service
journalctl -u pve-digest.service -n 20
```

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `PVE_HOST` | required | Hostname or URL. Port 8006 is assumed. |
| `PVE_TOKEN_ID` | required | `user@realm!tokenid` |
| `PVE_TOKEN_SECRET` | required | Token UUID |
| `PVE_NODE` | auto | Required only if the cluster has more than one node. |
| `PVE_VERIFY_TLS` | `true` | |
| `PVE_CA_BUNDLE` | none | Path to the PVE root CA |
| `NTFY_URL` | `https://ntfy.mediahaven.space` | |
| `NTFY_TOPIC` | required | |
| `NTFY_TOKEN` | none | Bearer token for protected topics |
| `NTFY_PRIORITY` | `default` | Used for clean digests. Digests with warnings are always `high`. |
| `DISK_WARN_PCT` | `85` | |
| `MEM_WARN_PCT` | `90` | |
| `RRD_TIMEFRAME` | `week` | Window for CPU avg/peak: `hour`, `day`, `week`, `month` or `year` |

## Development

```bash
python3 -m unittest discover -s tests -v
```
