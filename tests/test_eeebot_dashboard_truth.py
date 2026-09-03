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
    assert "100.0h" not in rendered
    assert rendered == "context-only (context-only artifact)"


def test_dashboard_documents_live_source_of_truth_decision() -> None:
    source = DASHBOARD_PATH.read_text(encoding="utf-8")

    assert "Source-of-truth decision (#1206)" in source
    assert "retired report/materialized artifacts" in source
    assert "labelled stale or unavailable" in source
