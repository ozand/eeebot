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
