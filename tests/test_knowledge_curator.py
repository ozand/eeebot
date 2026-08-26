from __future__ import annotations

import gzip
import json
from pathlib import Path

from nanobot.runtime.knowledge_curator import (
    _fact_path,
    clear_staged_manifest,
    lessons_after,
    load_staged_manifest,
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


def test_curator_stages_promotions_not_workspace(tmp_path):
    """#1001 A: run_curation must NOT write the workspace; facts land in staging."""
    _journal(tmp_path, ["L1", "L2"])
    state = tmp_path / "state"
    result = run_curation(tmp_path, state, llm=_llm([
        {"action": "create", "path": "memory/facts/novel.md", "title": "Novel", "content": "# Novel\n\nA fact.", "index_line": "- [Novel](memory/facts/novel.md)", "lesson_id": "L1", "reason": "new"},
        {"action": "duplicate", "lesson_id": "L2", "reason": "already covered"},
    ]))
    assert result["ok"] and result["writes"] == 1
    # Workspace checkout MUST be untouched — #1001 defect A fix
    assert not (tmp_path / "memory" / "facts" / "novel.md").exists(), \
        "run_curation must not write workspace checkout directly"
    # Staged manifest must exist
    manifest = load_staged_manifest(state)
    assert len(manifest) == 1
    assert manifest[0]["path"] == "memory/facts/novel.md"
    assert manifest[0]["action"] == "create"
    # Decisions sidecar records promoted + duplicate
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
    result = run_curation(tmp_path, tmp_path / "state", llm=_llm(decisions), max_writes=3)
    assert result["writes"] == 3
    # Workspace untouched
    assert not (tmp_path / "memory" / "facts" / "0.md").exists()
    assert _fact_path("goals.md") is None
    assert _fact_path("memory/../goals.md") is None


def test_watermark_advances_after_staging_not_on_failure(tmp_path):
    """#1001 B: watermark must advance after staging succeeds; not on exception."""
    _journal(tmp_path, ["L1"])
    state = tmp_path / "state"
    # Successful path: watermark advances after staging
    result = run_curation(tmp_path, state, llm=_llm([
        {"action": "create", "path": "memory/facts/ok.md", "content": "x", "lesson_id": "L1", "reason": "x"},
    ]))
    assert result["ok"]
    wm = json.loads((state / "curator/watermark.json").read_text())
    assert wm["last_processed_id"] == "L1"


def test_staging_failure_leaves_watermark_unmoved(tmp_path):
    """#1001 B: a staging failure must not advance the watermark."""
    _journal(tmp_path, ["L1"])
    state = tmp_path / "state"
    staged_dir = state / "curator" / "staged"
    staged_dir.mkdir(parents=True)
    # Make staged dir a file so _stage_promotions fails on mkdir.
    staged_dir.rmdir()
    staged_dir.write_text("not a dir", encoding="utf-8")
    result = run_curation(tmp_path, state, llm=_llm([
        {"action": "create", "path": "memory/facts/fail.md", "content": "x", "lesson_id": "L1", "reason": "x"},
    ]))
    assert not result["ok"]
    assert not (state / "curator" / "watermark.json").exists()


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


def test_staging_is_idempotent(tmp_path):
    """#1001: running run_curation twice for same lessons produces a merged manifest, not duplicates."""
    _journal(tmp_path, ["L1"])
    state = tmp_path / "state"
    run_curation(tmp_path, state, llm=_llm([
        {"action": "create", "path": "memory/facts/dup.md", "content": "fact", "lesson_id": "L1", "reason": "x"},
    ]))
    manifest_before = load_staged_manifest(state)
    assert len(manifest_before) == 1
    # Second run: watermark skips L1; no new staging.
    run_curation(tmp_path, state, llm=_llm([]))
    manifest_after = load_staged_manifest(state)
    assert len(manifest_after) == 1


def test_clear_staged_manifest_removes_files(tmp_path):
    """#1001: clear_staged_manifest removes payload and manifest files."""
    _journal(tmp_path, ["L1"])
    state = tmp_path / "state"
    run_curation(tmp_path, state, llm=_llm([
        {"action": "create", "path": "memory/facts/x.md", "content": "x", "lesson_id": "L1", "reason": "x"},
    ]))
    staged_dir = state / "curator" / "staged"
    assert (staged_dir / "manifest.json").exists()
    clear_staged_manifest(state)
    assert not (staged_dir / "manifest.json").exists()


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
