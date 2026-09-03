import importlib.util
from pathlib import Path

DASHBOARD_PATH = Path(__file__).resolve().parents[1] / "scripts" / "eeebot_dashboard.py"
SPEC = importlib.util.spec_from_file_location("eeebot_dashboard", DASHBOARD_PATH)
assert SPEC and SPEC.loader
DASHBOARD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DASHBOARD)


def _health_metrics(*, report_status: str, materialized_status: str) -> dict:
    return {
        "queue_depth": 0,
        "stale_queue_requests": 0,
        "archived_count": 0,
        "last_cleanup_status": "fresh",
        "last_cleanup_recency": "1m ago",
        "host_capability_probe_attention": "host probe current",
        "host_capability_probe": "1m ago (fresh)",
        "host_focus_missing": "none",
        "host_capability_coverage": "5/5 focus devices available",
        "reward_average": "0.88 avg over 5 sample(s)",
        "reward_momentum": "up +0.40 vs previous",
        "latest_report_artifact_status": report_status,
        "approval_gate_state": "materialize_synthesized_improvement",
        "materialized_artifact_status": materialized_status,
        "cpu_load": 0.1,
        "mem_pct": 20.0,
        "disk_pct": 20.0,
    }


def test_stale_retired_artifacts_are_not_reported_as_healthy() -> None:
    dimensions = DASHBOARD._build_health_dimensions(
        _health_metrics(report_status="context-only", materialized_status="context-only")
    )
    by_name = {name: (status, detail) for name, status, detail in dimensions}

    assert by_name["reward"][0] == "WARN"
    assert "source=context-only" in by_name["reward"][1]
    assert by_name["gate"][0] == "WARN"
    assert "source=context-only" in by_name["gate"][1]


def test_unavailable_artifacts_are_explicitly_non_healthy() -> None:
    dimensions = DASHBOARD._build_health_dimensions(
        _health_metrics(report_status="unavailable", materialized_status="unavailable")
    )
    by_name = {name: (status, detail) for name, status, detail in dimensions}

    assert by_name["reward"][0] == "WARN"
    assert "source=unavailable" in by_name["reward"][1]
    assert by_name["gate"][0] == "WARN"
    assert "source=unavailable" in by_name["gate"][1]


def test_malformed_artifacts_are_not_reported_as_healthy() -> None:
    dimensions = DASHBOARD._build_health_dimensions(
        _health_metrics(report_status="malformed", materialized_status="malformed")
    )
    by_name = {name: (status, detail) for name, status, detail in dimensions}

    assert by_name["reward"][0] == "WARN"
    assert "source=malformed" in by_name["reward"][1]
    assert by_name["gate"][0] == "WARN"
    assert "source=malformed" in by_name["gate"][1]


def test_source_state_distinctions() -> None:
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        missing, missing_status = DASHBOARD.read_json_state(root / "missing.json")
        assert missing is None and missing_status == "missing"
        malformed_path = root / "malformed.json"
        malformed_path.write_text("{", encoding="utf-8")
        _, malformed_status = DASHBOARD.read_json_state(malformed_path)
        assert malformed_status == "malformed"
        empty_path = root / "empty.json"
        empty_path.write_text("{}", encoding="utf-8")
        _, empty_status = DASHBOARD.read_json_state(empty_path)
        assert empty_status == "valid-empty"
        valid_path = root / "valid.json"
        valid_path.write_text('{"value": 1}', encoding="utf-8")
        _, valid_status = DASHBOARD.read_json_state(valid_path)
        assert valid_status == "valid"


def test_missing_and_permission_artifacts_are_not_reported_as_healthy() -> None:
    for status in ("missing", "permission", "unreadable", "valid-empty"):
        dimensions = DASHBOARD._build_health_dimensions(
            _health_metrics(report_status=status, materialized_status=status)
        )
        by_name = {name: (health, detail) for name, health, detail in dimensions}
        assert by_name["reward"][0] == "WARN"
        assert f"source={status}" in by_name["reward"][1]
        assert by_name["gate"][0] == "WARN"
        assert f"source={status}" in by_name["gate"][1]


def test_fresh_artifacts_keep_existing_ok_semantics() -> None:
    dimensions = DASHBOARD._build_health_dimensions(
        _health_metrics(report_status="fresh", materialized_status="fresh")
    )
    by_name = {name: (status, detail) for name, status, detail in dimensions}

    assert by_name["reward"][0] == "OK"
    assert by_name["gate"][0] == "OK"


def test_artifact_value_hides_raw_payload() -> None:
    rendered = DASHBOARD.format_artifact_value(
        "secret task title", 100.0, "valid"
    )
    assert "secret task title" not in rendered
    assert rendered == "stale; age=100.0h (context-only artifact)"


