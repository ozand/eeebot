from __future__ import annotations

import json
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
    broken = source.replace(
        '<span class="status-badge {prompt_fit_rung_class}" data-status="{prompt_fit_rung_html}">rung={prompt_fit_rung_html}</span>',
        '<span class="status-badge {prompt_fit_rung_class}">rung=full</span>',
        1,
    )
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
