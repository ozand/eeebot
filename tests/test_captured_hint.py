from __future__ import annotations

from pathlib import Path

from nanobot.runtime.llm_proposer import build_context, _captured_pattern_hint


def test_captured_hint_requires_repeated_successful_path():
    rows = [
        {"phase": "outcome", "outcome": "success", "files_changed": ["scripts/a.py"]},
        {"phase": "outcome", "outcome": "success", "files_changed": ["scripts/a.py"]},
    ]
    assert "bundle" in _captured_pattern_hint(rows)
    assert _captured_pattern_hint([rows[0]]) == ""


def test_build_context_places_hint_in_protected_tail(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "nanobot.runtime.llm_proposer._load_ledger_rows",
        lambda _state: [
            {"phase": "outcome", "outcome": "success", "files_changed": ["scripts/repeat.py"]},
            {"phase": "outcome", "outcome": "success", "files_changed": ["scripts/repeat.py"]},
        ],
    )
    monkeypatch.setattr("nanobot.runtime.llm_proposer._load_goal_text", lambda _state: "goal")
    context = build_context(tmp_path, None)
    assert "CAPTURED pattern hint" in context
    assert "bundle" in context
