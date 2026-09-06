import ast
import importlib.util
import json
from io import BytesIO
from pathlib import Path

DASHBOARD_PATH = Path(__file__).resolve().parents[1] / "scripts" / "eeebot_dashboard.py"
SPEC = importlib.util.spec_from_file_location("eeebot_dashboard", DASHBOARD_PATH)
assert SPEC and SPEC.loader
DASHBOARD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DASHBOARD)


def _render_ready(metrics: dict) -> dict:
    """Fill every key the HTML renderer reads, so a render test asserts on its
    own subject rather than on the fixture's completeness.

    ``_build_html_context`` indexes 45 keys; the semantic fixtures here supply
    20, and the other 25 have nothing to do with what these tests check. Adding
    them by hand means discovering them one ``KeyError`` at a time and then
    drifting the moment the renderer gains a field, so the set is derived from
    the renderer's own ``_HTML_KEY_MAP`` instead. The renderer/builder key
    contract itself is a separate concern, guarded by #1289 — this helper is
    only about not making every render test a fixture-completeness test.
    """
    filled = dict(metrics)
    for key in DASHBOARD._HTML_KEY_MAP.values():
        filled.setdefault(key, "")
    for key in ("host_capability_badges_html", "host_capability_details_html",
                "oldest_stale_request_age"):
        filled.setdefault(key, "")
    return filled


def _health_metrics(*, report_status: str, materialized_status: str) -> dict:
    return {
        "queue_depth": 0,
        "stale_queue_requests": 0,
        "archived_count": 0,
        "last_cleanup_status": "fresh",
        "last_cleanup_recency": "1m ago",
        "host_capability_probe_attention": "host probe current",
        "host_capability_probe": "1m ago (fresh)",
        "host_capability_badges_html": "",
        "host_capability_details_html": "",
        "queue_priority": "normal",
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
        "cycle_progress": {"state": "healthy", "hours_since_last_success": 0.1, "consecutive_non_integrating_cycles": 0, "dominant_reason": None, "threshold_hours": 1.0, "threshold_cycles": 15},
        # Knowledge plane (#1347)
        "prompt_fit_status": "missing",
        "prompt_fit_chars": "unavailable",
        "prompt_fit_headroom": "unavailable",
        "prompt_fit_dropped_count": "unavailable",
        "prompt_fit_dropped_chars": "unavailable",
        "prompt_fit_dropped_sections": "unavailable",
        "prompt_fit_rows_with_drops": "unavailable",
        "prompt_fit_source": {"status": "missing", "age_hours": None, "authoritative": False, "context_only": True},
        "skills_status": "missing",
        "skills_total": "0",
        "skills_distinct_read": "0",
        "skills_reads_in_window": "0",
        "skills_never_read_count": "0",
        "skills_top": "none",
        "skills_source": {"status": "missing", "age_hours": None, "authoritative": False, "context_only": True},
        "lessons_status": "missing",
        "lessons_corpus_size": "missing",
        "lessons_index_status": "missing",
        "lessons_indexed_count": "missing",
        "lessons_source": {"status": "missing", "age_hours": None, "authoritative": False, "context_only": True},
        "hypotheses_sources_text": "durable: missing | lifecycle: missing",
        "hypotheses_answered_lifecycle_count": "unavailable",
        "hypotheses_orphaned_lifecycle_count": "unavailable",
        "hypotheses_lifecycle_keys_text": "unavailable",
        "hypotheses_durable_source": {"status": "missing", "age_hours": None, "authoritative": False, "context_only": True},
        "hypotheses_lifecycle_source": {"status": "missing", "age_hours": None, "authoritative": False, "context_only": True},
    }


def test_cycle_progress_health_dimension_distinguishes_states() -> None:
    base = _health_metrics(report_status="fresh", materialized_status="fresh")
    base["cycle_progress"] = {"state": "stalled", "hours_since_last_success": 6.4, "consecutive_non_integrating_cycles": 26, "dominant_reason": "dirty_tree"}
    dimensions = {name: (status, detail) for name, status, detail in DASHBOARD._build_health_dimensions(base)}
    assert dimensions["cycle_progress"][0] == "CRIT"
    assert "26 non-integrating cycles" in dimensions["cycle_progress"][1]
    base["cycle_progress"] = {"state": "unavailable"}
    dimensions = {name: (status, detail) for name, status, detail in DASHBOARD._build_health_dimensions(base)}
    assert dimensions["cycle_progress"] == ("WARN", "unavailable")
    base["cycle_progress"] = {"state": "no_success_yet"}
    dimensions = {name: (status, detail) for name, status, detail in DASHBOARD._build_health_dimensions(base)}
    assert dimensions["cycle_progress"] == ("WARN", "no success yet; not zero")
    base["cycle_progress"] = {"state": "empty"}
    dimensions = {name: (status, detail) for name, status, detail in DASHBOARD._build_health_dimensions(base)}
    assert dimensions["cycle_progress"] == ("OK", "empty; no outcome events recorded")


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


def test_html_status_badges_expose_semantic_source_metadata() -> None:
    metrics = _health_metrics(report_status="fresh", materialized_status="fresh")
    metrics["approval_gate_source"] = {"status": "unavailable", "authoritative": False, "context_only": True}
    metrics["queue_priority"] = "normal"
    page = DASHBOARD.render_html(_render_ready(metrics))

    assert 'data-status="unavailable"' in page
    assert 'data-authoritative="false"' in page
    assert 'data-context-only="true"' in page
    assert "--state-offline" in page
    assert 'style="background:' not in page


