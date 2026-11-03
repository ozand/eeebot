import json
from pathlib import Path

import pytest

from nanobot.runtime import no_plan_recovery
from tests.test_eeebot_dashboard_truth import DASHBOARD, _mutation_test_request_class


def _state(**overrides):
    return {
        "schema": "no-plan-recovery-v1",
        "consecutive_supply": 0,
        "consecutive_planner": 0,
        "minimal_mode": False,
        "stopped": False,
        "planner_degraded": False,
        "model_supply_degraded": False,
        "deferred_verdict_ids": ["PRIVATE_CANDIDATE"],
        "task": "PRIVATE_TASK",
        **overrides,
    }


def _get(monkeypatch, state_dir: Path, endpoint: str):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("GET must not invoke recovery mutation")

    for name in ("_save_state", "record_outcome", "resume", "defer_or_bound_verdicts"):
        monkeypatch.setattr(no_plan_recovery, name, forbidden)
    monkeypatch.setattr(DASHBOARD, "STATE_DIR", state_dir)
    monkeypatch.setattr(DASHBOARD, "load_json", lambda *_args: {})
    monkeypatch.setattr(DASHBOARD, "load_latest_materialized", lambda: (None, {}))
    monkeypatch.setattr(DASHBOARD, "scan_report_artifacts", lambda: (None, {}, []))
    monkeypatch.setattr(DASHBOARD, "load_host_capabilities", lambda: {})
    monkeypatch.setattr(DASHBOARD, "scan_subagent_tree_stats", lambda: (0, 0, None, 0, None))
    monkeypatch.setattr(DASHBOARD, "scan_all_report_rewards", lambda **_kwargs: [])
    monkeypatch.setattr(DASHBOARD, "scan_prompt_fit_ledger", lambda *_args: {"source_status": "missing"})
    monkeypatch.setattr(DASHBOARD, "scan_skill_fitness", lambda *_args: {"source_status": "missing"})
    monkeypatch.setattr(DASHBOARD, "scan_lessons_corpus", lambda *_args: {"source_status": "missing", "index_status": "missing"})
    monkeypatch.setattr(DASHBOARD, "scan_hypotheses_sources", lambda *_args: {"sources": {}})
    monkeypatch.setattr(DASHBOARD, "scan_integration_class_share", lambda *_args: {"source_status": "missing"})
    monkeypatch.setattr(DASHBOARD, "get_cpu_load", lambda: 0.0)
    monkeypatch.setattr(DASHBOARD, "get_memory_usage_pct", lambda: 0.0)
    monkeypatch.setattr(DASHBOARD, "get_disk_usage_pct", lambda: 0.0)
    monkeypatch.setattr(DASHBOARD, "collect_metrics", DASHBOARD.collect_metrics_uncached)
    state_path = state_dir / "planner" / "no_plan_recovery.json"
    before = state_path.read_bytes() if state_path.exists() else None
    request = _mutation_test_request_class()(endpoint)
    request.do_GET()
    after = state_path.read_bytes() if state_path.exists() else None
    assert after == before
    return request.code, request.wfile.getvalue().decode("utf-8")


@pytest.mark.parametrize(("raw", "status", "label"), [
    (None, "missing", "недоступно"),
    ("{", "malformed", "неверный формат"),
    ("[]", "malformed", "неверный формат"),
    (json.dumps(_state(consecutive_planner=-1)), "malformed", "неверный формат"),
    (json.dumps(_state(consecutive_planner=True)), "malformed", "неверный формат"),
    (json.dumps(_state(stopped="false")), "malformed", "неверный формат"),
    (json.dumps({"schema": "no-plan-recovery-v1"}), "malformed", "неверный формат"),
])
def test_metrics_get_labels_unavailable_and_hostile_state(tmp_path, monkeypatch, raw, status, label):
    source = tmp_path / "planner" / "no_plan_recovery.json"
    source.parent.mkdir()
    if raw is not None:
        source.write_text(raw, encoding="utf-8")
    code, body = _get(monkeypatch, tmp_path, "/api/metrics")
    payload = json.loads(body)
    assert code == 200
    assert payload["no_plan_recovery"]["source_status"] == status
    assert label in payload["no_plan_recovery_text"]
    assert "PRIVATE_" not in body
    assert "PRIVATE_TASK" not in body


