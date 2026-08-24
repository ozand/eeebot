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
    assert _captured_pattern_hint([
        {"phase": "proposed", "files_changed": ["scripts/a.py"]},
        {"phase": "outcome", "outcome": "failed", "files_changed": ["scripts/a.py"]},
    ]) == ""
    assert _captured_pattern_hint([
        {"phase": "outcome", "outcome": "success", "files_changed": ["scripts/a.py", "scripts/a.py"]},
    ]) == ""
    assert _captured_pattern_hint([
        {"phase": "outcome", "outcome": "success", "files_changed": ["memory/HISTORY.md", "memory/MEMORY.md"]},
        {"phase": "outcome", "outcome": "success", "files_changed": ["memory/HISTORY.md", "memory/MEMORY.md"]},
    ]) == ""
    assert "bundle" in _captured_pattern_hint([
        {"phase": "outcome", "outcome": "success", "files_changed": ["skills/a/SKILL.md", "surfaces/policy.json"]},
        {"phase": "outcome", "outcome": "success", "files_changed": ["skills/a/SKILL.md", "surfaces/policy.json"]},
    ])
    assert _captured_pattern_hint([
        {"phase": "outcome", "outcome": "success", "files_changed": ["lessons/a.md", "docs/a.md", "tests/a.py"]},
        {"phase": "outcome", "outcome": "success", "files_changed": ["lessons/a.md", "docs/a.md", "tests/a.py"]},
    ]) == ""


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
    assert context.count("bundle this repeated pattern as a skill") == 1
    assert "(bundle this repeated pattern as a skill)" not in context