def test_status_tokens_meet_wcag_aa_contrast() -> None:
    def luminance(hex_color: str) -> float:
        channels = [int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    pairs = (
        ("#065f46", "#6ee7b7"),
        ("#92400e", "#fcd34d"),
        ("#7f1d1d", "#fecaca"),
        ("#1e3a8a", "#bfdbfe"),
        ("#166534", "#f8fafc"),
        ("#1d4ed8", "#f8fafc"),
        ("#b91c1c", "#f8fafc"),
    )
    for background, foreground in pairs:
        ratio = (max(luminance(background), luminance(foreground)) + 0.05) / (min(luminance(background), luminance(foreground)) + 0.05)
        assert ratio >= 4.5, (background, foreground, ratio)


def test_rendered_status_and_trend_badge_rules_emitted_exactly_once() -> None:
    html = DASHBOARD.render_html(_render_ready(_health_metrics(
        report_status="fresh",
        materialized_status="fresh",
    )))
    for selector in (
        '.status-badge[data-status="fresh"]',
        '.status-badge[data-status="valid"]',
        '.status-badge[data-status="nominal"]',
        '.status-badge[data-status="stale"]',
        '.status-badge[data-status="caution"]',
        '.status-badge[data-status="malformed"]',
        '.status-badge[data-status="error"]',
        '.status-badge[data-status="failed"]',
        '.status-badge[data-status="critical"]',
        '.status-badge[data-status="unavailable"]',
        '.status-badge[data-status="offline"]',
        '.status-badge[data-status="context-only"]',
        '.trend-badge.trend-good',
        '.trend-badge.trend-neutral',
        '.trend-badge.trend-bad',
    ):
        assert html.count(selector) == 1, f"Expected {selector} exactly once in rendered HTML"


def test_html_context_keys_are_produced_by_metrics_builder(monkeypatch) -> None:
    """Keep direct indexing loud and derive the builder/context contract.

    Presentation keys intentionally use direct indexing: a missing value should
    fail loudly instead of rendering a plausible but incomplete dashboard.
    """
    source = DASHBOARD_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(DASHBOARD_PATH))
    context_fn = next(
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "_build_html_context"
    )
    metrics_param = context_fn.args.args[0].arg
    direct_keys = {
        sub.slice.value
        for sub in ast.walk(context_fn)
        if isinstance(sub, ast.Subscript)
        and isinstance(sub.value, ast.Name)
        and sub.value.id == metrics_param
        and isinstance(sub.slice, ast.Constant)
        and isinstance(sub.slice.value, str)
    }
    dynamic_keys = {
        DASHBOARD._HTML_KEY_MAP[html_key]
        for html_key in DASHBOARD._HTML_ESCAPE_KEYS
    }
    indexed_keys = direct_keys | dynamic_keys
    assert len(dynamic_keys) > len(direct_keys), "dynamic HTML key map must be part of this contract"

    # The builder is deliberately exercised with its normal source adapters
    # patched to a tiny deterministic fixture; this remains the real builder,
    # not a copied list of expected keys.
    monkeypatch.setattr(DASHBOARD, "load_json", lambda *_args: {})
    monkeypatch.setattr(DASHBOARD, "load_latest_materialized", lambda: (None, {}))
    monkeypatch.setattr(DASHBOARD, "scan_report_artifacts", lambda: (None, {}, []))
    monkeypatch.setattr(DASHBOARD, "load_host_capabilities", lambda: {})
    monkeypatch.setattr(DASHBOARD, "scan_subagent_tree_stats", lambda: (0, 0, None, 0, None))
    monkeypatch.setattr(DASHBOARD, "scan_all_report_rewards", lambda **_kwargs: [])
    produced = DASHBOARD.collect_metrics_uncached()
    assert indexed_keys <= produced.keys(), sorted(indexed_keys - produced.keys())


def test_retired_artifact_families_never_scan_or_render_stale(monkeypatch, tmp_path):
    def forbidden(*args, **kwargs):
        raise AssertionError("retired artifact reader accessed filesystem")

    assert not hasattr(DASHBOARD, "REPORTS_DIR")
    assert not hasattr(DASHBOARD, "IMPROVEMENT_DIR")
    monkeypatch.setattr(Path, "glob", forbidden)
    monkeypatch.setattr(DASHBOARD, "load_json", lambda *_args: {})
    monkeypatch.setattr(DASHBOARD, "load_host_capabilities", lambda: {})
    monkeypatch.setattr(DASHBOARD, "scan_subagent_tree_stats", lambda: (0, 0, None, 0, None))
    metrics = DASHBOARD.collect_metrics_uncached()
    for key in ("approval_gate_state", "materialized_status", "concrete_statement", "latest_report_status", "artifact_freshness"):
        assert "retired" in metrics[key], (key, metrics[key])
        assert "stale" not in metrics[key]
    assert metrics["materialized_source"]["status"] == "retired"
    assert metrics["reward_source"]["status"] == "retired"
    assert DASHBOARD.scan_all_report_rewards() == []


def test_materialized_verifier_is_retired_without_reading(monkeypatch, capsys):
    import runpy

    module = runpy.run_path(str(Path(__file__).parents[1] / "scripts/verify_materialized_improvement.py"))
    def forbidden(*args, **kwargs):
        raise AssertionError("retired verifier read an artifact")
    monkeypatch.setattr(Path, "read_text", forbidden)
    assert module["main"](["verify_materialized_improvement.py"]) == 0
    assert "retired (#1312)" in capsys.readouterr().out


