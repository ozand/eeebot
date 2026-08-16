import asyncio
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

from nanobot.runtime.coordinator import run_self_evolving_cycle


def _read_json(path: Path):
    return json.loads(path.read_text(encoding='utf-8'))


def _git(*args, cwd: Path, env: dict | None = None) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env=env)


def _commit_autoevolve_change(workspace: Path, when: datetime) -> None:
    """Create a verifiable autoevolve commit — the evidence issue #565 now requires
    before the materialize lane can claim the reward bonus / mint a promotion.

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
    env = {
        **__import__("os").environ,
        "GIT_AUTHOR_DATE": commit_iso,
        "GIT_COMMITTER_DATE": commit_iso,
    }
    _git("commit", "-m", "autoevolve: bounded improvement", cwd=workspace, env=env)


def test_materialized_lane_gets_reward_bonus_readiness_and_deeper_budget(tmp_path: Path):
    approvals_dir = tmp_path / 'state' / 'approvals'
    approvals_dir.mkdir(parents=True)
    expires_at = datetime(2026, 4, 15, 13, 0, tzinfo=timezone.utc)
    (approvals_dir / 'apply.ok').write_text(json.dumps({'expires_at_utc': expires_at.isoformat(), 'ttl_minutes': 60}), encoding='utf-8')

    goals_dir = tmp_path / 'state' / 'goals'
    goals_dir.mkdir(parents=True)
    current_payload = {
        'schema_version': 'task-plan-v1',
        'current_task_id': 'materialize-pass-streak-improvement',
        'tasks': [
            {'task_id': 'record-reward', 'title': 'Record cycle reward', 'status': 'pending'},
            {'task_id': 'inspect-pass-streak', 'title': 'Inspect repeated PASS streak for a new bounded improvement', 'status': 'done', 'kind': 'review'},
            {'task_id': 'materialize-pass-streak-improvement', 'title': 'Materialize one concrete bounded improvement from the repeated PASS insight', 'status': 'active', 'kind': 'execution'},
        ],
        'generated_candidates': [
            {'task_id': 'inspect-pass-streak', 'title': 'Inspect repeated PASS streak for a new bounded improvement', 'status': 'done', 'kind': 'review'},
            {'task_id': 'materialize-pass-streak-improvement', 'title': 'Materialize one concrete bounded improvement from the repeated PASS insight', 'status': 'active', 'kind': 'execution'},
        ]
    }
    (goals_dir / 'current.json').write_text(json.dumps(current_payload), encoding='utf-8')

    execute = AsyncMock(return_value='agent completed bounded work')
    now = expires_at - timedelta(minutes=30)
    _commit_autoevolve_change(tmp_path, when=now + timedelta(minutes=1))
    asyncio.run(run_self_evolving_cycle(workspace=tmp_path, tasks='check open tasks', execute_turn=execute, now=now))

    report = _read_json(sorted((tmp_path / 'state' / 'reports').glob('evolution-*.json'))[-1])
    summary = _read_json(tmp_path / 'state' / 'control_plane' / 'current_summary.json')
    assert report['reward_signal']['value'] >= 1.2
    assert report['reward_signal']['source'] == 'materialized_improvement_artifact'
    assert report['budget_used']['tool_calls'] >= 2
    # #853: the coordinator must not self-accept its own promotion candidate —
    # a ready candidate stays pending operator review, not auto-reviewed/accepted.
    assert report['review_status'] == 'ready_for_policy_review'
    assert report['decision'] == 'ready_for_policy_review'
    assert summary['experiment']['review_status'] == 'ready_for_policy_review'
    assert summary['experiment']['decision'] == 'ready_for_policy_review'
    latest = _read_json(tmp_path / 'state' / 'promotions' / 'latest.json')
    candidate_id = latest['promotion_candidate_id']
    assert latest['decision_record'] == 'pending_operator_review_packet'
    assert latest['accepted_record'] is None
    assert not (tmp_path / 'state' / 'promotions' / 'accepted' / f'{candidate_id}.json').exists()
