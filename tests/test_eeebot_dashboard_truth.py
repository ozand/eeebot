import importlib.util
from io import BytesIO
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


def test_source_selection_preserves_present_empty_materialized_source() -> None:
    materialized_path = Path("/tmp/materialized.json")
    report_path = Path("/tmp/report.json")
    source, selected_path, kind = DASHBOARD.select_artifact_source(
        materialized_path, {}, report_path, {"goal_id": "REPORT_SECRET"}
    )
    assert source == {}
    assert selected_path == materialized_path
    assert kind == "materialized"


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
        "goal": "OLD_SECRET_GOAL",
        "goal_source": {"status": "stale", "age_hours": 100.0, "authoritative": False, "context_only": True},
        "active_task": "OLD_SECRET_TASK",
        "active_task_source": {"status": "stale", "age_hours": 100.0, "authoritative": False, "context_only": True},
        "approval_gate_state": "OLD_SECRET_GATE",
        "approval_gate_source": {"status": "stale", "age_hours": 100.0, "authoritative": False, "context_only": True},
        "reward_source": {"status": "stale", "age_hours": 100.0, "authoritative": False, "context_only": True},
        "materialized_source": {"status": "stale", "age_hours": 100.0, "authoritative": False, "context_only": True},
        "reward_average": "OLD_SECRET_REWARD_AVERAGE",
        "reward_momentum": "OLD_SECRET_REWARD_MOMENTUM",
        "reward_range": "OLD_SECRET_REWARD_RANGE",
        "materialized_cycle": "OLD_SECRET_CYCLE",
        "materialized_status": "OLD_SECRET_STATUS",
        "concrete_statement": "OLD_SECRET_STATEMENT",
        "goal_artifact_signature": "OLD_SECRET_SIGNATURE",
        "latest_report_status": "OLD_SECRET_REPORT",
        "next_bounded_candidate": "OLD_SECRET_CANDIDATE",
        "artifact_freshness": "materialized=100.0h ago, report=100.0h ago",
        "host_capability_badges_html": "",
        "host_capability_details_html": "",
        "host_capabilities": [],
        "host_focus_details": [],
        "host_capability_details": [],
        "host_focus_name_set": set(),
        "dashboard_summary": "OLD_SECRET_SUMMARY",
        "focus_line": "OLD_SECRET_FOCUS",
        "operator_attention": "OLD_SECRET_ATTENTION",

        "queue_snapshot": "idle",
        "recent_cycles": "no recent cycles",
        "reward_trend": [("OLD_SECRET_CYCLE", 99.0)],
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
        "reward_distribution": {"count": 1, "mean": 99.0, "median": 99.0, "p10": 99.0, "p95": 99.0, "std_dev": 0.0, "pass_rate": 1.0},
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
        assert "OLD_SECRET" not in rendered
        assert "stale" in rendered
        assert "100.0h" in rendered or "age_hours" in rendered
    assert '"goal_source"' in serialized
    assert '"active_task_source"' in serialized
    assert '"reward_source"' in serialized
    assert '"status": "stale"' in serialized
    assert "stale; age=100.0h" in cli
    assert "stale; age=100.0h" in tui


def test_fresh_report_status_is_still_bounded() -> None:
    metrics = _health_metrics(report_status="fresh", materialized_status="fresh")
    metrics.update({
        "reward_source": {"status": "fresh", "age_hours": 1.0},
        "latest_report_status": "FRESH_SECRET_REPORT",
        "reward_average": "FRESH_SECRET_REWARD",
        "reward_momentum": "FRESH_SECRET_MOMENTUM",
        "reward_range": "FRESH_SECRET_RANGE",
        "reward_trend": [("FRESH_SECRET_CYCLE", 99.0)],
        "sparkline_rewards": [("FRESH_SECRET_CYCLE", 99.0, "PASS")],
        "reward_distribution": {"count": 1, "mean": 99.0, "median": 99.0, "p10": 99.0, "p95": 99.0, "std_dev": 0.0, "pass_rate": 1.0},
    })
    sanitized = DASHBOARD.sanitize_public_metrics(metrics)
    assert sanitized["latest_report_status"] == "fresh; age=1.0h (context-only artifact)"
    for field in ("reward_average", "reward_momentum", "reward_range", "reward_trend", "sparkline_rewards"):
        assert "FRESH_SECRET" not in str(sanitized[field])


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
    assert DASHBOARD.render_watch_diff(old, new) == "bounded diff"
    assert calls == [(old, new)]