def test_source_selection_skips_retired_but_preserves_malformed_live():
    materialized = Path("materialized.json")
    report = Path("report.json")
    assert DASHBOARD.select_artifact_source(
        materialized, {}, report, {"id": "report"}, materialized_retired=True,
    ) == ({"id": "report"}, report, "report")
    assert DASHBOARD.select_artifact_source(
        materialized, {}, report, {"id": "report"},
    ) == ({}, materialized, "materialized")


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
        lambda **_: {
            "archived": 1,
            "paths": [raw_path],
            "skipped": 0,
            "skipped_details": [],
            "archive_dir": "/secret/queue/archive",
        },
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
        lambda **_: {
            "archived": 0,
            "paths": [],
            "skipped": 1,
            "skipped_details": [(raw_path, "permission denied")],
            "archive_dir": "/secret/queue/archive",
        },
    )
    monkeypatch.setattr(DASHBOARD, "update_health_with_cleanup", lambda _count: {})
    monkeypatch.setattr(DASHBOARD, "scan_subagent_tree_stats", lambda: (0, 0, None, 0, None))
    monkeypatch.setattr(DASHBOARD.sys, "argv", ["dashboard", "--cleanup-queue"])

    DASHBOARD.main()
    output = capsys.readouterr().out

    assert raw_path not in output
    assert "path-redacted" in output


def test_cleanup_result_redacts_http_json_paths() -> None:
    result = {
        "archived": 1,
        "paths": ["/secret/queue/request.json"],
        "skipped": 1,
        "skipped_details": [("/secret/queue/blocked.json", "/secret/error.log")],
        "archive_dir": "/secret/queue/archive",
    }
    rendered = DASHBOARD.json.dumps(DASHBOARD._sanitize_cleanup_result(result))
    assert "/secret/" not in rendered
    assert rendered.count("path-redacted") == 4


def test_queue_path_redaction_preserves_action_metadata() -> None:
    assert DASHBOARD._redact_queue_path("archive 1 stale request(s) — oldest 42.0h @ /secret/a.json") == "archive 1 stale request(s) — oldest 42.0h @ path-redacted"
    assert DASHBOARD._redact_queue_path("/secret/a.json (42.0h)") == "path-redacted (42.0h)"
    assert DASHBOARD._redact_queue_path(r"C:\\secret\\a.json") == "path-redacted"
    assert DASHBOARD._redact_queue_path(r"\\secret\\a.json") == "path-redacted"


def test_queue_reference_keeps_raw_internal_value_until_public_sanitization() -> None:
    # The subject is that the raw path survives until sanitization, not how the
    # platform spells a separator: str(Path("/secret/a.json")) is
    # "\secret\a.json" on Windows and "/secret/a.json" elsewhere. Pinning the
    # POSIX form made this a Windows-only failure and silently moved the
    # documented four-failure Windows baseline to five.
    raw_path = Path("/secret/a.json")
    raw_text = str(raw_path)
    reference = DASHBOARD.format_stale_request_reference(42.0, raw_path)
    assert reference == ("42.0h", raw_text)
    sanitized = DASHBOARD.sanitize_public_metrics({
        "oldest_stale_request_path": raw_path,
        "oldest_stale_request_path_text": raw_text,
        "queue_action": f"archive oldest 42.0h @ {reference[1]}",
        "queue_archive_target": f"{reference[1]} (42.0h)",
    })
    assert sanitized["queue_action"].endswith("@ path-redacted")
    assert sanitized["queue_archive_target"] == "path-redacted (42.0h)"


def _mutation_test_request_class():
    from io import BytesIO

    class Request(DASHBOARD.DashboardHTTPRequestHandler):
        def __init__(self, path):
            self.path = path
            # A LAN address, not the loopback the dashboard itself runs on --
            # this is the exact attack surface #1286 is about: a bare GET
            # from anywhere else on the network must not mutate state.
            self.client_address = ("192.0.2.7", 1234)
            self.wfile = BytesIO()
            self.headers_sent = {}

        def send_response(self, code):
            self.code = code

        def send_header(self, key, value):
            self.headers_sent[key] = value

        def end_headers(self):
            pass

        def send_error(self, code, message=None):
            self.code = code

    return Request


def test_get_cleanup_from_non_local_address_does_not_archive(monkeypatch):
    """#1286 acceptance criterion: GET /api/cleanup from a non-local address
    must not archive anything. The fix is unconditional on GET -- it does not
    inspect client_address at all, so this blocks every GET regardless of
    origin, local or not; the non-local address here reproduces the issue's
    literal LAN-attacker scenario, it is not evidence of address-based
    logic in the handler. Drives the handler with a monkeypatched archive
    function -- never the live handler, per the operator's own prohibition
    on using the real /api/cleanup to verify this fix."""
    calls = []
    monkeypatch.setattr(
        DASHBOARD,
        "archive_stale_subagent_requests",
        lambda **kw: calls.append(kw) or {"archived": 0, "paths": [], "skipped": 0},
    )
    request_cls = _mutation_test_request_class()

    for path in ("/api/cleanup", "/api/cleanup?dry_run=true", "/api/cleanup?hours=1"):
        request = request_cls(path)
        request.do_GET()
        assert request.code == 405
        assert request.headers_sent["Allow"] == "POST"

    assert calls == [], "GET must never reach archive_stale_subagent_requests"


def test_get_refresh_host_caps_from_non_local_address_does_not_refresh(monkeypatch):
    """#1286 acceptance criterion, same treatment as /api/cleanup: GET
    /api/refresh-host-caps from a non-local address must not refresh host
    capabilities. Same caveat as the cleanup test above: the guard is
    unconditional on GET, not conditional on client_address."""
    calls = []
    monkeypatch.setattr(
        DASHBOARD, "refresh_host_capabilities", lambda: calls.append("refresh") or {}
    )
    request_cls = _mutation_test_request_class()

    for path in ("/api/refresh-host-caps", "/api/refresh-host-caps?x=1"):
        request = request_cls(path)
        request.do_GET()
        assert request.code == 405
        assert request.headers_sent["Allow"] == "POST"

    assert calls == [], "GET must never reach refresh_host_capabilities"


