from __future__ import annotations

import subprocess
from pathlib import Path

from nanobot_ops_dashboard.config import DEFAULT_PROJECT_ROOT, load_config
from scripts import consume_queued_redispatch_assignments as redispatch_controller
from scripts import consume_stale_execution_incidents as stale_incident_controller
from scripts import consume_stale_execution_next_actions as stale_next_action_controller
from scripts import stale_execution_watchdog as stale_watchdog


CANONICAL_REPO_ROOT = Path(__file__).resolve().parents[3]
DASHBOARD_ROOT = CANONICAL_REPO_ROOT / "ops" / "dashboard"


def test_default_project_root_is_imported_canonical_dashboard_path(monkeypatch):
    for key in ("NANOBOT_DASHBOARD_ROOT", "NANOBOT_DASHBOARD_DB"):
        monkeypatch.delenv(key, raising=False)

    assert DEFAULT_PROJECT_ROOT == DASHBOARD_ROOT
    assert load_config().project_root == DASHBOARD_ROOT
    assert load_config().db_path == DASHBOARD_ROOT / "data" / "dashboard.sqlite3"


def test_runtime_scripts_resolve_root_from_their_own_location():
    for relative in ("scripts/run_web.sh", "scripts/run_collector.sh", "scripts/install_user_units.sh"):
        text = (DASHBOARD_ROOT / relative).read_text()
        assert "/home/ozand/herkoot/Projects/nanobot-ops-dashboard" not in text
        assert "BASH_SOURCE[0]" in text


def test_systemd_units_point_at_canonical_import_path():
    expected_root = str(DASHBOARD_ROOT)

    for unit in sorted((DASHBOARD_ROOT / "systemd").glob("*ops-dashboard-*.service")):
        text = unit.read_text()
        assert f"WorkingDirectory={expected_root}" in text
        assert f"ExecStart={expected_root}/scripts/" in text


def test_stale_execution_controllers_default_to_canonical_dashboard_root():
    expected_control_root = DASHBOARD_ROOT / "control"

    for module in (stale_watchdog, stale_incident_controller, stale_next_action_controller, redispatch_controller):
        assert module.ROOT == DASHBOARD_ROOT

    assert stale_watchdog.ACTIVE_EXECUTION_PATH == expected_control_root / "active_execution.json"
    assert stale_watchdog.QUEUE_PATH == expected_control_root / "execution_queue.json"
    assert stale_incident_controller.ACTIVE_EXECUTION_PATH == expected_control_root / "active_execution.json"
    assert stale_incident_controller.QUEUE_PATH == expected_control_root / "execution_queue.json"
    assert stale_incident_controller.INCIDENT_DIR == expected_control_root / "stale_execution_incidents"
    assert stale_incident_controller.NEXT_ACTION_DIR == expected_control_root / "stale_execution_next_actions"
    assert stale_next_action_controller.ACTIVE_EXECUTION_PATH == expected_control_root / "active_execution.json"
    assert stale_next_action_controller.QUEUE_PATH == expected_control_root / "execution_queue.json"
    assert stale_next_action_controller.NEXT_ACTION_DIR == expected_control_root / "stale_execution_next_actions"
    assert stale_next_action_controller.REDISPATCH_DIR == expected_control_root / "stale_execution_redispatches"
    assert redispatch_controller.ACTIVE_EXECUTION_PATH == expected_control_root / "active_execution.json"
    assert redispatch_controller.QUEUE_PATH == expected_control_root / "execution_queue.json"
    assert redispatch_controller.ASSIGNMENT_DIR == expected_control_root / "execution_assignments"
    assert redispatch_controller.LATEST_ASSIGNMENT_PATH == expected_control_root / "execution_assignment.json"


def test_generated_dashboard_control_artifacts_are_ignored():
    check_paths = [
        'ops/dashboard/control/stale_execution_incidents/20990101T000000Z-runtime-generated.json',
        'ops/dashboard/control/stale_execution_next_actions/20990101T000000Z-runtime-generated.json',
        'ops/dashboard/control/stale_execution_redispatches/20990101T000000Z-runtime-generated.json',
        'ops/dashboard/control/execution_assignments/20990101T000000Z-runtime-generated.json',
    ]
    result = subprocess.run(
        ['git', 'check-ignore', *check_paths],
        cwd=CANONICAL_REPO_ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    ignored = set(result.stdout.splitlines())
    assert ignored == set(check_paths)


def test_checked_in_dashboard_control_baselines_remain_tracked():
    baseline_paths = [
        'ops/dashboard/control/stale_execution_incidents/20260416T114049015519Z-stagnating_on_quality_blocker-goal-44e50921129bf475-var-lib-eeepc-agent-self-evolving-agent-state-reports-evolution-20260416T121151Z.json-no_concrete_change-planner_hardening.json',
        'ops/dashboard/control/stale_execution_next_actions/20260416T114049015519Z-stagnating_on_quality_blocker-goal-44e50921129bf475-var-lib-eeepc-agent-self-evolving-agent-state-reports-evolution-20260416T121151Z.json-no_concrete_change-planner_hardening.json',
        'ops/dashboard/control/stale_execution_redispatches/20260417T022553513622Z-stagnating_on_quality_blocker-goal-44e50921129bf475-var-lib-eeepc-agent-self-evolving-agent-state-reports-evolution-20260416T121151Z.json-no_concrete_change-planner_hardening.json',
        'ops/dashboard/control/execution_assignments/20260417T024647743230Z-stagnating_on_quality_blocker-goal-44e50921129bf475-var-lib-eeepc-agent-self-evolving-agent-state-reports-evolution-20260416T121151Z.json-no_concrete_change-planner_hardening.json',
    ]
    result = subprocess.run(
        ['git', 'ls-files', *baseline_paths],
        cwd=CANONICAL_REPO_ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    tracked = set(result.stdout.splitlines())
    assert tracked == set(baseline_paths)



def test_imported_dashboard_readme_marks_sibling_repo_as_noncanonical():
    readme = (DASHBOARD_ROOT / "README.md").read_text()

    assert "Canonical source" in readme
    assert "ops/dashboard" in readme
    assert "ozand/eeebot" in readme
    assert "not the durable source of truth" in readme


def test_tracked_import_excludes_runtime_and_secret_artifacts():
    result = subprocess.run(
        ["git", "ls-files", "ops/dashboard"],
        cwd=CANONICAL_REPO_ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    tracked_paths = [Path(line) for line in result.stdout.splitlines() if line.strip()]
    assert tracked_paths

    forbidden_parts = {
        ".env",
        ".pytest_cache",
        "__pycache__",
    }
    forbidden_suffixes = {
        ".sqlite",
        ".sqlite3",
        ".sqlite3-shm",
        ".sqlite3-wal",
        ".db",
        ".pyc",
    }
    forbidden_name_fragments = {
        "id_ed25519",
        "private_key",
    }

    for rel in tracked_paths:
        parts = set(rel.parts)
        lower = str(rel).lower()
        assert not (parts & forbidden_parts), rel
        assert not any(lower.endswith(suffix) for suffix in forbidden_suffixes), rel
        assert not any(fragment in lower for fragment in forbidden_name_fragments), rel
