from __future__ import annotations

from pathlib import Path

from tests.test_eeebot_dashboard_truth import DASHBOARD, _render_ready, _write_jsonl


def test_prompt_fit_reader_projects_rung_and_uniform_trim_details(tmp_path: Path) -> None:
    _write_jsonl(tmp_path / "ledger" / "cycles.jsonl", [
        {
            "phase": "system_prompt",
            "cycle_id": "cycle-degraded",
            "chars": 24000,
            "cap": 24000,
            "rung": "uniform_trim",
            "dropped": [],
            "trimmed": [
                {"section": "bootstrap", "chars": 81, "how": "uniform-trim"},
                {"section": "skills_catalogue", "chars": 234, "how": "uniform-trim"},
            ],
        },
    ])

    result = DASHBOARD.scan_prompt_fit_ledger(tmp_path)
    latest = result["latest"]

    assert latest["rung"] == "uniform_trim"
    assert latest["trimmed_count"] == 2
    assert latest["trimmed_chars"] == 315
    assert latest["trimmed_sections"] == ["bootstrap", "skills_catalogue"]
    assert result["rows_with_trims"] == 1

    tile = DASHBOARD.format_prompt_fit_tile(result, "fresh")
    assert tile["prompt_fit_rung"] == "uniform_trim"
    assert tile["prompt_fit_trimmed_count"] == "2"
    assert tile["prompt_fit_trimmed_chars"] == "315"
    assert tile["prompt_fit_trimmed_sections"] == "bootstrap; skills_catalogue"


def test_non_full_rung_is_visible_in_live_dashboard_html(tmp_path: Path, monkeypatch) -> None:
    state_dir = tmp_path / "state"
    (state_dir / "ledger").mkdir(parents=True)
    _write_jsonl(state_dir / "ledger" / "cycles.jsonl", [{
        "phase": "system_prompt",
        "cycle_id": "cycle-degraded",
        "chars": 24000,
        "cap": 24000,
        "rung": "uniform_trim",
        "dropped": [],
        "trimmed": [{"section": "bootstrap", "chars": 14, "how": "uniform-trim"}],
    }])
    monkeypatch.setattr(DASHBOARD, "STATE_DIR", state_dir)
    metrics = DASHBOARD.collect_metrics_uncached()
    html = DASHBOARD.render_html(_render_ready(metrics))

    assert "rung=uniform_trim" in html
    assert "Uniform-trimmed sections / chars:" in html
    assert "1 / 14" in html
    assert "bootstrap" in html
    assert 'data-status="uniform_trim"' in html


def test_rung_fixture_is_non_vacuous_against_an_isolated_renderer_copy(tmp_path: Path, monkeypatch) -> None:
    source = Path(DASHBOARD.__file__).read_text(encoding="utf-8")
    live_badge = (
        '<span class="status-badge" data-status="{prompt_fit_rung_html}">'
        "rung={prompt_fit_rung_html}</span>"
    )
    # A silent no-op replace would make this whole test vacuous the moment the
    # template drifts -- which is exactly what happened once already.
    assert source.count(live_badge) == 1, "rung badge template drifted; update this fixture"
    broken = source.replace(
        live_badge,
        '<span class="status-badge">rung=full</span>',
        1,
    )
    assert broken != source
    broken_path = tmp_path / "broken_dashboard.py"
    broken_path.write_text(broken, encoding="utf-8")

    namespace = {"__file__": str(broken_path), "__name__": "broken_dashboard"}
    exec(compile(broken, str(broken_path), "exec"), namespace)
    state_dir = tmp_path / "state"
    (state_dir / "ledger").mkdir(parents=True)
    _write_jsonl(state_dir / "ledger" / "cycles.jsonl", [{
        "phase": "system_prompt",
        "cycle_id": "cycle-degraded",
        "chars": 24000,
        "cap": 24000,
        "rung": "uniform_trim",
        "dropped": [],
        "trimmed": [{"section": "bootstrap", "chars": 14, "how": "uniform-trim"}],
    }])
    monkeypatch.setattr(DASHBOARD, "STATE_DIR", state_dir)
    metrics = DASHBOARD.collect_metrics_uncached()
    context = DASHBOARD._build_html_context(DASHBOARD.sanitize_public_metrics(_render_ready(metrics)))
    rendered = namespace["_HTML_TEMPLATE"].format_map(context)
    assert "rung=full" in rendered
    assert "rung=uniform_trim" not in rendered


def test_legacy_row_without_rung_reports_trims_unavailable_not_zero(tmp_path: Path) -> None:
    """155 of 282 real system_prompt rows predate #1476 and carry no ``rung``.

    A fabricated "0 trimmed chars" on such a row reads as a measurement of
    something that was never measured -- the same defect class as #1473/#1474.
    """
    _write_jsonl(tmp_path / "ledger" / "cycles.jsonl", [
        {"phase": "system_prompt", "cycle_id": "c-legacy", "chars": 23814, "cap": 24000},
    ])

    tile = DASHBOARD.format_prompt_fit_tile(DASHBOARD.scan_prompt_fit_ledger(tmp_path), "fresh")

    assert tile["prompt_fit_rung"] == "unavailable"
    assert tile["prompt_fit_trimmed_count"] == "unavailable"
    assert tile["prompt_fit_trimmed_chars"] == "unavailable"
    assert tile["prompt_fit_trimmed_sections"] == "unavailable"
    # The drop accounting predates this change and stays as it was.
    assert tile["prompt_fit_dropped_count"] == "0"


def test_row_with_rung_and_no_trims_reports_a_real_zero(tmp_path: Path) -> None:
    """The counterpart: ``rung`` present means trim accounting ran, so zero is
    a genuine zero and must not be masked as unavailable."""
    _write_jsonl(tmp_path / "ledger" / "cycles.jsonl", [
        {
            "phase": "system_prompt", "cycle_id": "c-full", "chars": 23814, "cap": 24000,
            "rung": "full", "dropped": [], "trimmed": [],
        },
    ])

    tile = DASHBOARD.format_prompt_fit_tile(DASHBOARD.scan_prompt_fit_ledger(tmp_path), "fresh")

    assert tile["prompt_fit_rung"] == "full"
    assert tile["prompt_fit_trimmed_count"] == "0"
    assert tile["prompt_fit_trimmed_chars"] == "0"
    assert tile["prompt_fit_trimmed_sections"] == "none"