def test_post_cleanup_parses_previously_dead_hours_and_dry_run_parameters(monkeypatch):
    """The query-parsing block that #1286 found unreachable (route matched
    on exact self.path == "/api/cleanup", so any query string 404'd) is now
    reachable via POST and actually drives hours/dry_run through to the
    archive call."""
    calls = []
    monkeypatch.setattr(
        DASHBOARD,
        "archive_stale_subagent_requests",
        lambda **kw: calls.append(kw) or {"archived": 0, "paths": [], "skipped": 0},
    )
    request_cls = _mutation_test_request_class()

    request = request_cls("/api/cleanup?hours=48&dry_run=true")
    request.do_POST()
    assert request.code == 200
    assert calls == [{"hours": 48, "dry_run": True}]

    for query in ("hours=-1", "hours=no", "dry_run=nope", "hours=1&hours=2"):
        request = request_cls("/api/cleanup?" + query)
        request.do_POST()
        assert request.code == 400
    assert len(calls) == 1, "invalid query options must not reach the archive call"


def test_post_refresh_host_caps_and_unknown_path_behave(monkeypatch):
    calls = []
    monkeypatch.setattr(
        DASHBOARD, "refresh_host_capabilities", lambda: calls.append("refresh") or {}
    )
    request_cls = _mutation_test_request_class()

    request = request_cls("/api/refresh-host-caps")
    request.do_POST()
    assert request.code == 200
    assert calls == ["refresh"]

    request = request_cls("/api/cleanup-extra")
    request.do_POST()
    assert request.code == 404


def test_read_only_routes_are_unaffected_by_the_post_mutation_change(monkeypatch) -> None:
    """#1286 acceptance criterion: the read-only routes still answer exactly
    as before -- same status code, same content-type -- once /api/cleanup and
    /api/refresh-host-caps require POST. Drives the handler, not the source.
    """
    from io import BytesIO

    base_metrics = _render_ready(_health_metrics(report_status="ok", materialized_status="ok"))
    base_metrics["reward_source"] = {"status": "stale", "age_hours": 100.0}
    base_metrics.setdefault("captured_at", "now")
    base_metrics.setdefault("goal", "")
    base_metrics.setdefault("active_task", "")
    monkeypatch.setattr(DASHBOARD, "collect_metrics", lambda: base_metrics)

    class Request(DASHBOARD.DashboardHTTPRequestHandler):
        def __init__(self, path):
            self.path = path
            self.client_address = ("192.0.2.7", 1234)
            self.wfile = BytesIO()
            self.headers_sent = {}

        def send_response(self, code):
            self.code = code

        def send_header(self, key, value):
            self.headers_sent[key] = value

        def end_headers(self):
            pass

    for path, expected_content_type in (
        ("/", "text/html; charset=utf-8"),
        ("/api/metrics", "application/json; charset=utf-8"),
        ("/api/health", "application/json; charset=utf-8"),
        ("/api/health-oneliner", "text/plain; charset=utf-8"),
        ("/api/reward-csv", "text/csv; charset=utf-8"),
        ("/api/top-cycles", "text/plain; charset=utf-8"),
    ):
        request = Request(path)
        request.do_GET()
        assert request.code == 200, f"{path} did not return 200"
        assert request.headers_sent["Content-Type"] == expected_content_type, path
        assert len(request.wfile.getvalue()) > 0, f"{path} returned an empty body"


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


def test_dashboard_renders_cycle_progress_tile_without_ambiguous_zero() -> None:
    metrics = _render_ready(_health_metrics(report_status="fresh", materialized_status="fresh"))
    html = DASHBOARD.render_html(metrics)
    assert 'data-cycle-progress="true"' in html
    assert "Cycle Progress:" in html
    assert "since success" in html or "not zero" in html


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


# ─── Issue #1347: knowledge-plane dashboard tiles ───────────────────────────
# prompt-fit, skills, lessons, hypotheses. Read-only projection of state that
# already exists; every source goes through artifact_status/artifact_metadata
# and reports its own honest status (missing/unreadable/stale/malformed),
# never a fabricated healthy zero.


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def test_prompt_fit_ledger_missing_reports_missing_not_zero(tmp_path: Path) -> None:
    """No ledger/cycles.jsonl at all: the tile must say `missing`, never a
    fabricated `0 dropped` that looks like a healthy, fully-fitting prompt."""
    result = DASHBOARD.scan_prompt_fit_ledger(tmp_path)
    assert result["source_status"] == "missing"
    assert result["latest"] is None


def test_prompt_fit_ledger_reads_latest_row_and_counts_recent_drops(tmp_path: Path) -> None:
    """This is #1347's core claim: a silently truncated system prompt must
    become visible -- latest chars/cap, dropped section names and chars, and
    how many of the recent system_prompt rows carried any drop at all (not
    just whether the LATEST one did)."""
    _write_jsonl(tmp_path / "ledger" / "cycles.jsonl", [
        {"phase": "dedup_decision", "cycle_id": "c0"},  # non system_prompt rows must be skipped
        {
            "phase": "system_prompt", "cycle_id": "c1", "chars": 20000, "cap": 24000,
            "dropped": [{"section": "## A", "chars": 500, "how": "declared-droppable"}],
        },
        {"phase": "system_prompt", "cycle_id": "c2", "chars": 12000, "cap": 24000, "dropped": []},
        {
            "phase": "system_prompt", "cycle_id": "c3", "chars": 23268, "cap": 24000,
            "dropped": [
                {"section": "## B", "chars": 1000, "how": "declared-droppable"},
                {"section": "## C", "chars": 700, "how": "declared-droppable"},
            ],
        },
    ])
    result = DASHBOARD.scan_prompt_fit_ledger(tmp_path)
    assert result["source_status"] == "valid"
    latest = result["latest"]
    assert latest["chars"] == 23268
    assert latest["cap"] == 24000
    assert latest["dropped_count"] == 2
    assert latest["dropped_chars"] == 1700
    assert latest["dropped_sections"] == ["## B", "## C"]
    assert latest["cycle_id"] == "c3"
    # 2 of the 3 system_prompt rows carried a drop (c1 and c3, not c2).
    assert result["rows_considered"] == 3
    assert result["rows_with_drops"] == 2


