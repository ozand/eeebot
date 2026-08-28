from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from nanobot.runtime.bridge import build_task
from nanobot.runtime.reflection_context import build_reflection_hints, render_reflection_hints


def _write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def _row(text: str, days: int = 0, kind: str = "approach_hint") -> dict:
    ts = datetime.now(timezone.utc) - timedelta(days=days)
    return {"ts": ts.isoformat().replace("+00:00", "Z"), "kind": kind, "task_title": "timeout bridge", "approach_hint": text}


def test_selects_recent_relevant_bounded_hints(tmp_path: Path) -> None:
    _write(tmp_path / "reflector/reflections.jsonl", [_row("old timeout", 8), _row("new timeout approach", 1), _row("other timeout error", 0, "error_pattern")])
    hints = build_reflection_hints(tmp_path, "fix timeout bridge", now=datetime.now(timezone.utc))
    assert hints == ["new timeout approach", "other timeout error"]
    assert len(hints) <= 3 and all(len(x) <= 200 for x in hints)


def test_missing_corrupt_and_expired_are_empty(tmp_path: Path) -> None:
    assert build_reflection_hints(tmp_path, "timeout bridge") == []
    path = tmp_path / "reflector/reflections.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("not json\n", encoding="utf-8")
    assert build_reflection_hints(tmp_path, "timeout bridge") == []
    _write(path, [_row("expired timeout", 8)])
    assert build_reflection_hints(tmp_path, "timeout bridge") == []


def test_prompt_renders_section_only_when_hints_exist(tmp_path: Path) -> None:
    req = {"task_title": "Fix timeout bridge", "request_id": "r", "cycle_id": "c", "goal_id": "g", "source_artifact": "", "lessons_context": {"reflection_hints": ["use bounded timeout"]}}
    prompt = build_task(req, goal_text="goal", report_source="source", state_dir=tmp_path)
    assert "## Recent reflections" in prompt and "use bounded timeout" in prompt
    req["lessons_context"] = {"reflection_hints": []}
    without = build_task(req, goal_text="goal", report_source="source", state_dir=tmp_path)
    assert "## Recent reflections" not in without


def test_render_empty_is_absent() -> None:
    assert render_reflection_hints([]) == ""


def test_extracts_nested_recommendations_and_findings_from_live_journal(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    ts = now.isoformat().replace("+00:00", "Z")
    live_journal_row = {
        "cycle_id": "cycle-2026-08-26-001",
        "created_at": ts,
        "summary": "Evaluation completed with timeout in websocket transport",
        "task_title": "Fix websocket reconnect",
        "target_path": "nanobot/transport/ws.py",
        "findings": [
            {
                "kind": "error_pattern",
                "detail": "Unhandled websocket timeout exception during handshake",
                "evidence": "cycle-2026-08-26-001 traceback",
            },
            {
                "kind": "good_practice",
                "detail": "Cleaned up dangling asyncio sockets on failure",
            },
        ],
        "recommendations": [
            {
                "kind": "approach_hint",
                "detail": "Use asyncio.timeout around the initial websocket handshake",
                "evidence": "tests/test_ws.py",
            },
            {
                "kind": "instruction_change",
                "detail": "Update AGENTS.md with socket teardown guide",
            },
        ],
        "followed_previous": [],
    }
    _write(tmp_path / "reflector/reflections.jsonl", [live_journal_row])
    hints = build_reflection_hints(tmp_path, "websocket handshake timeout", "nanobot/transport/ws.py", now=now)
    assert len(hints) == 2
    assert "Use asyncio.timeout around the initial websocket handshake" in hints
    assert "Unhandled websocket timeout exception during handshake" in hints


def test_dedupes_and_caps_hints(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    ts = now.isoformat().replace("+00:00", "Z")
    row = {
        "cycle_id": "c1",
        "created_at": ts,
        "recommendations": [
            {"kind": "approach_hint", "detail": "shared duplicate hint detail"},
            {"kind": "approach_hint", "detail": "shared duplicate hint detail"},
            {"kind": "approach_hint", "detail": "second unique hint detail"},
            {"kind": "approach_hint", "detail": "third unique hint detail"},
            {"kind": "approach_hint", "detail": "fourth unique hint detail"},
        ],
    }
    _write(tmp_path / "reflector/reflections.jsonl", [row])
    hints = build_reflection_hints(tmp_path, "hint detail task", now=now)
    assert len(hints) == 3
    assert len(set(hints)) == 3
    assert hints[0] == "shared duplicate hint detail"


def test_extracts_nested_explicit_approach_hint_and_error_pattern_keys(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    ts = now.isoformat().replace("+00:00", "Z")
    live_journal_row = {
        "cycle_id": "cycle-2026-08-26-002",
        "created_at": ts,
        "task_title": "Fix memory leak in subscriber",
        "target_path": "nanobot/bus/subscriber.py",
        "findings": [
            {
                "error_pattern": "Unclosed event loop subscription listener",
                "evidence": "traceback in worker log",
            }
        ],
        "recommendations": [
            {
                "approach_hint": "Unsubscribe listener during shutdown handler",
                "evidence": "subscriber lifecycle docs",
            }
        ],
    }
    _write(tmp_path / "reflector/reflections.jsonl", [live_journal_row])
    hints = build_reflection_hints(tmp_path, "subscriber shutdown leak", "nanobot/bus/subscriber.py", now=now)
    assert len(hints) == 2
    assert "Unsubscribe listener during shutdown handler" in hints
    assert "Unclosed event loop subscription listener" in hints