def test_reward_exports_are_metadata_only() -> None:
    source = {"status": "fresh", "age_hours": 1.0}
    rows = [("SECRET_CYCLE", 99.0, "PASS")]
    assert DASHBOARD.bounded_reward_export(rows, source) == []
    message = DASHBOARD.reward_export_unavailable(source)
    assert "SECRET_CYCLE" not in message
    assert "99.0" not in message
    assert "unavailable" in message


def test_cli_export_modes_do_not_emit_report_rows(monkeypatch, capsys) -> None:
    monkeypatch.setattr(DASHBOARD, "collect_metrics", lambda: {
        "reward_source": {"status": "fresh", "age_hours": 1.0},
    })
    monkeypatch.setattr(DASHBOARD.sys, "argv", ["dashboard", "--export-csv"])
    DASHBOARD.main()
    csv_output = capsys.readouterr().out
    monkeypatch.setattr(DASHBOARD.sys, "argv", ["dashboard", "--top-cycles"])
    DASHBOARD.main()
    top_output = capsys.readouterr().out
    for output in (csv_output, top_output):
        assert "SECRET_CYCLE" not in output
        assert "99.0" not in output
        assert "unavailable" in output


def test_cleanup_queue_dry_run_redacts_request_paths(monkeypatch, capsys) -> None:
    raw_path = "/secret/queue/request.json"
    monkeypatch.setattr(
        DASHBOARD,
        "archive_stale_subagent_requests",
        lambda **_: {"archived": 1, "paths": [raw_path], "skipped": 0, "skipped_details": []},
    )
    monkeypatch.setattr(DASHBOARD, "scan_subagent_tree_stats", lambda: (0, 0, None, 0, None))
    monkeypatch.setattr(DASHBOARD.sys, "argv", ["dashboard", "--cleanup-queue", "--dry-run"])

    DASHBOARD.main()
    output = capsys.readouterr().out

    assert raw_path not in output
    assert "path-redacted" in output


def test_cleanup_queue_error_paths_are_redacted(monkeypatch, capsys) -> None:
    raw_path = "/secret/queue/request.json"
    monkeypatch.setattr(
        DASHBOARD,
        "archive_stale_subagent_requests",
        lambda **_: {"archived": 0, "paths": [], "skipped": 1, "skipped_details": [(raw_path, "permission denied")]},
    )
    monkeypatch.setattr(DASHBOARD, "update_health_with_cleanup", lambda _count: {})
    monkeypatch.setattr(DASHBOARD, "scan_subagent_tree_stats", lambda: (0, 0, None, 0, None))
    monkeypatch.setattr(DASHBOARD.sys, "argv", ["dashboard", "--cleanup-queue"])

    DASHBOARD.main()
    output = capsys.readouterr().out

    assert raw_path not in output
    assert "path-redacted" in output


def test_queue_path_redaction_preserves_action_metadata() -> None:
    assert DASHBOARD._redact_queue_path("archive 1 stale request(s) — oldest 42.0h @ /secret/a.json") == "archive 1 stale request(s) — oldest 42.0h @ path-redacted"
    assert DASHBOARD._redact_queue_path("/secret/a.json (42.0h)") == "path-redacted (42.0h)"


def test_queue_reference_keeps_raw_internal_value_until_public_sanitization() -> None:
    reference = DASHBOARD.format_stale_request_reference(42.0, Path("/secret/a.json"))
    assert reference == ("42.0h", "/secret/a.json")
    sanitized = DASHBOARD.sanitize_public_metrics({
        "oldest_stale_request_path": Path("/secret/a.json"),
        "oldest_stale_request_path_text": "/secret/a.json",
        "queue_action": f"archive oldest 42.0h @ {reference[1]}",
        "queue_archive_target": f"{reference[1]} (42.0h)",
    })
    assert sanitized["queue_action"].endswith("@ path-redacted")
    assert sanitized["queue_archive_target"] == "path-redacted (42.0h)"


def test_export_endpoints_use_metadata_only(monkeypatch) -> None:
    from io import BytesIO

    monkeypatch.setattr(DASHBOARD, "collect_metrics", lambda: {
        "reward_source": {"status": "stale", "age_hours": 100.0},
    })
    class Request(DASHBOARD.DashboardHTTPRequestHandler):
        def __init__(self, path):
            self.path = path
            self.wfile = BytesIO()
        def send_response(self, code): self.code = code
        def send_header(self, *_args): pass
        def end_headers(self): pass
    for path in ("/api/reward-csv", "/api/top-cycles?top_n=3"):
        request = Request(path)
        request.do_GET()
        output = request.wfile.getvalue().decode()
        assert "SECRET_CYCLE" not in output
        assert "99.0" not in output
        assert "stale" in output


