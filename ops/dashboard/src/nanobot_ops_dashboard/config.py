from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os


@dataclass(slots=True)
class DashboardConfig:
    project_root: Path
    db_path: Path
    nanobot_repo_root: Path
    eeepc_ssh_host: str
    eeepc_ssh_key: Path
    eeepc_state_root: str
    eeepc_sudo_password: str | None = None
    poll_interval_seconds: int = 300
    max_subagent_records: int = 200


DEFAULT_REPO_ROOT = Path("/home/ozand/herkoot/Projects/nanobot")
DEFAULT_PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = DEFAULT_PROJECT_ROOT / "data" / "dashboard.sqlite3"


def load_config() -> DashboardConfig:
    project_root = Path(os.environ.get("NANOBOT_DASHBOARD_ROOT", str(DEFAULT_PROJECT_ROOT)))
    db_path = Path(os.environ.get("NANOBOT_DASHBOARD_DB", str(DEFAULT_DB_PATH)))
    repo_root = Path(os.environ.get("NANOBOT_REPO_ROOT", str(DEFAULT_REPO_ROOT)))
    ssh_host = os.environ.get("NANOBOT_EEEPC_SSH_HOST", "eeepc")
    ssh_key = Path(os.environ.get("NANOBOT_EEEPC_SSH_KEY", "/home/ozand/.ssh/id_ed25519_eeepc"))
    eeepc_state_root = os.environ.get(
        "NANOBOT_EEEPC_STATE_ROOT",
        "/var/lib/eeepc-agent/self-evolving-agent/state",
    )
    sudo_password = os.environ.get("NANOBOT_EEEPC_SUDO_PASSWORD")
    poll_interval = int(os.environ.get("NANOBOT_DASHBOARD_POLL_INTERVAL", "300"))
    max_subagent_records = int(os.environ.get("NANOBOT_DASHBOARD_MAX_SUBAGENT_RECORDS", "200"))
    return DashboardConfig(
        project_root=project_root,
        db_path=db_path,
        nanobot_repo_root=repo_root,
        eeepc_ssh_host=ssh_host,
        eeepc_ssh_key=ssh_key,
        eeepc_state_root=eeepc_state_root,
        eeepc_sudo_password=sudo_password,
        poll_interval_seconds=poll_interval,
        max_subagent_records=max_subagent_records,
    )
