"""PVE backup sync to NAS - Main module"""

import logging
import os
import subprocess
import time
import tomllib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import paramiko
import requests
from ping3 import ping
from wakeonlan import send_magic_packet


# ==================== Configuration ====================


@dataclass
class NASConfig:
    """NAS connection configuration"""

    mac_address: str
    ip: str
    ssh_user: str = "admin"
    ssh_port: int = 22
    ssh_key: Optional[str] = None
    # Wait settings
    max_wait_time: int = 300
    ping_interval: int = 5
    ssh_ready_wait: int = 30


@dataclass
class BackupConfig:
    """Backup configuration"""

    local_dir: str
    nas_dir: str
    rsync_options: str = "-avhP --delete --checksum"


@dataclass
class LogConfig:
    """Logging configuration"""

    log_file: str = "/var/log/pve_backup_sync_to_nas.log"
    log_level: str = "INFO"


@dataclass
class NotificationConfig:
    """Notification configuration"""

    enabled: bool = False
    discord_webhook: Optional[str] = None
    on_success: bool = True
    on_failure: bool = True


@dataclass
class Config:
    """Main configuration container"""

    nas: NASConfig
    backup: BackupConfig
    log: LogConfig
    notification: NotificationConfig


def load_config(config_path: str) -> Config:
    """Load configuration from TOML file"""
    config_file = Path(config_path)

    if not config_file.exists():
        raise FileNotFoundError(
            f"Configuration file not found: {config_path}\n"
            f"Please create config.toml from config.example.toml"
        )

    with open(config_file, "rb") as f:
        data = tomllib.load(f)

    return Config(
        nas=NASConfig(**data.get("nas", {})),
        backup=BackupConfig(**data.get("backup", {})),
        log=LogConfig(**data.get("log", {})),
        notification=NotificationConfig(**data.get("notification", {})),
    )


# ==================== Notification ====================


def send_discord_notification(
    webhook_url: str,
    success: bool,
    duration: float,
    file_size: Optional[str] = None,
    error_msg: Optional[str] = None,
):
    """Send Discord webhook notification"""
    if not webhook_url:
        return

    color = 0x00FF00 if success else 0xFF0000  # Green or Red
    title = "✅ Backup Successful" if success else "❌ Backup Failed"

    fields = [
        {"name": "Status", "value": "Success" if success else "Failed", "inline": True},
        {"name": "Duration", "value": f"{duration:.1f}s", "inline": True},
    ]

    if file_size:
        fields.append({"name": "Size", "value": file_size, "inline": True})

    if error_msg:
        fields.append(
            {"name": "Error", "value": f"```{error_msg[:1000]}```", "inline": False}
        )

    payload = {
        "embeds": [
            {
                "title": title,
                "color": color,
                "fields": fields,
                "timestamp": datetime.now().isoformat(),
                "footer": {"text": "PVE Backup Sync to NAS"},
            }
        ]
    }

    try:
        response = requests.post(webhook_url, json=payload, timeout=10)
        response.raise_for_status()
        logging.info("Discord notification sent")
    except Exception as e:
        logging.warning(f"Failed to send Discord notification: {e}")