def test_queue_path_is_redacted_across_public_surfaces() -> None:
    metrics = _health_metrics(report_status="stale", materialized_status="stale")
    metrics.update({
        "captured_at": "now",
        "artifact_freshness": "materialized=42.0h, report=100.0h",
        "latest_report_status": "stale",
        "materialized_status": "stale",
        "concrete_statement": "stale",
        "goal_artifact_signature": "stale",
        "next_bounded_candidate": "stale",
        "materialized_cycle": "stale",
        "queue_depth": 10,
        "stale_queue_requests": 1,
        "oldest_stale_age_hours": 42.0,
        "oldest_stale_request_age": "42.0h",
        "oldest_stale_request_path": "/secret/queue/request.json",
        "oldest_stale_request_path_text": "/secret/queue/request.json",
        "queue_action": "archive 1 stale request(s) — oldest 42.0h @ /secret/queue/request.json",
        "queue_archive_target": "/secret/queue/request.json (42.0h)",
        "queue_pressure": "1/10 stale (10%), oldest 42.0h",
        "queue_freshness": "1/10 stale (10%)",
        "queue_priority": "elevated",
        "queue_hygiene": "1/10 stale (10%) · cleanup=1m ago/fresh",
        "queue_health": "last cleanup 0 @ now",
        "last_cleanup_count": 0,
        "last_cleanup_timestamp": "now",
        "last_cleanup_recency": "1m ago",
        "last_cleanup_status": "fresh",
        "queue_snapshot": "1/10 stale · archived=2 · cleanup=1m ago · gate=stale",
        "dashboard_summary": "raw summary",
        "focus_line": "raw focus",
        "operator_attention": "raw attention",
        "host_capability_badges_html": "",
        "host_capability_details_html": "",
        "host_capabilities": [],
        "host_focus_details": [],
        "host_capability_details": [],
        "host_focus_name_set": set(),
        "host_capability_coverage": "5/5",
        "host_focus_status": "all",
        "host_focus_missing": "none",
        "host_capability_probe": "fresh",
        "host_capability_probe_attention": "current",
        "reward_trend": [],
        "reward_distribution": {"count": 0},
        "overall_health": "WARN",
        "health_status": "WARN",
        "materialized_path": None,
        "latest_report_path": None,
        "reward_source": {"status": "stale", "age_hours": 100.0, "authoritative": False, "context_only": True},
    })
    rendered_values = [
        DASHBOARD.render_json(metrics),
        DASHBOARD.render_health(metrics),
        DASHBOARD.render_health_json(metrics),
        DASHBOARD.render_html(metrics),
        DASHBOARD.render_cli(metrics),
        DASHBOARD.render_tui(metrics),
        DASHBOARD.render_oneliner(metrics),
        DASHBOARD.render_health_oneliner(metrics),
        DASHBOARD.render_diff(metrics, {**metrics, "oldest_stale_age_hours": 43.0}),
        DASHBOARD.render_watch_diff(metrics, {**metrics, "oldest_stale_age_hours": 43.0}),
    ]
    snapshot_path = DASHBOARD.write_snapshot(metrics, Path("/tmp/queue-path-redaction-test.txt"))
    rendered_values.append(snapshot_path.read_text(encoding="utf-8"))
    snapshot_path.unlink(missing_ok=True)
    for rendered in rendered_values:
        assert "/secret/queue/request.json" not in rendered
        assert "/secret/queue/" not in rendered
    combined = "\n".join(rendered_values)
    assert "1/10 stale" in combined or "10 pending / 1 stale" in combined or "queue=10/1s" in combined
    assert "42.0h" in combined
    assert "path-redacted" in combined or '"oldest_stale_request_path": null' in combined


