from __future__ import annotations

import gzip
import json
from pathlib import Path

from nanobot.runtime.knowledge_curator import (
    _fact_path,
    lessons_after,
    migrate_loose_lessons,
    run_curation,
)


def _journal(root: Path, ids: list[str]) -> None:
    (root / "lessons").mkdir(parents=True)
    (root / "lessons" / "lessons.yaml").write_text(
        "\n".join(f"- id: {i}\n  title: insight {i}\n  approach: use {i}" for i in ids),
        encoding="utf-8",
    )


def _llm(decisions):
    def call(messages, model):
        assert model
        assert "NEW LESSONS" in messages[1]["content"]
        return json.dumps(decisions)
    return call


def test_curator_promotes_novel_and_journals_duplicate(tmp_path):
    _journal(tmp_path, ["L1", "L2"])
    state = tmp_path / "state"
    result = run_curation(tmp_path, state, llm=_llm([
        {"action": "create", "path": "memory/facts/novel.md", "title": "Novel", "content": "# Novel\n\nA fact.", "index_line": "- [Novel](memory/facts/novel.md)", "lesson_id": "L1", "reason": "new"},
        {"action": "duplicate", "lesson_id": "L2", "reason": "already covered"},
    ]), gate=lambda *_: True)
    assert result["ok"] and result["writes"] == 1
    assert (tmp_path / "memory/facts/novel.md").exists()
    rows = [json.loads(x) for x in (state / "curator/decisions.jsonl").read_text().splitlines()]
    assert {r["decision"] for r in rows} == {"promoted", "duplicate"}


def test_watermark_skips_prior_and_failure_does_not_advance(tmp_path):
    _journal(tmp_path, ["L1", "L2"])
    state = tmp_path / "state"
    state.joinpath("curator").mkdir(parents=True)
    (state / "curator/watermark.json").write_text(json.dumps({"last_processed_id": "L1"}))
    seen = []
    result = run_curation(tmp_path, state, llm=lambda messages, model: seen.append(messages) or "bad")
    assert not result["ok"] and len(seen) == 1
    assert json.loads((state / "curator/watermark.json").read_text())["last_processed_id"] == "L1"


def test_cap_and_delete_or_forbidden_paths_are_rejected(tmp_path):
    _journal(tmp_path, ["L1", "L2", "L3", "L4"])
    decisions = [{"action": "create", "path": f"memory/facts/{i}.md", "content": f"fact {i}", "lesson_id": f"L{i}", "reason": "new"} for i in range(4)]
    decisions.append({"action": "delete", "path": "memory/facts/old.md", "lesson_id": "L4", "reason": "delete"})
    result = run_curation(tmp_path, tmp_path / "state", llm=_llm(decisions), gate=lambda *_: True, max_writes=3)
    assert result["writes"] == 3
    assert not (tmp_path / "memory/facts/old.md").exists()
    assert _fact_path("goals.md") is None
    assert _fact_path("memory/../goals.md") is None


def test_gate_rejects_forbidden_output_and_keeps_watermark(tmp_path):
    _journal(tmp_path, ["L1"])
    state = tmp_path / "state"
    result = run_curation(tmp_path, state, llm=_llm([{"action": "create", "path": "docs/facts/x.md", "content": "x", "lesson_id": "L1", "reason": "x"}]), gate=lambda *_: False)
    assert not result["ok"]
    assert not (state / "curator/watermark.json").exists()


def test_archived_lessons_are_in_watermark_stream(tmp_path):
    _journal(tmp_path, ["L2"])
    archive = tmp_path / "lessons/archive"
    archive.mkdir(parents=True)
    with gzip.open(archive / "lessons-2026-01-01.yaml.gz", "wt", encoding="utf-8") as fh:
        fh.write("- id: L1\n  title: old\n")
    assert [x["id"] for x in lessons_after(tmp_path, "")] == ["L1", "L2"]


def test_sanitary_migration_archives_loose_notes(tmp_path):
    (tmp_path / "lessons").mkdir()
    (tmp_path / "lessons/a.md").write_text("A durable insight", encoding="utf-8")
    (tmp_path / "lessons/b.md").write_text("A durable insight", encoding="utf-8")
    result = migrate_loose_lessons(tmp_path)
    assert result["facts_created"] == 1
    assert not (tmp_path / "lessons/a.md").exists()
    assert (tmp_path / "lessons/archive/loose/a.md").exists()
    assert (tmp_path / "memory/facts/a.md").exists()


def test_missing_litellm_credentials_yields_distinct_error(tmp_path, monkeypatch):
    """#986 first-run incident: with no LITELLM_BASE_URL/LITELLM_API_KEY the
    default LLM path must fail with an actionable credentials error, not the
    misleading 'malformed curator output'."""
    from nanobot.runtime import knowledge_curator as kc
    monkeypatch.delenv("LITELLM_BASE_URL", raising=False)
    monkeypatch.delenv("LITELLM_API_KEY", raising=False)
    workspace = tmp_path / "ws"
    (workspace / "lessons").mkdir(parents=True)
    (workspace / "lessons" / "lessons.yaml").write_text(
        "lessons:\n- id: LESS-1\n  date: '2026-08-25'\n  reusable_insight: x\n",
        encoding="utf-8",
    )
    state = tmp_path / "state"
    result = kc.run_curation(workspace, state)
    assert result["ok"] is False
    assert "credentials not configured" in result["error"]
    assert "malformed" not in result["error"]
