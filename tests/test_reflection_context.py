from __future__ import annotations

import gzip
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
    assert len(hints) <= 3 and all(len(x) <= 320 for x in hints)


def _sentences(n: int, stem: str) -> str:
    return " ".join(f"{stem} sentence number {i} keeps the timeout bridge steady." for i in range(n))


def test_long_hint_is_cut_at_a_sentence_boundary_within_320(tmp_path: Path) -> None:
    """#1728 AC4a: a hint longer than the cap keeps whole sentences only."""
    text = _sentences(8, "Approach")  # ~55 chars each, ~440 total
    assert len(text) > 320
    _write(tmp_path / "reflector/reflections.jsonl", [_row(text, 1)])

    hints = build_reflection_hints(tmp_path, "fix timeout bridge", now=datetime.now(timezone.utc))

    assert len(hints) == 1
    hint = hints[0]
    assert len(hint) <= 320 and hint.endswith(".")
    assert text.startswith(hint)
    # The cut is exactly the longest run of whole sentences that fits.
    expected = ""
    for sentence in (s + "." for s in text.rstrip(".").split(". ")):
        candidate = f"{expected} {sentence}".strip()
        if len(candidate) > 320:
            break
        expected = candidate
    assert hint == expected and hint.count(".") == 5


def test_400_char_first_sentence_yields_no_hint(tmp_path: Path) -> None:
    """#1728 AC4b: a first sentence over the cap is dropped, never truncated."""
    text = "timeout bridge " + "very " * 78 + "long sentence."
    assert len(text) > 400 and text.count(".") == 1
    _write(tmp_path / "reflector/reflections.jsonl", [_row(text, 1), _row("short timeout hint.", 0)])

    hints = build_reflection_hints(tmp_path, "fix timeout bridge", now=datetime.now(timezone.utc))

    assert hints == ["short timeout hint."]


def test_every_emitted_hint_ends_at_a_sentence_boundary(tmp_path: Path) -> None:
    """#1728 AC4: across cut and uncut hints, none ends mid-sentence, and a
    path with a dot inside (``not_a_test.py when``) is not a boundary."""
    cut = "Use tests/test_bridge.py rather than not_a_test.py when testing the timeout bridge. " + _sentences(6, "Extra")
    whole = "Prefer the timeout bridge fixture."
    dotted = "Read scripts/x.py when the timeout bridge fails. " + "Then " + "check the fixture " * 30 + "again."
    _write(tmp_path / "reflector/reflections.jsonl", [_row(cut, 1), _row(whole, 0), _row(dotted, 2)])

    hints = build_reflection_hints(tmp_path, "fix timeout bridge", now=datetime.now(timezone.utc))

    assert len(hints) == 3
    for hint in hints:
        assert len(hint) <= 320
        assert hint.endswith(".")
    assert whole in hints
    assert any(h.startswith("Use tests/test_bridge.py rather than not_a_test.py when testing the timeout bridge.") for h in hints)
    assert "Read scripts/x.py when the timeout bridge fails." in hints  # cut at the sentence, not at ``x.py``


def test_render_applies_the_same_sentence_bound() -> None:
    rendered = render_reflection_hints([_sentences(8, "Rendered"), "x" * 400])
    assert rendered.startswith("## Recent reflections")
    lines = rendered.strip().splitlines()[1:]
    assert len(lines) == 1 and lines[0].endswith(".") and len(lines[0]) <= 322


def test_reads_matching_hint_from_rotated_archive(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    ts = now.isoformat().replace("+00:00", "Z")
    archive_row = _row("preserve archive import resolution", kind="approach_hint")
    archive_row.update({"ts": ts, "task_title": "Fix import resolution", "target_path": "scripts/verify_imports.py"})
    archive = tmp_path / "reflector/archive/reflections-2026-09-02.jsonl.gz"
    archive.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(archive, "wt", encoding="utf-8") as fh:
        fh.write(json.dumps(archive_row) + "\n")
    _write(
        tmp_path / "reflector/reflections.jsonl",
        [_row("unrelated live hint", kind="approach_hint"), _row("another live hint", kind="error_pattern")],
    )

    hints = build_reflection_hints(
        tmp_path,
        "Fix import resolution",
        "scripts/verify_imports.py",
        now=now,
    )

    assert "preserve archive import resolution" in hints


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