def test_html_context_computes_health_when_metrics_omit_derived_fields(monkeypatch) -> None:
    metrics = _health_metrics(report_status="stale", materialized_status="stale")
    metrics.update({
        "captured_at": "now",
        "goal": "bounded goal",
        "active_task": "bounded task",
        "reward_trend": [],
        "reward_distribution": {"count": 0},
        "dashboard_summary": "bounded summary",
        "focus_line": "bounded focus",
        "operator_attention": "bounded attention",
        "queue_snapshot": "1/10 stale",
        "queue_freshness": "1/10 stale",
        "queue_pressure": "1/10 stale, oldest 42.0h",
        "queue_action": "archive 1 stale request(s) — oldest 42.0h @ path-redacted",
        "queue_archive_target": "path-redacted (42.0h)",
        "queue_priority": "elevated",
        "queue_hygiene": "1/10 stale · cleanup=fresh",
        "oldest_stale_request_age": "42.0h",
        "oldest_stale_request_path_text": "none",
        "oldest_stale_request_path": None,
        "host_capability_badges_html": "",
        "host_capability_details_html": "",
        "host_capabilities": [],
        "host_focus_details": [],
        "host_focus_name_set": set(),
        "host_capability_probe": "current",
        "host_capability_probe_attention": "current",
        "host_focus_missing": "none",
        "host_capability_coverage": "5/5",
        "host_focus_status": "all",
        "host_capability_probe_age": "1m",
        "host_capability_probe_age_hours": 1.0,
        "host_capability_probe_status": "fresh",
        "last_cleanup_count": 0,
        "last_cleanup_timestamp": "now",
        "queue_health": "fresh",
        "materialized_cycle": "none",
        "materialized_status": "stale",
        "concrete_statement": "stale",
        "goal_artifact_signature": "stale",
        "latest_report_status": "stale",
        "next_bounded_candidate": "none",
        "artifact_freshness": "stale",
        "materialized_path": None,
        "latest_report_path": None,
    })
    metrics.pop("overall_health", None)
    metrics.pop("health_status", None)

    html = DASHBOARD.render_html(metrics)

    assert html.startswith("<!DOCTYPE html>")
    assert "KeyError" not in html
    assert "WARN" in html

    monkeypatch.setattr(DASHBOARD, "collect_metrics", lambda: metrics)

    class Request(DASHBOARD.DashboardHTTPRequestHandler):
        def __init__(self):
            self.path = "/"
            self.wfile = BytesIO()

        def send_response(self, code):
            self.code = code

        def send_header(self, *_args):
            pass

        def end_headers(self):
            pass

    for _ in range(3):
        request = Request()
        request.do_GET()
        body = request.wfile.getvalue().decode("utf-8")
        assert request.code == 200
        assert body.startswith("<!DOCTYPE html>")
        assert len(body) > 1000
        assert "KeyError" not in body


def test_dashboard_documents_live_source_of_truth_decision() -> None:
    source = DASHBOARD_PATH.read_text(encoding="utf-8")

    assert "Source-of-truth decision (#1206)" in source
    assert "retired report/materialized artifacts" in source
    assert "labelled stale or unavailable" in source


def test_sanitized_reward_label_is_not_nested_inside_itself() -> None:
    """`sanitize_public_metrics` collapses average, momentum and range to one label.

    When the report family is not authoritative it writes the same
    "stale; age=Nh (context-only artifact)" string into `reward_average` and
    `reward_momentum`. The health detail used to interpolate both, producing the
    label nested in its own parenthetical — observed live on :8080 as
    "stale; age=302.5h (context-only artifact) (stale; age=302.5h (context-only
    artifact)); source=stale". Momentum is only worth printing when it says
    something the average does not.
    """
    label = "stale; age=302.5h (context-only artifact)"
    metrics = _health_metrics(report_status="stale", materialized_status="stale")
    metrics["reward_average"] = label
    metrics["reward_momentum"] = label

    dimensions = DASHBOARD._build_health_dimensions(metrics)
    detail = {name: detail for name, _status, detail in dimensions}["reward"]

    assert detail == f"{label}; source=stale"
    assert detail.count("context-only artifact") == 1, detail
    assert "(stale;" not in detail


def test_distinct_momentum_is_still_shown() -> None:
    """The collapse must not swallow a momentum that carries its own information."""
    metrics = _health_metrics(report_status="fresh", materialized_status="fresh")
    dimensions = DASHBOARD._build_health_dimensions(metrics)
    detail = {name: detail for name, _status, detail in dimensions}["reward"]

    assert "0.88 avg over 5 sample(s)" in detail
    assert "up +0.40 vs previous" in detail
    assert detail == "0.88 avg over 5 sample(s) (up +0.40 vs previous); source=fresh"
