"""Regression tests for issue #581.

``run_self_evolving_cycle`` computes the R11 stall record from the
*preliminary* reward signal in ``_build_experiment_snapshot`` (~coordinator.py
line 2351), but the materialization-reward block that follows
(~coordinator.py lines 4906-4930+) rewrites ``experiment["reward_signal"]`` /
``metric_current`` / ``metric_frontier`` / ``outcome`` without recomputing
``experiment["stall"]`` / ``experiment["stop_reason"]``. Because the
preliminary PASS reward (1.0) always beats the previous cycle's penalized
final metric (0.8, see issue #565's metadata-only penalty), ``stall_signal``
always sees "metric advanced" and the no-progress counter (R11) never fires
— idle keep-loops never self-terminate.

These tests exercise the fixed behaviour: the stall/stop_reason recorded on
the experiment (and mirrored onto the report) must be computed from the
*final*, post-upgrade metrics.
"""
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
    """Create a verifiable autoevolve commit inside ``cycle_started_utc..now`` so
    ``_has_concrete_changes`` grants the reward bonus (concrete-change path,
    metric_current=1.2) instead of the metadata-only penalty (0.8).
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


def _seed_materialize_lane_plan(goals_dir: Path) -> None:
    goals_dir.mkdir(parents=True, exist_ok=True)
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


def _seed_approval_gate(approvals_dir: Path, expires_at: datetime) -> None:
    approvals_dir.mkdir(parents=True, exist_ok=True)
    (approvals_dir / 'apply.ok').write_text(
        json.dumps({'expires_at_utc': expires_at.isoformat(), 'ttl_minutes': 60}), encoding='utf-8'
    )


def _seed_previous_experiment(experiments_dir: Path, *, metric_current: float, metric_frontier: float,
                               goal_id: str = "goal-bootstrap", lane_iteration: int = 1,
                               stall: dict | None = None) -> None:
    experiments_dir.mkdir(parents=True, exist_ok=True)
    previous = {
        "schema_version": "experiment-v1",
        "experiment_id": "experiment-cycle-previous",
        "goal_id": goal_id,
        "result_status": "PASS",
        "outcome": "keep",
        "metric_current": metric_current,
        "metric_frontier": metric_frontier,
        "lane_iteration": lane_iteration,
        "stall": stall,
        "stop_reason": "gate_clean",
    }
    (experiments_dir / "latest.json").write_text(json.dumps(previous), encoding='utf-8')


def test_idle_cycle_marks_stalled_after_penalty_downgrade(tmp_path: Path, monkeypatch):
    """Metadata-only materialize cycle (no concrete diff, penalized to 0.8) whose
    previous experiment ended at the same penalized metric must record
    stall.signal == 'verifier_unchanged' — the no-progress signal the live bug
    was permanently masking.
    """
    monkeypatch.setenv('NANOBOT_SOURCE_COMMIT', 'materialized-source-commit')
    state_root = tmp_path / 'state'
    expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    _seed_approval_gate(state_root / 'approvals', expires_at)
    _seed_materialize_lane_plan(state_root / 'goals')
    _seed_previous_experiment(
        state_root / 'experiments',
        metric_current=0.8,
        metric_frontier=0.8,
    )

    execute = AsyncMock(return_value='agent completed bounded work')
    now = expires_at - timedelta(minutes=30)
    # tmp_path is NOT a git repo -> _has_concrete_changes is False -> penalty path (0.8).
    asyncio.run(run_self_evolving_cycle(workspace=tmp_path, tasks='check open tasks', execute_turn=execute, now=now))

    report = _read_json(sorted((state_root / 'reports').glob('evolution-*.json'))[-1])
    assert report['reward_signal']['value'] == 0.8
    assert report['reward_signal']['source'] == 'metadata_only_improvement_penalty'
    assert report['experiment']['outcome'] == 'keep'
    assert report['stall']['signal'] == 'verifier_unchanged'
    assert report['stall']['stalled'] is True
    assert report['stall']['consecutive'] == 1
    # Below R11's threshold (2) on the first stalled cycle -> not yet stopped.
    assert report['stall']['stop'] is False


def test_two_consecutive_idle_cycles_stop_the_lane(tmp_path: Path, monkeypatch):
    """A second consecutive idle (penalized, unchanged-metric) cycle must trip
    the R11 no-progress stop, with a stop_reason that reflects the guard chain.
    """
    monkeypatch.setenv('NANOBOT_SOURCE_COMMIT', 'materialized-source-commit')
    state_root = tmp_path / 'state'
    expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    _seed_approval_gate(state_root / 'approvals', expires_at)
    _seed_materialize_lane_plan(state_root / 'goals')
    # Previous cycle already stalled once (consecutive=1), well below
    # MAX_ITERATIONS_DEFAULT (12) so the no-progress path is what's observed.
    _seed_previous_experiment(
        state_root / 'experiments',
        metric_current=0.8,
        metric_frontier=0.8,
        lane_iteration=2,
        stall={"signal": "verifier_unchanged", "consecutive": 1, "threshold": 2, "stalled": True, "stop": False},
    )

    execute = AsyncMock(return_value='agent completed bounded work')
    now = expires_at - timedelta(minutes=30)
    asyncio.run(run_self_evolving_cycle(workspace=tmp_path, tasks='check open tasks', execute_turn=execute, now=now))

    report = _read_json(sorted((state_root / 'reports').glob('evolution-*.json'))[-1])
    assert report['reward_signal']['value'] == 0.8
    assert report['stall']['consecutive'] == 2
    assert report['stall']['stop'] is True
    assert report['stop_reason'] == 'no_progress'


def test_real_progress_stays_unstalled(tmp_path: Path, monkeypatch):
    """A materialize cycle with a verified concrete change (reward upgraded to
    1.2, beating the previous cycle's 0.8) must not be marked stalled.
    """
    monkeypatch.setenv('NANOBOT_SOURCE_COMMIT', 'materialized-source-commit')
    state_root = tmp_path / 'state'
    expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    _seed_approval_gate(state_root / 'approvals', expires_at)
    _seed_materialize_lane_plan(state_root / 'goals')
    _seed_previous_experiment(
        state_root / 'experiments',
        metric_current=0.8,
        metric_frontier=0.8,
    )

    execute = AsyncMock(return_value='agent completed bounded work')
    now = expires_at - timedelta(minutes=30)
    _commit_autoevolve_change(tmp_path, when=now + timedelta(minutes=1))
    asyncio.run(run_self_evolving_cycle(workspace=tmp_path, tasks='check open tasks', execute_turn=execute, now=now))

    report = _read_json(sorted((state_root / 'reports').glob('evolution-*.json'))[-1])
    assert report['reward_signal']['value'] >= 1.2
    assert report['reward_signal']['source'] == 'materialized_improvement_artifact'
    assert report['stall']['signal'] is None
    assert report['stall']['stalled'] is False


def test_outcome_and_stall_signal_no_longer_disagree(tmp_path: Path, monkeypatch):
    """Regression for the exact inconsistency seen in the live record: outcome
    'keep' recorded alongside a stall.signal of 'discarded_no_keep' (stale from
    the preliminary, pre-upgrade outcome). After the fix, whenever the
    recomputed outcome is 'keep', the recomputed stall signal must be one of
    the values ``stall_signal`` can actually produce for a kept outcome
    (``None`` or ``verifier_unchanged``) — never a discard/blocked signal.
    """
    monkeypatch.setenv('NANOBOT_SOURCE_COMMIT', 'materialized-source-commit')
    state_root = tmp_path / 'state'
    expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    _seed_approval_gate(state_root / 'approvals', expires_at)
    _seed_materialize_lane_plan(state_root / 'goals')
    _seed_previous_experiment(
        state_root / 'experiments',
        metric_current=0.8,
        metric_frontier=0.8,
    )

    execute = AsyncMock(return_value='agent completed bounded work')
    now = expires_at - timedelta(minutes=30)
    asyncio.run(run_self_evolving_cycle(workspace=tmp_path, tasks='check open tasks', execute_turn=execute, now=now))

    experiment = _read_json(state_root / 'experiments' / 'latest.json')
    report = _read_json(sorted((state_root / 'reports').glob('evolution-*.json'))[-1])

    assert experiment['outcome'] == 'keep'
    assert experiment['stall']['signal'] in (None, 'verifier_unchanged')
    # Report and experiment must agree — both are populated from the same
    # (post-recompute) experiment dict.
    assert report['stall'] == experiment['stall']
    assert report['stop_reason'] == experiment['stop_reason']