def test_prompt_fit_ledger_is_a_bounded_tail_not_a_full_file_read(tmp_path: Path) -> None:
    """A ledger far larger than the tail window must not be read in full --
    only the last `limit` lines are considered, same discipline as
    the bounded ledger-tail discipline (deque maxlen, never a full read)."""
    rows = [{"phase": "system_prompt", "cycle_id": f"old-{i}", "chars": 1, "cap": 2, "dropped": []} for i in range(50)]
    rows.append({"phase": "system_prompt", "cycle_id": "newest", "chars": 999, "cap": 1000, "dropped": []})
    _write_jsonl(tmp_path / "ledger" / "cycles.jsonl", rows)
    result = DASHBOARD.scan_prompt_fit_ledger(tmp_path, limit=10)
    assert result["rows_considered"] == 10
    assert result["latest"]["cycle_id"] == "newest"


def test_prompt_fit_ledger_malformed_json_line_does_not_crash(tmp_path: Path) -> None:
    """A corrupt line (partial write, disk full mid-append) must be skipped,
    not raise -- the read stays fail-open. Since a valid system_prompt row
    is still present, the source status stays `valid` and the malformed tail
    line is simply ignored."""
    path = tmp_path / "ledger" / "cycles.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text('{"phase": "system_prompt", "cycle_id": "ok", "chars": 1, "cap": 2, "dropped": []}\nNOT JSON\n', encoding="utf-8")
    result = DASHBOARD.scan_prompt_fit_ledger(tmp_path)
    assert result["source_status"] == "valid"
    assert result["latest"]["cycle_id"] == "ok"


def test_prompt_fit_ledger_malformed_without_valid_rows_reports_malformed(tmp_path: Path) -> None:
    """If the bounded tail contains only malformed lines, report `malformed`,
    not `valid-empty`; the latter means a readable ledger with no prompt-fit
    rows, not corrupt content."""
    path = tmp_path / "ledger" / "cycles.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("NOT JSON\n", encoding="utf-8")

    result = DASHBOARD.scan_prompt_fit_ledger(tmp_path)

    assert result["source_status"] == "malformed"
    assert result["latest"] is None


def test_skills_tile_computes_total_read_and_never_read(tmp_path: Path) -> None:
    """#1347: 62 reads across 10 of 18 skills, run-tests at 40% -- this test
    proves the same shape with a small synthetic fixture: total skill count
    comes from the instance repo's skills/*/SKILL.md, never-read is the
    complement of read skills, not a hardcoded list."""
    state_dir = tmp_path / "state"
    selfevo = tmp_path / "eeebot-self-evolving"
    for name in ("run-tests", "batch_grep", "never-touched"):
        skill_dir = selfevo / "skills" / name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# skill", encoding="utf-8")
    reads_path = state_dir / "skill_fitness" / "reads.json"
    reads_path.parent.mkdir(parents=True)
    reads_path.write_text(json.dumps({
        "schema_version": "skill-fitness-v1",
        "reads": [
            {"skill": "run-tests", "ts": "2026-01-01T00:00:00Z"},
            {"skill": "run-tests", "ts": "2026-01-02T00:00:00Z"},
            {"skill": "batch_grep", "ts": "2026-01-01T00:00:00Z"},
        ],
    }), encoding="utf-8")

    result = DASHBOARD.scan_skill_fitness(state_dir)
    assert result["source_status"] == "valid"
    assert result["total_skills"] == 3
    assert result["distinct_skills_read"] == 2
    assert result["reads_in_window"] == 3
    assert result["never_read_count"] == 1
    assert result["never_read"] == ["never-touched"]
    assert result["top_skills"][0] == ("run-tests", 2)


def test_skills_tile_missing_reads_file_reports_missing(tmp_path: Path) -> None:
    result = DASHBOARD.scan_skill_fitness(tmp_path)
    assert result["source_status"] == "missing"
    assert result["reads_in_window"] == 0


def test_skills_tile_malformed_reads_file_reports_malformed_not_zero_reads(tmp_path: Path) -> None:
    """A corrupt reads.json must say `malformed`, distinguishable from a
    genuinely empty/never-used sidecar (`valid-empty`)."""
    reads_path = tmp_path / "skill_fitness" / "reads.json"
    reads_path.parent.mkdir(parents=True)
    reads_path.write_text("{not json", encoding="utf-8")
    result = DASHBOARD.scan_skill_fitness(tmp_path)
    assert result["source_status"] == "malformed"


def test_lessons_corpus_counts_top_level_md_excluding_readme(tmp_path: Path) -> None:
    """Corpus size is the top-level lessons/*.md population -- excludes
    README.md (not a lesson) and anything in subdirectories (archive/,
    errors/), matching the issue's own measured count of 41."""
    state_dir = tmp_path / "state"
    lessons_dir = tmp_path / "eeebot-self-evolving" / "lessons"
    lessons_dir.mkdir(parents=True)
    (lessons_dir / "README.md").write_text("# readme", encoding="utf-8")
    (lessons_dir / "lesson_one.md").write_text("# one", encoding="utf-8")
    (lessons_dir / "lesson_two.md").write_text("# two", encoding="utf-8")
    (lessons_dir / "errors").mkdir()
    (lessons_dir / "errors" / "ERR-001.md").write_text("# err", encoding="utf-8")

    result = DASHBOARD.scan_lessons_corpus(state_dir)
    assert result["source_status"] == "valid"
    assert result["corpus_size"] == 2


