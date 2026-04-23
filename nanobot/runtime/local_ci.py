from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding='utf-8')


def write_local_ci_result(*, workspace: Path, command: list[str], exit_code: int, output: str, summary: str, now: datetime | None = None) -> dict[str, Any]:
    current = now.astimezone(timezone.utc) if now else datetime.now(timezone.utc)
    stamp = current.isoformat()
    local_ci_dir = workspace.resolve() / 'state' / 'local_ci'
    payload = {
        'schema_version': 'local-ci-result-v1',
        'created_at_utc': stamp,
        'ok': exit_code == 0,
        'exit_code': exit_code,
        'command': command,
        'summary': summary,
        'output_tail': output[-4000:],
    }
    _write_json(local_ci_dir / f'result-{current.strftime("%Y%m%dT%H%M%SZ")}.json', payload)
    _write_json(local_ci_dir / 'latest.json', payload)
    return payload


def write_local_ci_state_summary(*, workspace: Path) -> dict[str, Any]:
    workspace = workspace.resolve()
    local_ci_dir = workspace / 'state' / 'local_ci'
    latest_path = local_ci_dir / 'latest.json'
    latest = json.loads(latest_path.read_text(encoding='utf-8')) if latest_path.exists() else None
    payload = {
        'schema_version': 'local-ci-state-v1',
        'latest_result': latest,
    }
    _write_json(local_ci_dir / 'current_state.json', payload)
    return payload
