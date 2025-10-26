# PVE Backup Sync to NAS

Sync Proxmox VE backups to NAS with auto wake and shutdown.

## Quick Start
```bash
# Install dependencies
curl -LsSf https://astral.sh/uv/install.sh | sh
uv sync

# Setup SSH key
ssh-keygen -t rsa -b 4096
ssh-copy-id user@nas_ip

# Configure
cp pve_backup_sync_to_nas/config.example.toml pve_backup_sync_to_nas/config.toml
nano pve_backup_sync_to_nas/config.toml

# Run
uv run pve-backup-sync-to-nas pve_backup_sync_to_nas/config.toml
```

## Configuration
```toml
[nas]
mac_address = "00:11:32:XX:XX:XX"
ip = "192.168.1.100"
ssh_user = "admin"
ssh_key = "~/.ssh/id_rsa"
max_wait_time = 300
ping_interval = 5
ssh_ready_wait = 30

[backup]
local_dir = "/var/lib/vz/dump"
nas_dir = "/volume1/backup/pve"
rsync_options = "-avhP --delete --checksum"

[log]
log_file = "/var/log/pve_backup_sync_to_nas.log"
log_level = "INFO"

[notification]
enabled = false
discord_webhook = "https://discord.com/api/webhooks/..."
on_success = true
on_failure = true
```

## PVE Backup Hook

**Step 1: Create hook script**
```bash
# Create custom scripts directory
mkdir -p /custom-scripts

# Create hook script
cat > /custom-scripts/pve-backup-sync-to-nas-hook.sh << 'EOF'
#!/bin/bash

# Only run when entire backup job completes
if [ "$1" = "job-end" ]; then
    echo "[$(date)] PVE backup job completed, starting NAS sync..." >> /var/log/pve-backup-sync-to-nas-hook.log
    
    cd /root/pve-auto-scripts
    /root/.local/bin/uv run pve-backup-sync-to-nas pve_backup_sync_to_nas/config.toml >> /var/log/pve-backup-sync-to-nas-hook.log 2>&1
    
    if [ $? -eq 0 ]; then
        echo "[$(date)] NAS sync completed successfully" >> /var/log/pve-backup-sync-to-nas-hook.log
    else
        echo "[$(date)] NAS sync failed!" >> /var/log/pve-backup-sync-to-nas-hook.log
    fi
fi
EOF

chmod +x /custom-scripts/pve-backup-sync-to-nas-hook.sh
```

**Step 2: Bind via CLI**
```bash
# List existing backup jobs
pvesh get /cluster/backup

# Bind hook to specific job (replace backup-XXXXXXXX with your job ID)
pvesh set /cluster/backup/backup-XXXXXXXX -script /custom-scripts/pve-backup-sync-to-nas-hook.sh

# Verify configuration
cat /etc/pve/jobs.cfg
# Should show:
# vzdump: backup-XXXXXXXX
#     script /custom-scripts/pve-backup-sync-to-nas-hook.sh
```

**Step 3: Test hook**
```bash
# Manual test
/custom-scripts/pve-backup-sync-to-nas-hook.sh job-end

# View logs
tail -f /var/log/pve-backup-sync-to-nas-hook.log
```

## Discord Notification

1. Create webhook in Discord Server Settings → Integrations → Webhooks
2. Enable in config.toml:
```toml
[notification]
enabled = true
discord_webhook = "https://discord.com/api/webhooks/YOUR_URL"
```

## Troubleshooting
```bash
# Test WOL
wakeonlan 00:11:32:XX:XX:XX

# Test SSH
ssh -i ~/.ssh/id_rsa user@nas_ip

# Test rsync (dry-run)
rsync -e "ssh -i ~/.ssh/id_rsa" -avhn --delete /source/ user@nas:/target/

# View logs
tail -f /var/log/pve_backup_sync_to_nas.log
tail -f /var/log/pve-backup-sync-to-nas-hook.log

# Check hook script is bound
cat /etc/pve/jobs.cfg

# Test hook manually
/custom-scripts/pve-backup-sync-to-nas-hook.sh job-end
```

## Requirements

- Python 3.8+
- uv, rsync, ssh, wakeonlan
- Synology NAS with WOL, SSH, and rsync enabled