def test_lessons_index_reports_missing_not_zero_before_1343_lands(tmp_path: Path) -> None:
    """This is #1347's explicit acceptance criterion: before lessons/index.md
    exists (#1343, a separate line), the indexed-count field MUST read
    `missing`, never `0` -- a `0` would claim an empty-but-present index,
    which is a different, false, state from no index existing at all."""
    state_dir = tmp_path / "state"
    lessons_dir = tmp_path / "eeebot-self-evolving" / "lessons"
    lessons_dir.mkdir(parents=True)
    (lessons_dir / "lesson_one.md").write_text("# one", encoding="utf-8")

    result = DASHBOARD.scan_lessons_corpus(state_dir)
    assert result["corpus_size"] == 1
    assert result["index_status"] == "missing"
    assert result["indexed_count"] is None
    formatted = DASHBOARD.format_lessons_tile(result, "fresh", "missing")
    assert formatted["lessons_indexed_count"] == "missing"
    assert formatted["lessons_indexed_count"] != "0"


def test_lessons_index_once_present_is_parsed_for_indexed_count(tmp_path: Path) -> None:
    """Once index.md exists, its list-item rows are counted -- format-agnostic
    (bulleted markdown list), so this does not depend on #1343's exact layout."""
    state_dir = tmp_path / "state"
    lessons_dir = tmp_path / "eeebot-self-evolving" / "lessons"
    lessons_dir.mkdir(parents=True)
    (lessons_dir / "lesson_one.md").write_text("# one", encoding="utf-8")
    (lessons_dir / "index.md").write_text("# Lesson Index\n\n- lesson_one.md\n- lesson_two.md\n", encoding="utf-8")

    result = DASHBOARD.scan_lessons_corpus(state_dir)
    assert result["index_status"] == "valid"
    assert result["indexed_count"] == 2


