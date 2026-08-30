from __future__ import annotations

import gzip
import json
from pathlib import Path

from nanobot.runtime.knowledge_curator import (
    _ACTION_INDEX_SEGMENTS,
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
        "\n".join(
            f"- id: {i}\n  title: insight {i}\n  approach: use {i}\n  evidence: ['#1094']"
            for i in ids
        ),
        encoding="utf-8",
    )


def _llm(decisions):
    def call(messages, model):
        assert model
        assert "NEW LESSONS" in messages[1]["content"]
        enriched = []
        for decision in decisions:
            item = dict(decision)
            if item.get("action") in {"create", "update"}:
                item.setdefault("support_claim", item.get("content", "")[:200])
            enriched.append(item)
        return json.dumps(enriched)
    return call


def test_tail_expired_cycle_resolves_from_action_index_and_records_source(tmp_path):
    """#1107: durable action index resolves a cycle absent from the ledger tail."""
    _journal(tmp_path, ["L1"])
    state = tmp_path / "state"
    ledger = state / "ledger" / "cycles.jsonl"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(
        "\n".join(json.dumps({"cycle_id": f"cycle-tail-{i}"}) for i in range(200)) + "\n",
        encoding="utf-8",
    )
    index = state / "action_index" / "2026-08-29.jsonl"
    index.parent.mkdir(parents=True)
    index.write_text(json.dumps({
        "cycle_id": "cycle-expired",
        "outcome": "success",
        "actions": ["exec:pytest"],
    }) + "\n", encoding="utf-8")

    result = run_curation(tmp_path, state, llm=_llm([
        {
            "action": "create", "path": "memory/facts/pytest.md",
            "content": "pytest evidence is durable", "lesson_id": "L1",
            "reason": "record test evidence", "evidence": ["cycle-expired"],
            "support_claim": "pytest evidence",
        },
    ]))

    assert result["ok"] and result["writes"] == 1
    row = json.loads((state / "curator/decisions.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert row["decision"] == "promoted"
    assert "evidence source: action_index" in row["reason"]


def test_nonexistent_cycle_still_rejected_with_ledger_tail_reason(tmp_path):
    """#1107: fallback lookup remains fail-closed when no source contains the cycle."""
    _journal(tmp_path, ["L1"])
    state = tmp_path / "state"
    result = run_curation(tmp_path, state, llm=_llm([
        {
            "action": "create", "path": "memory/facts/missing.md",
            "content": "missing cycle must not promote", "lesson_id": "L1",
            "reason": "test rejection", "evidence": ["cycle-does-not-exist"],
            "support_claim": "missing cycle",
        },
    ]))

    assert result["ok"] and result["writes"] == 0
    row = json.loads((state / "curator/decisions.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert row["decision"] == "rejected"
    assert row["reason"] == "evidence ref rejected: cycle_id not in ledger tail: cycle-does-not-exist"


def test_action_index_fallback_opens_only_bounded_newest_segments(tmp_path, monkeypatch):
    """#1107: a large index/archive cannot cause an unbounded fallback scan."""
    import builtins
    import gzip
    from nanobot.runtime.knowledge_curator import _read_action_index_cycle_text

    index_dir = tmp_path / "action_index"
    index_dir.mkdir(parents=True)
    for day in range(100):
        (index_dir / f"2026-01-{day + 1:02d}.jsonl").write_text(
            json.dumps({"cycle_id": f"cycle-{day}"}) + "\n", encoding="utf-8"
        )

    opened: list[str] = []
    real_open = builtins.open
    real_gzip_open = gzip.open

    def tracking_open(file, *args, **kwargs):
        opened.append(str(file))
        return real_open(file, *args, **kwargs)

    def tracking_gzip_open(file, *args, **kwargs):
        opened.append(str(file))
        return real_gzip_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", tracking_open)
    monkeypatch.setattr(gzip, "open", tracking_gzip_open)
    assert _read_action_index_cycle_text(tmp_path, "cycle-0") is None
    assert len(opened) <= _ACTION_INDEX_SEGMENTS


def test_curator_stages_promotions_not_workspace(tmp_path):
    """#1001 A: run_curation must NOT write the workspace; facts land in staging."""
    _journal(tmp_path, ["L1", "L2"])
    state = tmp_path / "state"
    result = run_curation(tmp_path, state, llm=_llm([
        {"action": "create", "path": "memory/facts/novel.md", "title": "Novel", "content": "# Novel\n\nA fact.", "index_line": "- [Novel](memory/facts/novel.md)", "lesson_id": "L1", "reason": "new", "evidence": ["#1094"]},
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
    decisions = [{"action": "create", "path": f"memory/facts/{i}.md", "content": f"fact {i}", "lesson_id": f"L{i}", "reason": "new", "evidence": ["#1094"]} for i in range(4)]
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
        {"action": "create", "path": "memory/facts/ok.md", "content": "x", "lesson_id": "L1", "reason": "x", "evidence": ["#1094"]},
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
        {"action": "create", "path": "memory/facts/fail.md", "content": "x", "lesson_id": "L1", "reason": "x", "evidence": ["#1094"]},
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
        {"action": "create", "path": "memory/facts/dup.md", "content": "fact", "lesson_id": "L1", "reason": "x", "evidence": ["#1094"]},
    ]))
    manifest_before = load_staged_manifest(state)
    assert len(manifest_before) == 1
    # Second run: watermark skips L1; no new staging.
    run_curation(tmp_path, state, llm=_llm([]))
    manifest_after = load_staged_manifest(state)
    assert len(manifest_after) == 1


def test_iter_lessons_ingests_normal_reflector_recommendation_dict_shape(tmp_path):
    """#1041 Part 3: iter_lessons ingests normal {kind, detail, evidence} shape as well as alternate shapes."""
    state_dir = tmp_path / "state"
    reflections_file = state_dir / "reflector" / "reflections.jsonl"
    reflections_file.parent.mkdir(parents=True)

    row = {
        "cycle_id": "cycle-abcdef123456",
        "timestamp": "2026-08-27T07:15:00Z",
        "summary": "Optimize memory allocation in loop workers",
        "recommendations": [
            {
                "kind": "instruction_change",
                "detail": "Clarify that tool workers should use generators rather than full lists",
                "evidence": "Observed 45MB memory spike during large file processing",
            },
            {
                "recommendation": "Alternate legacy format recommendation",
                "actionable_step": "Fallback step text",
            },
        ],
    }
    reflections_file.write_text(json.dumps(row) + "\n", encoding="utf-8")

    entries = lessons_after(tmp_path, "", state_dir=state_dir)
    assert len(entries) == 2
    normal_entry = entries[0]
    assert normal_entry["id"] == "REFL-abcdef123456-0"
    assert "generators rather than full lists" in normal_entry["approach"]
    assert "generators rather than full lists" in normal_entry["reusable_insight"]
    assert "instruction_change" in normal_entry["hypothesis"]
    assert "Observed 45MB memory spike" in normal_entry["result"]

    alt_entry = entries[1]
    assert alt_entry["id"] == "REFL-abcdef123456-1"
    assert "Alternate legacy format" in alt_entry["approach"]
    assert "Fallback step text" in alt_entry["reusable_insight"]


def test_iter_lessons_includes_reflections_as_third_source(tmp_path):
    """#1041 Part 3: reflections.jsonl acts as third source in iter_lessons/lessons_after."""
    state_dir = tmp_path / "state"
    reflections_file = state_dir / "reflector" / "reflections.jsonl"
    reflections_file.parent.mkdir(parents=True)

    row1 = {
        "cycle_id": "cycle-111111111111",
        "timestamp": "2026-08-27T06:00:00Z",
        "recommendations": [
            {
                "recommendation": "Reduce LLM prompt size to avoid OOM",
                "reason": "Bridge cycle timed out on heavy prompt formatting",
                "actionable_step": "Truncate history buffer to 20k chars",
            }
        ],
    }
    row2 = {
        "cycle_id": "cycle-222222222222",
        "timestamp": "2026-08-27T06:30:00Z",
        "status": "consumed",
        "recommendations": [{"recommendation": "consumed reflection"}],
    }
    with reflections_file.open("w", encoding="utf-8") as f:
        f.write(json.dumps(row1) + "\n")
        f.write(json.dumps(row2) + "\n")

    # Regular lessons journal
    _journal(tmp_path, ["L1"])

    entries = lessons_after(tmp_path, "", state_dir=state_dir)
    assert any(e.get("id") == "L1" for e in entries)
    # Reflection source present
    ref_entries = [e for e in entries if e.get("id", "").startswith("REFL-")]
    assert len(ref_entries) == 1
    assert ref_entries[0]["cycle_id"] == "cycle-111111111111"
    assert "Reduce LLM prompt size" in ref_entries[0]["approach"]
    assert "Truncate history buffer" in ref_entries[0]["reusable_insight"]


def test_clear_staged_manifest_removes_files(tmp_path):
    """#1001: clear_staged_manifest removes payload and manifest files."""
    _journal(tmp_path, ["L1"])
    state = tmp_path / "state"
    run_curation(tmp_path, state, llm=_llm([
        {"action": "create", "path": "memory/facts/x.md", "content": "x", "lesson_id": "L1", "reason": "x", "evidence": ["#1094"]},
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


def test_reflections_watermark_does_not_suppress_newer_errors_or_lessons(tmp_path):
    """#1041 reviewer P1: a watermark set from a reflection entry must not
    suppress newer errors or lessons from YAML journals."""
    state_dir = tmp_path / "state"
    reflections_file = state_dir / "reflector" / "reflections.jsonl"
    reflections_file.parent.mkdir(parents=True)

    # Reflection at 06:00
    row1 = {
        "cycle_id": "cycle-111111111111",
        "timestamp": "2026-08-27T06:00:00Z",
        "recommendations": [
            {
                "recommendation": "Earlier reflection recommendation",
                "actionable_step": "Fix early issue",
            }
        ],
    }
    reflections_file.write_text(json.dumps(row1) + "\n", encoding="utf-8")

    # Regular lesson at 05:00 (older than reflection) and 07:00 (newer than reflection)
    lessons_file = tmp_path / "lessons" / "lessons.yaml"
    lessons_file.parent.mkdir(parents=True)
    lessons_file.write_text(
        "lessons:\n"
        "- id: LESS-OLD\n  timestamp: '2026-08-27T05:00:00Z'\n  reusable_insight: old lesson\n"
        "- id: LESS-NEW\n  timestamp: '2026-08-27T07:00:00Z'\n  reusable_insight: newer lesson\n",
        encoding="utf-8",
    )

    # Error at 08:00 (newer than reflection)
    errors_file = tmp_path / "lessons" / "errors.yaml"
    errors_file.write_text(
        "errors:\n"
        "- id: ERR-NEW\n  timestamp: '2026-08-27T08:00:00Z'\n  reusable_insight: newer error\n",
        encoding="utf-8",
    )

    # Watermark set to the reflection ID
    refl_id = "REFL-111111111111-0"
    entries = lessons_after(tmp_path, refl_id, state_dir=state_dir)

    entry_ids = [e.get("id") for e in entries]
    # LESS-OLD should be skipped because it is before the reflection watermark chronologically
    assert "LESS-OLD" not in entry_ids
    # LESS-NEW and ERR-NEW must be present (not suppressed by the reflection watermark)
    assert "LESS-NEW" in entry_ids
    assert "ERR-NEW" in entry_ids
