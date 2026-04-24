from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import DashboardConfig


CONTROL_FILENAME = 'eeepc_reachability.json'


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def _truncate_text(value: str | None, limit: int = 240) -> str | None:
    if value is None:
        return None
    compact = ' '.join(str(value).split())
    return compact if len(compact) <= limit else compact[: limit - 1] + '…'


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f'.{path.name}.tmp')
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    tmp_path.replace(path)


def _ssh_probe_command(cfg: DashboardConfig) -> list[str]:
    return [
        'ssh',
        '-F', '/home/ozand/.ssh/config',
        '-i', str(cfg.eeepc_ssh_key),
        '-o', 'IdentitiesOnly=yes',
        '-o', 'BatchMode=yes',
        '-o', 'ConnectionAttempts=1',
        '-o', 'ConnectTimeout=5',
        '-o', 'LogLevel=ERROR',
        cfg.eeepc_ssh_host,
        'true',
    ]


def probe_eeepc_reachability(cfg: DashboardConfig, persist: bool = True) -> dict[str, Any]:
    command = _ssh_probe_command(cfg)
    collected_at = _utc_now()
    control_artifact_path = cfg.project_root / 'control' / CONTROL_FILENAME

    reachable = False
    error: str | None = None
    returncode: int | None = None

    try:
        subprocess.run(command, capture_output=True, text=True, timeout=12, check=True)
        reachable = True
    except subprocess.TimeoutExpired as exc:
        error = _truncate_text(f'probe timed out after {exc.timeout} seconds')
    except subprocess.CalledProcessError as exc:
        returncode = exc.returncode
        error = _truncate_text(exc.stderr or exc.output or str(exc)) or exc.__class__.__name__
    except Exception as exc:  # pragma: no cover - defensive guard for unexpected local failures
        error = _truncate_text(str(exc)) or exc.__class__.__name__

    recommended_next_action = (
        'Proceed with eeepc collection.'
        if reachable
        else 'Treat as a control-plane incident; verify eeepc power/network access, then retry collection.'
    )

    result = {
        'collected_at': collected_at,
        'reachable': reachable,
        'ssh_host': cfg.eeepc_ssh_host,
        'target': cfg.eeepc_ssh_host,
        'error': None if reachable else error,
        'returncode': returncode,
        'recommended_next_action': recommended_next_action,
        'control_artifact_path': str(control_artifact_path),
    }

    if persist:
        _atomic_write_json(control_artifact_path, result)

    return result