def test_lessons_corpus_missing_instance_repo_reports_missing(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    result = DASHBOARD.scan_lessons_corpus(state_dir)
    assert result["source_status"] == "missing"
    assert result["corpus_size"] is None


def test_hypotheses_sources_report_independent_per_file_status(tmp_path: Path) -> None:
    """One missing/malformed hypothesis source file must not blank out the
    other -- durable.json and lifecycle.json are read and classified
    independently. backlog.json is no longer a source (#1356): a stale one on
    disk is ignored, not reported."""
    hyp_dir = tmp_path / "hypotheses"
    hyp_dir.mkdir()
    (hyp_dir / "durable.json").write_text(json.dumps({
        "entries": [{"a": 1}, {"a": 2}], "updated_at": "2026-09-01T00:00:00Z",
    }), encoding="utf-8")
    (hyp_dir / "backlog.json").write_text("{not json", encoding="utf-8")  # inert leftover
    # lifecycle.json intentionally absent.

    result = DASHBOARD.scan_hypotheses_sources(tmp_path)
    sources = result["sources"]
    assert set(sources) == {"durable", "lifecycle"}
    assert sources["durable"]["source_status"] == "valid"
    assert sources["durable"]["entry_count"] == 2
    assert sources["durable"]["updated_at"] == "2026-09-01T00:00:00Z"
    assert sources["lifecycle"]["source_status"] == "missing"


def test_hypotheses_lifecycle_counts_reused_not_reparsed(tmp_path: Path) -> None:
    """The answered/orphaned count comes from hypothesis_backlog.lifecycle_counts
    (#878's own reader), not a hand-rolled re-parse of the lifecycle schema."""
    hyp_dir = tmp_path / "hypotheses"
    hyp_dir.mkdir()
    (hyp_dir / "lifecycle.json").write_text(json.dumps({
        "entries": {
            "k1": {"status": "answered", "verdict": "supported"},
            "k2": {"status": "active"},
        }
    }), encoding="utf-8")

    result = DASHBOARD.scan_hypotheses_sources(tmp_path)
    assert result["lifecycle_counts"]["answered"] == 1
    assert result["lifecycle_counts"]["active"] == 1


def test_knowledge_plane_tiles_are_wired_into_collect_metrics(tmp_path: Path, monkeypatch) -> None:
    """End-to-end: collect_metrics_uncached() must surface all four tiles'
    fields, with a resolved (fresh/stale/missing) status, never the raw
    internal `valid`/`valid-empty` sentinel leaking into the public field."""
    state_dir = tmp_path / "state"
    for sub in ("ledger", "skill_fitness", "hypotheses"):
        (state_dir / sub).mkdir(parents=True)
    selfevo = tmp_path / "eeebot-self-evolving"
    (selfevo / "skills" / "run-tests").mkdir(parents=True)
    (selfevo / "skills" / "run-tests" / "SKILL.md").write_text("# skill", encoding="utf-8")
    (selfevo / "lessons").mkdir(parents=True)
    (selfevo / "lessons" / "lesson_one.md").write_text("# one", encoding="utf-8")

    _write_jsonl(state_dir / "ledger" / "cycles.jsonl", [
        {"phase": "system_prompt", "cycle_id": "c1", "chars": 23268, "cap": 24000,
         "dropped": [{"section": "## A", "chars": 500, "how": "declared-droppable"}]},
    ])
    (state_dir / "skill_fitness" / "reads.json").write_text(json.dumps({
        "schema_version": "skill-fitness-v1",
        "reads": [{"skill": "run-tests", "ts": "2026-01-01T00:00:00Z"}],
    }), encoding="utf-8")
    (state_dir / "hypotheses" / "durable.json").write_text(json.dumps({
        "entries": [{"a": 1}], "updated_at": "2026-01-01T00:00:00Z",
    }), encoding="utf-8")

    monkeypatch.setattr(DASHBOARD, "STATE_DIR", state_dir)
    metrics = DASHBOARD.collect_metrics_uncached()

    # Never the raw sentinel -- always a resolved published status.
    assert metrics["prompt_fit_status"] in DASHBOARD.ARTIFACT_SOURCE_STATUSES
    assert metrics["skills_status"] in DASHBOARD.ARTIFACT_SOURCE_STATUSES
    assert metrics["lessons_status"] in DASHBOARD.ARTIFACT_SOURCE_STATUSES
    assert metrics["prompt_fit_chars"] == "23268/24000"
    assert metrics["prompt_fit_dropped_count"] == "1"
    assert metrics["prompt_fit_dropped_sections"] == "## A"
    assert metrics["skills_total"] == "1"
    assert metrics["skills_distinct_read"] == "1"
    assert metrics["lessons_corpus_size"] == "1"
    assert metrics["lessons_index_status"] == "missing"
    assert metrics["lessons_indexed_count"] == "missing"
    assert "durable: 1 entries" in metrics["hypotheses_sources_text"]
    assert "backlog" not in metrics["hypotheses_sources_text"]  # #1356

    # Every new source is exposed through artifact_metadata's bounded contract
    # (status/age_hours/authoritative/context_only), the same shape the
    # deploy gate validates -- no parallel status vocabulary.
    for key in (
        "prompt_fit_source", "skills_source", "lessons_source",
        "hypotheses_durable_source", "hypotheses_lifecycle_source",
    ):
        source = metrics[key]
        assert {"status", "age_hours", "authoritative", "context_only"} <= source.keys()
        assert source["authoritative"] is False
        assert source["context_only"] is True
        assert source["status"] in DASHBOARD.ARTIFACT_SOURCE_STATUSES

    # Renders without raising, through every existing surface.
    html_out = DASHBOARD.render_html(metrics)
    assert "Prompt Fit" in html_out
    assert "Skills" in html_out
    assert "Lessons" in html_out
    assert "Hypotheses" in html_out
    json_out = DASHBOARD.render_json(metrics)
    parsed = json.loads(json_out)
    assert parsed["prompt_fit_chars"] == "23268/24000"


def test_knowledge_plane_all_missing_renders_without_raising(tmp_path: Path, monkeypatch) -> None:
    """No ledger, no skill_fitness, no hypotheses, no instance repo at all --
    every tile must report `missing` and the page must still render, never
    crash and never silently report a fabricated healthy zero."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(DASHBOARD, "STATE_DIR", state_dir)
    metrics = DASHBOARD.collect_metrics_uncached()

    assert metrics["prompt_fit_status"] == "missing"
    assert metrics["skills_status"] == "missing"
    assert metrics["lessons_status"] == "missing"
    assert metrics["lessons_indexed_count"] == "missing"
    assert metrics["hypotheses_answered_lifecycle_count"] == "missing"

    html_out = DASHBOARD.render_html(metrics)
    assert "Prompt Fit" in html_out
    json.loads(DASHBOARD.render_json(metrics))  # must not raise


def test_answered_lifecycle_count_never_publishes_a_missing_key_as_zero() -> None:
    """A counts dict that lacks ``answered`` is missing data, not zero answered.

    The empty-dict branch already yielded ``unavailable``; the missing-key
    branch used to yield ``0``, which claims a measurement that was never
    taken. Reviewed on PR #1351.
    """
    absent_key = DASHBOARD.format_hypotheses_tile(
        {"sources": {}, "lifecycle_counts": {"active": 3, "stale": 1}}
    )
    assert absent_key["hypotheses_answered_lifecycle_count"] == "unavailable"

    empty = DASHBOARD.format_hypotheses_tile({"sources": {}, "lifecycle_counts": {}})
    assert empty["hypotheses_answered_lifecycle_count"] == "unavailable"

    present = DASHBOARD.format_hypotheses_tile(
        {"sources": {}, "lifecycle_counts": {"active": 3, "answered": 0}}
    )
    assert present["hypotheses_answered_lifecycle_count"] == "0"


def test_orphaned_count_is_published_under_its_own_name() -> None:
    """#1346: orphaned / total from lifecycle_counts; an absent key is
    unavailable (a pre-#1346 counts dict has no orphan marks), never 0."""
    tile = DASHBOARD.format_hypotheses_tile(
        {"sources": {}, "lifecycle_counts": {"answered": 7}}
    )
    assert tile["hypotheses_orphaned_lifecycle_count"] == "unavailable"
    assert tile["hypotheses_answered_lifecycle_count"] == "7"
    tile = DASHBOARD.format_hypotheses_tile(
        {"sources": {}, "lifecycle_counts": {"answered": 2, "orphaned": 100, "total": 115, "last_pass_recorded": 1}}
    )
    assert tile["hypotheses_orphaned_lifecycle_count"] == "100 of 115"
    # a sidecar no #1346 pass has evaluated yet: zero orphans is "unmeasured", not "none"
    tile = DASHBOARD.format_hypotheses_tile(
        {"sources": {}, "lifecycle_counts": {"answered": 2, "orphaned": 0, "total": 115, "last_pass_recorded": 0}}
    )
    assert tile["hypotheses_orphaned_lifecycle_count"] == "not yet reconciled (115 rows)"
    # the last pass found durable.json unreadable: no zero is published
    tile = DASHBOARD.format_hypotheses_tile(
        {"sources": {}, "lifecycle_counts": {"answered": 2, "orphaned": 0, "total": 115, "last_pass_recorded": 0, "inputs_unavailable": 1}}
    )
    assert tile["hypotheses_orphaned_lifecycle_count"] == "inputs unavailable (115 rows)"
    assert tile["hypotheses_answered_lifecycle_count"] == "2"
    assert tile["hypotheses_lifecycle_keys_text"] == "unavailable"  # no prefix_* keys in this dict
    tile = DASHBOARD.format_hypotheses_tile({"sources": {}, "lifecycle_counts": {
        "answered": 2, "orphaned": 100, "total": 115,
        "prefix_hypothesis": 91, "prefix_hyp": 22, "prefix_slug": 2, "prefix_other": 0,
    }})
    assert tile["hypotheses_lifecycle_keys_text"] == "hypothesis-*: 91 | hyp-*: 22 | slug-*: 2 | other: 0"


def test_lifecycle_counts_disambiguate_missing_file_from_reader_failure() -> None:
    """Issue #1358: a missing source file must NOT be masked as .

    - If lifecycle.json is missing on disk, report  for answered, orphaned, and keys.
    - If lifecycle.json is malformed on disk, report .
    - If lifecycle.json is unreadable on disk, report .
    - If lifecycle.json is valid on disk, but the reader module failed to import
      or was unavailable, report .
    - If lifecycle.json is valid on disk and reader computed counts, report formatted values.
    """
    # 1. File physically absent on disk -> 'missing'
    missing_file = DASHBOARD.format_hypotheses_tile({
        "sources": {"lifecycle": {"source_status": "missing"}},
        "lifecycle_counts": {},
    })
    assert missing_file["hypotheses_answered_lifecycle_count"] == "missing"
    assert missing_file["hypotheses_orphaned_lifecycle_count"] == "missing"
    assert missing_file["hypotheses_lifecycle_keys_text"] == "missing"

    # 2. File malformed on disk -> 'malformed'
    malformed_file = DASHBOARD.format_hypotheses_tile({
        "sources": {"lifecycle": {"source_status": "malformed"}},
        "lifecycle_counts": {},
    })
    assert malformed_file["hypotheses_answered_lifecycle_count"] == "malformed"
    assert malformed_file["hypotheses_orphaned_lifecycle_count"] == "malformed"
    assert malformed_file["hypotheses_lifecycle_keys_text"] == "malformed"

    # 3. File unreadable on disk -> 'unreadable'
    unreadable_file = DASHBOARD.format_hypotheses_tile({
        "sources": {"lifecycle": {"source_status": "unreadable"}},
        "lifecycle_counts": {},
    })
    assert unreadable_file["hypotheses_answered_lifecycle_count"] == "unreadable"
    assert unreadable_file["hypotheses_orphaned_lifecycle_count"] == "unreadable"
    assert unreadable_file["hypotheses_lifecycle_keys_text"] == "unreadable"

    # 4. File valid on disk, but import failed / reader produced empty counts -> 'unavailable'
    valid_file_broken_reader = DASHBOARD.format_hypotheses_tile({
        "sources": {"lifecycle": {"source_status": "valid", "entry_count": 115}},
        "lifecycle_counts": {},
    })
    assert valid_file_broken_reader["hypotheses_answered_lifecycle_count"] == "unavailable"
    assert valid_file_broken_reader["hypotheses_orphaned_lifecycle_count"] == "unavailable"
    assert valid_file_broken_reader["hypotheses_lifecycle_keys_text"] == "unavailable"

    # 5. File valid on disk, reader succeeded -> formatted counts
    valid_file_working_reader = DASHBOARD.format_hypotheses_tile({
        "sources": {"lifecycle": {"source_status": "valid", "entry_count": 115}},
        "lifecycle_counts": {
            "active": 82,
            "answered": 2,
            "orphaned": 100,
            "total": 115,
            "last_pass_recorded": 1,
            "prefix_hypothesis": 91,
            "prefix_hyp": 22,
            "prefix_slug": 2,
            "prefix_other": 0,
        },
    })
    assert valid_file_working_reader["hypotheses_answered_lifecycle_count"] == "2"
    assert valid_file_working_reader["hypotheses_orphaned_lifecycle_count"] == "100 of 115"
    assert valid_file_working_reader["hypotheses_lifecycle_keys_text"] == "hypothesis-*: 91 | hyp-*: 22 | slug-*: 2 | other: 0"


def test_dashboard_systemd_unit_sets_pythonpath_and_bytecode_flags() -> None:
    """Issue #1358: eeebot-dashboard.service in host/eeepc/systemd/ must set
    Environment=PYTHONPATH to the current release directory and
    Environment=PYTHONDONTWRITEBYTECODE=1, matching all other runtime units.
    """
    unit_path = Path(__file__).resolve().parents[1] / "host" / "eeepc" / "systemd" / "eeebot-dashboard.service"
    assert unit_path.is_file(), f"missing unit file {unit_path}"

    content = unit_path.read_text(encoding="utf-8")
    assert "Environment=PYTHONPATH=/opt/eeepc-agent/runtimes/self-evolving-agent/current" in content
    assert "Environment=PYTHONDONTWRITEBYTECODE=1" in content
    assert "WorkingDirectory=/opt/eeepc-agent/runtimes/self-evolving-agent/current" in content
    assert "ExecStart=/opt/eeepc-agent/runtimes/self-evolving-agent/venv/bin/python3 /opt/eeepc-agent/runtimes/self-evolving-agent/current/scripts/eeebot_dashboard.py --serve --port 8080 --host 0.0.0.0" in content
    assert "User=eeepc-agent" in content
    assert "Group=eeepc-agent" in content