@pytest.mark.parametrize(("error", "status"), [(PermissionError, "permission"), (OSError, "unreadable")])
def test_unreadable_and_permission_recovery_get_are_labelled(tmp_path, monkeypatch, error, status):
    path = tmp_path / "planner" / "no_plan_recovery.json"
    path.parent.mkdir()
    path.write_text("{}", encoding="utf-8")
    original = Path.read_text

    def denied(self, *args, **kwargs):
        if self == path:
            raise error("private path detail")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", denied)
    code, body = _get(monkeypatch, tmp_path, "/api/metrics")
    assert code == 200
    assert json.loads(body)["no_plan_recovery"]["source_status"] == status
    assert status in body
    assert "private path detail" not in body


def test_stopped_degraded_normal_are_visible_on_get_and_html(tmp_path, monkeypatch):
    path = tmp_path / "planner" / "no_plan_recovery.json"
    path.parent.mkdir()
    for values, state_text, health in (
        ({"consecutive_planner": 6, "stopped": True}, "остановлен", "CRIT"),
        ({"consecutive_planner": 3, "minimal_mode": True, "planner_degraded": True}, "деградация", "WARN"),
        ({}, "норма", "OK"),
    ):
        path.write_text(json.dumps(_state(**values)), encoding="utf-8")
        code, metrics_body = _get(monkeypatch, tmp_path, "/api/metrics")
        assert code == 200 and state_text in json.loads(metrics_body)["no_plan_recovery_text"]
        assert "PRIVATE_" not in metrics_body and "PRIVATE_TASK" not in metrics_body
        code, health_body = _get(monkeypatch, tmp_path, "/api/health")
        assert code == 200
        assert json.loads(health_body)["dimensions"]["planner_recovery"]["status"] == health
        code, html_body = _get(monkeypatch, tmp_path, "/")
        assert code == 200 and state_text in html_body
        assert 'data-status="valid"' in html_body
        assert "PRIVATE_" not in html_body and "PRIVATE_TASK" not in html_body
        metrics = json.loads(metrics_body)
        dimensions = {name: status for name, status, _ in DASHBOARD._build_health_dimensions(metrics)}
        assert dimensions["planner_recovery"] == health


def test_invalid_utf8_recovery_is_malformed_on_every_get_surface(tmp_path, monkeypatch):
    path = tmp_path / "planner" / "no_plan_recovery.json"
    path.parent.mkdir()
    path.write_bytes(bytes([255]) + b"private payload")
    for endpoint in ("/api/metrics", "/api/health", "/"):
        code, body = _get(monkeypatch, tmp_path, endpoint)
        assert code == 200
        if endpoint == "/api/metrics":
            assert json.loads(body)["no_plan_recovery"]["source_status"] == "malformed"
        elif endpoint == "/api/health":
            assert json.loads(body)["dimensions"]["planner_recovery"]["status"] == "WARN"
        else:
            assert 'data-status="malformed"' in body
            assert "неверный формат" in body
        assert "private payload" not in body
        assert "�" not in body


def test_recovery_badge_severity_is_distinct_from_source_availability(tmp_path, monkeypatch):
    path = tmp_path / "planner" / "no_plan_recovery.json"
    path.parent.mkdir()
    for values, expected_status, health in (
        ({"consecutive_planner": 6, "stopped": True}, "critical", "CRIT"),
        ({"consecutive_planner": 3, "minimal_mode": True, "planner_degraded": True}, "caution", "WARN"),
        ({}, "valid", "OK"),
    ):
        path.write_text(json.dumps(_state(**values)), encoding="utf-8")
        code, metrics_body = _get(monkeypatch, tmp_path, "/api/metrics")
        assert code == 200
        assert json.loads(metrics_body)["no_plan_recovery"]["source_status"] == "valid"
        code, health_body = _get(monkeypatch, tmp_path, "/api/health")
        assert code == 200
        assert json.loads(health_body)["dimensions"]["planner_recovery"]["status"] == health
        code, html_body = _get(monkeypatch, tmp_path, "/")
        assert code == 200
        assert f'data-status="{expected_status}"' in html_body
        assert "PRIVATE_" not in html_body and "PRIVATE_TASK" not in html_body