def test_rendered_surfaces_use_status_age_and_hide_payloads() -> None:
    metrics = _health_metrics(report_status="stale", materialized_status="stale")
    metrics.update({
        "captured_at": "now",
        "goal": "stale; age=100.0h (context-only artifact)",
        "goal_source": {"status": "stale", "age_hours": 100.0, "authoritative": False, "context_only": True},
        "active_task": "stale; age=100.0h (context-only artifact)",
        "active_task_source": {"status": "stale", "age_hours": 100.0, "authoritative": False, "context_only": True},
        "approval_gate_state": "stale; age=100.0h (context-only artifact)",
        "approval_gate_source": {"status": "stale", "age_hours": 100.0, "authoritative": False, "context_only": True},
        "reward_source": {"status": "stale", "age_hours": 100.0, "authoritative": False, "context_only": True},
        "materialized_cycle": "context-only artifact",
        "materialized_status": "context-only artifact",
        "concrete_statement": "context-only artifact",
        "goal_artifact_signature": "context-only artifact",
        "latest_report_status": "context-only artifact",
        "next_bounded_candidate": "context-only artifact",
        "artifact_freshness": "materialized=100.0h ago, report=100.0h ago",
        "host_capability_badges_html": "",
        "host_capability_details_html": "",
        "host_capabilities": [],
        "host_focus_details": [],
        "host_capability_details": [],
        "host_focus_name_set": set(),
        "dashboard_summary": "context-only",
        "focus_line": "context-only",
        "operator_attention": "context-only",

        "queue_snapshot": "idle",
        "recent_cycles": "no recent cycles",
        "reward_trend": [],
        "reward_range": "no recent reward samples",
        "queue_freshness": "idle",
        "queue_pressure": "idle",
        "queue_action": "no queue work pending",
        "queue_archive_target": "none",
        "queue_priority": "normal",
        "queue_hygiene": "cleanup=1m ago/fresh",
        "oldest_stale_request_age": "none",
        "oldest_stale_request_path_text": "none",
        "queue_health": "last cleanup 0 @ now",
        "last_cleanup_count": 0,
        "last_cleanup_timestamp": "now",
        "host_focus_status": "all",
        "host_capability_coverage": "5/5",
        "host_focus_missing": "none",
        "host_capability_probe": "fresh",
        "host_capability_probe_attention": "current",
        "cpu_load": 0.1,
        "mem_pct": 1.0,
        "disk_pct": 1.0,
        "reward_distribution": {"count": 0},
        "overall_health": "WARN",
        "health_status": "WARN",
        "materialized_path": "/secret/materialized.json",
        "latest_report_path": "/secret/report.json",
        "oldest_stale_request_path": None,
    })
    serialized = DASHBOARD.render_json(metrics)
    health = DASHBOARD.render_health_json(metrics)
    page = DASHBOARD.render_html(metrics)
    tui = DASHBOARD.render_tui(metrics)
    cli = DASHBOARD.render_cli(metrics)
    oneliner = DASHBOARD.render_oneliner(metrics)
    health_oneliner = DASHBOARD.render_health_oneliner(metrics)
    snapshot_path = DASHBOARD.write_snapshot(metrics, Path("/tmp/eeebot-dashboard-truth-test.txt"))
    snapshot = snapshot_path.read_text(encoding="utf-8")
    snapshot_path.unlink(missing_ok=True)
    for rendered in (serialized, health, page, tui, cli, oneliner, health_oneliner, snapshot):
        assert "/secret/" not in rendered
        assert "secret task title" not in rendered
        assert "stale" in rendered
        assert "100.0h" in rendered or "age_hours" in rendered
    assert '"goal_source"' in serialized
    assert '"active_task_source"' in serialized
    assert '"reward_source"' in serialized
    assert '"status": "stale"' in serialized
    assert "stale; age=100.0h" in cli
    assert "stale; age=100.0h" in tui


def test_diff_hides_stale_reward_and_context_payloads() -> None:
    old = _health_metrics(report_status="stale", materialized_status="stale")
    new = dict(old)
    old.update({
        "goal": "OLD_SECRET_GOAL",
        "active_task": "OLD_SECRET_TASK",
        "approval_gate_state": "OLD_SECRET_GATE",
        "reward_average": "OLD_SECRET_REWARD",
        "reward_momentum": "OLD_SECRET_MOMENTUM",
        "goal_source": {"status": "stale", "age_hours": 100.0},
        "active_task_source": {"status": "stale", "age_hours": 100.0},
        "approval_gate_source": {"status": "stale", "age_hours": 100.0},
        "reward_source": {"status": "stale", "age_hours": 100.0},
    })
    new.update(old)
    new["goal_source"] = {"status": "stale", "age_hours": 101.0}
    rendered = DASHBOARD.render_diff(old, new)
    assert "OLD_SECRET" not in rendered
    assert "stale" in rendered
    assert "101.0h" in rendered


def test_watch_diff_path_uses_bounded_renderer(monkeypatch) -> None:
    calls = []

    def fake_render_diff(old, new):
        calls.append((old, new))
        return "bounded diff"

    monkeypatch.setattr(DASHBOARD, "render_diff", fake_render_diff)
    old = {"goal_source": {"status": "stale", "age_hours": 100.0}}
    new = {"goal_source": {"status": "stale", "age_hours": 101.0}}
    assert DASHBOARD.render_diff(old, new) == "bounded diff"
    assert calls == [(old, new)]


def test_dashboard_documents_live_source_of_truth_decision() -> None:
    source = DASHBOARD_PATH.read_text(encoding="utf-8")

    assert "Source-of-truth decision (#1206)" in source
    assert "retired report/materialized artifacts" in source
    assert "labelled stale or unavailable" in source
