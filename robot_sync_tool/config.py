from dataclasses import dataclass, field
import os


@dataclass
class SyncConfig:
    remote_host: str = os.environ.get("ROBOT_SYNC_REMOTE_HOST", "robot@192.168.61.142")
    jump_host: str = os.environ.get("ROBOT_SYNC_JUMP_HOST", "")
    remote_dir: str = os.environ.get("ROBOT_SYNC_REMOTE_DIR", "~/data/datasets/robot")
    local_dir: str = os.environ.get("ROBOT_SYNC_LOCAL_DIR", "/data03/data/datasets/robot")
    interval_seconds: int = int(os.environ.get("ROBOT_SYNC_INTERVAL_SECONDS", "60"))
    settle_seconds: int = int(os.environ.get("ROBOT_SYNC_SETTLE_SECONDS", "180"))
    concurrent_syncs: int = int(os.environ.get("ROBOT_SYNC_CONCURRENT_SYNCS", "1"))
    record_depth: int = 2
    graceful_stop: bool = True
    ssh_password: str = os.environ.get("ROBOT_SYNC_SSH_PASSWORD", "")
    config_file: str = os.environ.get("ROBOT_SYNC_CONFIG_FILE", "config/robot_sync_config.json")
    records_file: str = os.environ.get("ROBOT_SYNC_RECORDS_FILE", "data/robot_sync_records.json")
    log_file: str = os.environ.get("ROBOT_SYNC_LOG_FILE", "logs/robot_sync.log")
    excludes: list[str] = field(
        default_factory=lambda: [
            "*.zip",
            "*.tar",
            "*.tar.gz",
            "*.tgz",
            "*.gz",
            "*.bz2",
            "*.xz",
            "*.7z",
            "*.rar",
            "*bak*",
            "*BAK*",
        ]
    )
