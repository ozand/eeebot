import asyncio
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

from nanobot.runtime.coordinator import run_self_evolving_cycle


def _read_json(path: str | Path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def _git(*args, cwd: Path, env: dict | None = None) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env=env)


def _commit_autoevolve_change(workspace: Path, when: datetime) -> None:
    """Create a verifiable autoevolve commit — the evidence issue #565 now requires
    before the materialize lane can claim a promotion candidate.

    Needs a parent commit first: `git diff-tree` on a root commit (no parent)
    reports no changed files without `--root`, same as the coordinator's check.
    """
    _git("init", cwd=workspace)
    _git("config", "user.email", "test@test.com", cwd=workspace)
    _git("config", "user.name", "Test", cwd=workspace)
    (workspace / "README.md").write_text("init\n")
    _git("add", ".", cwd=workspace)
    _git("commit", "-m", "init", cwd=workspace)

    (workspace / "scripts").mkdir(parents=True, exist_ok=True)
    (workspace / "scripts" / "example_improvement.py").write_text("print('bounded change')\n")
    _git("add", ".", cwd=workspace)
    commit_iso = when.isoformat()
    env = {**os.environ, "GIT_AUTHOR_DATE": commit_iso, "GIT_COMMITTER_DATE": commit_iso}
    _git("commit", "-m", "autoevolve: bounded improvement", cwd=workspace, env=env)


def test_ready_materialized_lane_writes_governance_packet_into_promotion_candidate(tmp_path: Path):
    approvals_dir = tmp_path / 'state' / 'approvals'
    approvals_dir.mkdir(parents=True)
    expires_at = datetime(2026, 4, 18, 12, 0, tzinfo=timezone.utc)
    (approvals_dir / 'apply.ok').write_text(
        json.dumps({'expires_at_utc': expires_at.isoformat(), 'ttl_minutes': 60}),
        encoding='utf-8',
    )

    goals_dir = tmp_path / 'state' / 'goals'
    goals_dir.mkdir(parents=True)
    artifact_path = tmp_path / 'state' / 'improvements' / 'materialized-existing.json'
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(json.dumps({'ok': True}), encoding='utf-8')
    current_payload = {
        'schema_version': 'task-plan-v1',
        'current_task_id': 'materialize-pass-streak-improvement',
        'tasks': [
            {'task_id': 'record-reward', 'title': 'Record cycle reward', 'status': 'pending'},
            {'task_id': 'materialize-pass-streak-improvement', 'title': 'Materialize one concrete bounded improvement from the repeated PASS insight', 'status': 'active', 'kind': 'execution'},
        ],
        'generated_candidates': [
            {'task_id': 'materialize-pass-streak-improvement', 'title': 'Materialize one concrete bounded improvement from the repeated PASS insight', 'status': 'active', 'kind': 'execution'},
        ],
        'materialized_improvement_artifact_path': str(artifact_path),
    }
    (goals_dir / 'current.json').write_text(json.dumps(current_payload), encoding='utf-8')

    now = expires_at - timedelta(minutes=15)
    _commit_autoevolve_change(tmp_path, when=now + timedelta(minutes=1))

    result = asyncio.run(
        run_self_evolving_cycle(
            workspace=tmp_path,
            tasks='prepare candidate',
            execute_turn=AsyncMock(return_value='bounded work complete'),
            now=now,
        )
    )
    assert 'PASS' in result

    latest = _read_json(tmp_path / 'state' / 'promotions' / 'latest.json')
    candidate_id = latest['promotion_candidate_id']
    candidate = _read_json(tmp_path / 'state' / 'promotions' / f'{candidate_id}.json')

    assert candidate['review_status'] == 'reviewed'
    assert candidate['decision'] == 'accept'
    assert candidate['readiness_checks']
    assert candidate['readiness_reasons']
    assert candidate['decision_record'].endswith(f"decisions/{candidate_id}.json")
    assert candidate['accepted_record'].endswith(f"accepted/{candidate_id}.json")
    assert Path(candidate['decision_record']).exists()
    assert Path(candidate['accepted_record']).exists()
    assert candidate['artifact_path']
    assert candidate['governance_packet']
    assert candidate['governance_packet']['review_packet_status'] == 'accepted'
    assert candidate['governance_packet']['source_artifact'] == candidate['artifact_path']
