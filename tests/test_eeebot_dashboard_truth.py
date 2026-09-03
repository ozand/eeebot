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
        _health_metrics(report_status="stale", materialized_status="stale")
    )
    by_name = {name: (status, detail) for name, status, detail in dimensions}

    assert by_name["reward"][0] == "WARN"
    assert "source=stale" in by_name["reward"][1]
    assert by_name["gate"][0] == "WARN"
    assert "source=stale" in by_name["gate"][1]


def test_unavailable_artifacts_are_explicitly_non_healthy() -> None:
    dimensions = DASHBOARD._build_health_dimensions(
        _health_metrics(report_status="unavailable", materialized_status="unavailable")
    )
    by_name = {name: (status, detail) for name, status, detail in dimensions}

    assert by_name["reward"][0] == "WARN"
    assert "source=unavailable" in by_name["reward"][1]
    assert by_name["gate"][0] == "WARN"
    assert "source=unavailable" in by_name["gate"][1]


def test_fresh_artifacts_keep_existing_ok_semantics() -> None:
    dimensions = DASHBOARD._build_health_dimensions(
        _health_metrics(report_status="fresh", materialized_status="fresh")
    )
    by_name = {name: (status, detail) for name, status, detail in dimensions}

    assert by_name["reward"][0] == "OK"
    assert by_name["gate"][0] == "OK"


def test_dashboard_documents_live_source_of_truth_decision() -> None:
    source = DASHBOARD_PATH.read_text(encoding="utf-8")

    assert "Source-of-truth decision (#1206)" in source
    assert "retired report/materialized artifacts" in source
    assert "labelled stale or unavailable" in source