def get_directory_size(path: str) -> str:
    """Get human-readable directory size"""
    try:
        result = subprocess.run(
            ["du", "-sh", path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            size = result.stdout.split()[0]
            return size
        return "Unknown"
    except Exception:
        return "Unknown"


# ==================== Backup Logic ====================


class NASBackup:
    """NAS Backup Manager Class"""

    def __init__(self, nas: NASConfig, backup: BackupConfig, log: LogConfig):
        """
        Initialize NAS Backup Manager

        Args:
            nas: NAS configuration
            backup: Backup configuration
            log: Logging configuration
        """
        self.nas = nas
        self.backup = backup
        self.log = log

        # Convenience attributes
        self.nas_ip = nas.ip
        self.nas_mac = nas.mac_address
        self.ssh_user = nas.ssh_user
        self.ssh_port = nas.ssh_port
        self.ssh_key = Path(nas.ssh_key).expanduser() if nas.ssh_key else None
        self.ssh_client = None

        self._setup_logging()

    def _setup_logging(self):
        """Setup logging configuration"""
        log_level = getattr(logging, self.log.log_level.upper(), logging.INFO)

        logging.basicConfig(
            level=log_level,
            format="%(asctime)s - %(levelname)s - %(message)s",
            handlers=[logging.FileHandler(self.log.log_file), logging.StreamHandler()],
        )

    def send_wol(self):
        """Send WOL packet to wake NAS"""
        try:
            send_magic_packet(self.nas_mac)
            logging.info(f"WOL packet sent to {self.nas_mac}")
            return True
        except Exception as e:
            logging.error(f"Failed to send WOL packet: {e}")
            return False

    def ping_host(self, timeout=1):
        """Check if host is online"""
        try:
            result = ping(self.nas_ip, timeout=timeout)
            return result is not None
        except Exception:
            return False

    def check_ssh_ready(self, timeout=5):
        """Check if SSH service is ready"""
        try:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(
                self.nas_ip,
                port=self.ssh_port,
                username=self.ssh_user,
                key_filename=str(self.ssh_key) if self.ssh_key else None,
                timeout=timeout,
                look_for_keys=True,
                allow_agent=True,
            )
            client.close()
            return True
        except Exception:
            return False

    def wait_for_online(self):
        """Wait for NAS to be online and ready"""
        max_wait = self.nas.max_wait_time
        check_interval = self.nas.ping_interval
        ssh_wait = self.nas.ssh_ready_wait

        logging.info(f"Waiting for NAS ({self.nas_ip}) to come online...")
        start_time = time.time()

        # Phase 1: Wait for ping response
        while time.time() - start_time < max_wait:
            if self.ping_host():
                logging.info(f"NAS responded to ping")
                break
            time.sleep(check_interval)
        else:
            logging.error(f"Timeout ({max_wait} seconds), NAS did not respond to ping")
            return False

        # Phase 2: Wait for SSH service to be ready
        logging.info("Waiting for SSH service to be ready...")
        time.sleep(ssh_wait)

        retry_count = 0
        while retry_count < 10:
            if self.check_ssh_ready():
                logging.info("SSH service is ready")
                return True
            retry_count += 1
            time.sleep(5)

        logging.error("SSH service not ready after retries")
        return False

    def connect_ssh(self):
        """Establish SSH connection"""
        try:
            self.ssh_client = paramiko.SSHClient()
            self.ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

            connect_kwargs = {
                "hostname": self.nas_ip,
                "port": self.ssh_port,
                "username": self.ssh_user,
                "timeout": 10,
                "look_for_keys": True,
                "allow_agent": True,
            }

            if self.ssh_key and self.ssh_key.exists():
                connect_kwargs["key_filename"] = str(self.ssh_key)

            self.ssh_client.connect(**connect_kwargs)
            logging.info("SSH connection established")
            return True
        except Exception as e:
            logging.error(f"SSH connection failed: {e}")
            return False

    def execute_ssh_command(self, command):
        """Execute SSH command"""
        if not self.ssh_client:
            return None, None, False

        try:
            stdin, stdout, stderr = self.ssh_client.exec_command(command, timeout=30)
            exit_status = stdout.channel.recv_exit_status()
            output = stdout.read().decode("utf-8")
            error = stderr.read().decode("utf-8")
            return output, error, (exit_status == 0)
        except Exception as e:
            logging.error(f"Failed to execute SSH command: {e}")
            return None, str(e), False

    def close_ssh(self):
        """Close SSH connection"""
        if self.ssh_client:
            self.ssh_client.close()
            logging.info("SSH connection closed")

    def rsync_backup(self):
        """Execute Rsync backup"""
        source_dir = self.backup.local_dir
        target_dir = self.backup.nas_dir
        rsync_opts = self.backup.rsync_options

        try:
            if not Path(source_dir).exists():
                logging.error(f"Source directory does not exist: {source_dir}")
                return False

            target = f"{self.ssh_user}@{self.nas_ip}:{target_dir}"

            # Build rsync command with proper SSH path
            if self.ssh_key and self.ssh_key.exists():
                # Use RSYNC_RSH environment variable instead
                env = os.environ.copy()
                env['RSYNC_RSH'] = f"/usr/bin/ssh -i {self.ssh_key} -p {self.ssh_port}"
                
                cmd = ["/usr/bin/rsync"]
                cmd.extend(rsync_opts.split())
                cmd.extend([f"{source_dir}/", f"{target}/"])
            else:
                cmd = ["/usr/bin/rsync"]
                cmd.extend(rsync_opts.split())
                cmd.extend([f"{source_dir}/", f"{target}/"])
                env = None

            logging.info(f"Starting Rsync backup...")
            logging.info(f"Command: {' '.join(cmd)}")

            # Use Popen for real-time output
            process = subprocess.Popen(
                cmd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                universal_newlines=True,
            )

            # Print output in real-time
            for line in process.stdout:
                print(line, end="")

            # Wait for process to complete
            process.wait()

            if process.returncode == 0:
                logging.info("Rsync backup completed successfully")
                return True
            else:
                logging.error(f"Rsync failed (return code: {process.returncode})")
                return False

        except Exception as e:
            logging.error(f"Rsync execution exception: {e}")
            return False

    def shutdown_nas(self):
        """Shutdown NAS"""
        try:
            logging.info("Shutting down NAS...")

            if self.ssh_client:
                try:
                    self.ssh_client.exec_command("sudo shutdown -h now", timeout=5)
                    logging.info("Shutdown command sent")
                    return True
                except Exception:
                    logging.info(
                        "Shutdown command sent (connection interruption is normal)"
                    )
                    return True
            else:
                logging.error("SSH client not connected, cannot shutdown")
                return False

        except Exception as e:
            logging.error(f"Failed to shutdown NAS: {e}")
            return False


# ==================== Entry Point ====================


def main():
    """Entry point for CLI"""
    import sys

    if len(sys.argv) < 2:
        print("Usage: pve-backup-sync-to-nas <config.toml>")
        sys.exit(1)

    # Load configuration
    try:
        config = load_config(sys.argv[1])
    except FileNotFoundError as e:
        print(f"Error: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"Failed to load configuration: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)

    # Initialize backup manager
    backup = NASBackup(config.nas, config.backup, config.log)

    # Track execution
    start_time = time.time()
    success = False
    error_msg = None
    file_size = None

    try:
        logging.info("=" * 60)
        logging.info("PVE Backup Sync to NAS Started")
        logging.info(f"Execution time: {datetime.now()}")
        logging.info("=" * 60)

        # Step 1: Wake NAS
        logging.info("\n[Step 1] Wake NAS")
        if not backup.send_wol():
            raise Exception("Failed to send WOL packet")

        # Step 2: Wait for NAS to be online
        logging.info("\n[Step 2] Wait for NAS to be online")
        if not backup.wait_for_online():
            raise Exception("NAS failed to come online")

        # Connect SSH
        if not backup.connect_ssh():
            raise Exception("Failed to establish SSH connection")

        # Step 3: Execute Rsync backup
        logging.info("\n[Step 3] Execute Rsync backup")
        if not backup.rsync_backup():
            raise Exception("Backup failed")

        # Get backup size
        file_size = get_directory_size(config.backup.local_dir)

        # Step 4: Shutdown NAS
        logging.info("\n[Step 4] Shutdown NAS")
        if not backup.shutdown_nas():
            logging.warning("Failed to shutdown NAS, please check manually")

        # Success
        success = True
        logging.info("\n" + "=" * 60)
        logging.info("Backup process completed successfully!")
        logging.info("=" * 60)

    except KeyboardInterrupt:
        logging.info("\nUser interrupted execution")
        error_msg = "User interrupted"

    except Exception as e:
        logging.error(f"\nError occurred during execution: {e}")
        import traceback

        logging.error(traceback.format_exc())
        error_msg = str(e)

    finally:
        # Always close SSH connection
        backup.close_ssh()

        # Calculate duration
        duration = time.time() - start_time

        # Send notification if enabled
        if config.notification.enabled:
            should_notify = (success and config.notification.on_success) or (
                not success and config.notification.on_failure
            )

            if should_notify and config.notification.discord_webhook:
                send_discord_notification(
                    webhook_url=config.notification.discord_webhook,
                    success=success,
                    duration=duration,
                    file_size=file_size,
                    error_msg=error_msg,
                )

        # Exit with appropriate code
        sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
