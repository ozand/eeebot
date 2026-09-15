from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nanobot.runtime._io import write_json as _write_json

DEFAULT_TEST_TARGETS: tuple[str, ...] = (
    "tests/test_identity_contract.py",
    "tests/test_mutation_policy.py",
    "tests/test_import_hygiene.py",
    "tests/test_skill_retirement.py",
    "tests/test_knowledge_curator.py",
)


def _resolve_local_ci_dir(*, workspace: Path | None = None, state_dir: Path | None = None) -> Path:
    """Resolve state directory for local CI results."""
    if state_dir is not None:
        return state_dir.resolve() / "local_ci"
    if workspace is not None:
        return workspace.resolve() / "state" / "local_ci"
    raise ValueError("Either state_dir or workspace must be specified")


def write_local_ci_result(
    *,
    workspace: Path | None = None,
    state_dir: Path | None = None,
    command: list[str],
    exit_code: int,
    output: str,
    summary: str,
    now: datetime | None = None,
    wall_seconds: float | None = None,
    targets: list[str] | None = None,
) -> dict[str, Any]:
    current = now.astimezone(timezone.utc) if now else datetime.now(timezone.utc)
    stamp = current.isoformat()
    local_ci_dir = _resolve_local_ci_dir(workspace=workspace, state_dir=state_dir)
    payload: dict[str, Any] = {
        'schema_version': 'local-ci-result-v1',
        'created_at_utc': stamp,
        'ok': exit_code == 0,
        'exit_code': exit_code,
        'command': command,
        'summary': summary,
        'output_tail': output[-4000:],
    }
    if wall_seconds is not None:
        payload['wall_seconds'] = round(wall_seconds, 2)
    if targets is not None:
        payload['targets'] = targets
    _write_json(local_ci_dir / f'result-{current.strftime("%Y%m%dT%H%M%SZ")}.json', payload)
    _write_json(local_ci_dir / 'latest.json', payload)
    return payload


def write_local_ci_state_summary(
    *,
    workspace: Path | None = None,
    state_dir: Path | None = None,
) -> dict[str, Any]:
    local_ci_dir = _resolve_local_ci_dir(workspace=workspace, state_dir=state_dir)
    latest_path = local_ci_dir / 'latest.json'
    latest = json.loads(latest_path.read_text(encoding='utf-8')) if latest_path.exists() else None
    payload = {
        'schema_version': 'local-ci-state-v1',
        'latest_result': latest,
    }
    _write_json(local_ci_dir / 'current_state.json', payload)
    return payload


def run_and_record_local_ci(
    *,
    workspace: Path,
    state_dir: Path | None = None,
    pytest_bin: str = "python",
    test_targets: list[str] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    targets = list(test_targets or DEFAULT_TEST_TARGETS)
    cmd = [
        pytest_bin,
        "-m",
        "pytest",
        *targets,
        "-o",
        "cache_dir=/tmp/pytest_cache",
        "-q",
    ]
    t0 = time.perf_counter()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(workspace),
            capture_output=True,
            text=True,
            check=False,
        )
        wall = time.perf_counter() - t0
        exit_code = proc.returncode
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        combined_output = f"{stdout}\n{stderr}".strip()
        summary_line = stdout.strip().splitlines()[-1] if stdout.strip() else (stderr.strip().splitlines()[-1] if stderr.strip() else "no output")
    except Exception as exc:
        wall = time.perf_counter() - t0
        exit_code = 127
        combined_output = f"Execution failed: {exc}"
        summary_line = f"execution_error: {exc}"

    result_payload = write_local_ci_result(
        workspace=workspace,
        state_dir=state_dir,
        command=cmd,
        exit_code=exit_code,
        output=combined_output,
        summary=summary_line,
        now=now,
        wall_seconds=wall,
        targets=targets,
    )
    write_local_ci_state_summary(workspace=workspace, state_dir=state_dir)
    result_payload["success"] = (exit_code == 0)
    return result_payload
