from __future__ import annotations

import json
from pathlib import Path

from nanobot.runtime import demand
from nanobot.runtime.skill_candidate_mining import (
    _has_legacy_var,
    _is_meaningful,
    mine,
    write_sidecar,
)


def _write_rows(state: Path, rows: list[dict]) -> None:
    path = state / "action_index" / "2026-08-01.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def _rows(count: int = 10) -> list[dict]:
    return [
        {
            "cycle_id": f"cycle-{i}",
            "ts": f"2026-08-{i + 1:02d}T12:00:00Z",
            "actions": ["read:scripts/*.py", "exec:pytest", "edit:scripts/*.py"],
        }
        for i in range(1, count + 1)
    ]


def test_longest_recurrent_ngram_collapses_prefix(tmp_path, monkeypatch):
    monkeypatch.setenv("SELFEVO_SKILL_CANDIDATE_MIN_CYCLES", "8")
    monkeypatch.setenv("SELFEVO_SKILL_CANDIDATE_MIN_DAYS", "3")
    monkeypatch.setenv("SELFEVO_SKILL_CANDIDATE_TOP_N", "10")
    _write_rows(tmp_path, _rows())
    candidates = mine(tmp_path, None)
    assert len(candidates) == 1
    assert candidates[0]["sequence"] == ["read:scripts/*.py", "exec:pytest", "edit:scripts/*.py"]
    assert candidates[0]["cycles"] == 10
    assert candidates[0]["days"] == 10
    assert candidates[0]["samples"] == ["cycle-1", "cycle-2", "cycle-3"]


def test_trivial_and_below_threshold_patterns_are_suppressed(tmp_path, monkeypatch):
    monkeypatch.setenv("SELFEVO_SKILL_CANDIDATE_MIN_CYCLES", "8")
    monkeypatch.setenv("SELFEVO_SKILL_CANDIDATE_MIN_DAYS", "3")
    rows = _rows(7)
    rows.extend({**row, "cycle_id": "trivial-" + row["cycle_id"], "actions": ["exec:pytest", "exec:git-commit"]} for row in _rows(10))
    _write_rows(tmp_path, rows)
    candidates = mine(tmp_path, None)
    assert candidates == []


def test_candidate_enters_demand_and_completed_candidate_is_suppressed(tmp_path, monkeypatch):
    monkeypatch.setenv("SELFEVO_SKILL_CANDIDATE_MIN_CYCLES", "8")
    monkeypatch.setenv("SELFEVO_SKILL_CANDIDATE_MIN_DAYS", "3")
    _write_rows(tmp_path, _rows())
    # F1+F2: write sidecar first, then demand reads it
    write_sidecar(tmp_path, None)
    items = demand._skill_candidate_items(tmp_path, None)
    assert len(items) == 1
    assert items[0]["kind"] == "skill-candidate"
    assert "recurs in 10 distinct cycles" in items[0]["evidence"]
    completed = tmp_path / "demand" / "completed.json"
    completed.parent.mkdir(parents=True, exist_ok=True)
    completed.write_text(json.dumps({"entries": {items[0]["id"]: {"ts": "2026-08-10T00:00:00Z"}}}), encoding="utf-8")
    collected = demand.collect_demand(tmp_path, None)
    assert not any(item["id"] == items[0]["id"] for item in collected)


def test_candidate_kind_is_ordered_before_hypothesis(tmp_path, monkeypatch):
    monkeypatch.setenv("SELFEVO_SKILL_CANDIDATE_MIN_CYCLES", "8")
    monkeypatch.setenv("SELFEVO_SKILL_CANDIDATE_MIN_DAYS", "3")
    _write_rows(tmp_path, _rows())
    # F2: pre-write sidecar so demand path reads it
    write_sidecar(tmp_path, None)
    monkeypatch.setattr(demand, "_priority_items", lambda *_: [])
    monkeypatch.setattr(demand, "_ledger_defects", lambda *_: [])
    monkeypatch.setattr(demand, "_result_file_defects", lambda *_: [])
    monkeypatch.setattr(demand, "_compile_defects", lambda *_: [])
    monkeypatch.setattr(demand, "_heldout_defect_items", lambda *_: [])
    monkeypatch.setattr(demand, "_validator_defect_items", lambda *_: [])
    monkeypatch.setattr(demand, "_tamper_defect_items", lambda *_: [])
    monkeypatch.setattr(demand, "_repair_unused_items", lambda *_: [])
    monkeypatch.setattr(demand, "_goal_gap_items", lambda *_: [{"kind": "goal-gap", "id": "gap", "summary": "gap", "evidence": "", "affected_path": "", "vector": "V1", "direction": ""}])
    monkeypatch.setattr(demand, "_hypothesis_items", lambda *_: [{"kind": "hypothesis", "id": "hyp", "summary": "hyp", "evidence": "", "affected_path": "", "vector": "", "direction": ""}])
    monkeypatch.setattr(demand, "_decay_items", lambda *_: [])
    monkeypatch.setattr(demand, "_reflection_items", lambda *_: [])
    kinds = [item["kind"] for item in demand.collect_demand(tmp_path, None)]
    assert kinds == ["goal-gap", "skill-candidate", "hypothesis"]


def test_existing_skill_suppresses_candidate(tmp_path, monkeypatch):
    monkeypatch.setenv("SELFEVO_SKILL_CANDIDATE_MIN_CYCLES", "8")
    monkeypatch.setenv("SELFEVO_SKILL_CANDIDATE_MIN_DAYS", "3")
    _write_rows(tmp_path, _rows())
    skill = tmp_path / "skills" / "repeat-review" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("# Repeat review\n\nread:scripts/*.py exec:pytest edit:scripts/*.py\n", encoding="utf-8")
    assert mine(tmp_path, tmp_path) == []


def test_no_llm_dependency_and_window_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setenv("SELFEVO_SKILL_CANDIDATE_WINDOW_DAYS", "30")
    source = Path("nanobot/runtime/skill_candidate_mining.py").read_text(encoding="utf-8")
    assert not any(token in source for token in ("openai", "litellm", "LLMProvider"))
    _write_rows(tmp_path, [{**_rows(1)[0], "ts": "2020-01-01T00:00:00Z"}])
    assert mine(tmp_path, None) == []


# ── F1: top-N cap regression ─────────────────────────────────────────────────

def test_f1_top_n_cap_default_three(tmp_path, monkeypatch):
    """F1: write_sidecar caps candidates to top-N (default 3)."""
    monkeypatch.setenv("SELFEVO_SKILL_CANDIDATE_MIN_CYCLES", "8")
    monkeypatch.setenv("SELFEVO_SKILL_CANDIDATE_MIN_DAYS", "3")
    # Produce 4+ distinct qualifying sequences by using 4 different 3-grams
    rows = []
    for i in range(10):
        rows.append({
            "cycle_id": f"cycle-a-{i}",
            "ts": f"2026-08-{i + 1:02d}T12:00:00Z",
            "actions": ["exec:git-log", "exec:grep", "edit:scripts/*.py"],
        })
        rows.append({
            "cycle_id": f"cycle-b-{i}",
            "ts": f"2026-08-{i + 1:02d}T13:00:00Z",
            "actions": ["exec:find", "exec:grep", "write:scripts/*.py"],
        })
        rows.append({
            "cycle_id": f"cycle-c-{i}",
            "ts": f"2026-08-{i + 1:02d}T14:00:00Z",
            "actions": ["exec:python3", "exec:grep", "edit:tests/*.py"],
        })
        rows.append({
            "cycle_id": f"cycle-d-{i}",
            "ts": f"2026-08-{i + 1:02d}T15:00:00Z",
            "actions": ["exec:sed", "exec:grep", "write:tests/*.py"],
        })
    _write_rows(tmp_path, rows)
    summary = write_sidecar(tmp_path, None)
    assert summary["written"] <= 3
    sidecar_data = json.loads((tmp_path / "demand" / "skill_candidates.json").read_text(encoding="utf-8"))
    assert len(sidecar_data["candidates"]) <= 3
    assert sidecar_data["schema"] == "skill-candidates-v1"


def test_f1_sidecar_write_is_atomic(tmp_path, monkeypatch):
    """F1: sidecar publication replaces the file atomically."""
    monkeypatch.setenv("SELFEVO_SKILL_CANDIDATE_MIN_CYCLES", "8")
    monkeypatch.setenv("SELFEVO_SKILL_CANDIDATE_MIN_DAYS", "3")
    _write_rows(tmp_path, _rows())
    write_sidecar(tmp_path, None)
    sidecar = tmp_path / "demand" / "skill_candidates.json"
    assert json.loads(sidecar.read_text(encoding="utf-8"))["schema"] == "skill-candidates-v1"
    assert not list(sidecar.parent.glob(f".{sidecar.name}.*.tmp"))


def test_f1_sidecar_ranked_by_cycles_times_days(tmp_path, monkeypatch):
    """F1: sidecar candidates are ranked by cycles×days descending."""
    monkeypatch.setenv("SELFEVO_SKILL_CANDIDATE_MIN_CYCLES", "8")
    monkeypatch.setenv("SELFEVO_SKILL_CANDIDATE_MIN_DAYS", "3")
    monkeypatch.setenv("SELFEVO_SKILL_CANDIDATE_TOP_N", "10")
    # Sequence A: 10 cycles over 10 distinct days (score 100)
    # Sequence B: 8 cycles over 5 distinct days (score 40)
    rows = []
    for i in range(10):
        rows.append({
            "cycle_id": f"cycle-a-{i}",
            "ts": f"2026-08-{i + 1:02d}T12:00:00Z",
            "actions": ["exec:git-log", "exec:grep", "edit:scripts/*.py"],
        })
    for i in range(8):
        rows.append({
            "cycle_id": f"cycle-b-{i}",
            "ts": f"2026-08-{(i % 5) + 1:02d}T13:00:00Z",
            "actions": ["exec:find", "exec:grep", "write:scripts/*.py"],
        })
    _write_rows(tmp_path, rows)
    candidates = mine(tmp_path, None)
    assert len(candidates) >= 2
    # highest score must come first
    assert candidates[0]["cycles"] * candidates[0]["days"] >= candidates[1]["cycles"] * candidates[1]["days"]


# ── F2: no mining in cycle path regression ────────────────────────────────────

def test_f2_collect_demand_does_not_call_mine(tmp_path, monkeypatch):
    """F2: collect_demand must not invoke mine() — only reads the sidecar."""
    import nanobot.runtime.skill_candidate_mining as scm

    def _mine_should_not_be_called(*args, **kwargs):
        raise AssertionError("mine() was called from the cycle path — F2 regression")

    monkeypatch.setattr(scm, "mine", _mine_should_not_be_called)
    # No sidecar written → read_sidecar returns [] → no candidates, no mine() call
    result = demand._skill_candidate_items(tmp_path, None)
    assert result == []

    # With a sidecar present: still must not call mine()
    sidecar_path = tmp_path / "demand" / "skill_candidates.json"
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    sidecar_path.write_text(json.dumps({
        "schema": "skill-candidates-v1",
        "written_at": "2026-08-26T00:00:00Z",
        "candidates": [{
            "sequence": ["exec:grep", "edit:scripts/*.py"],
            "cycles": 10,
            "days": 5,
            "samples": ["cycle-1"],
        }],
    }), encoding="utf-8")
    result2 = demand._skill_candidate_items(tmp_path, None)
    assert len(result2) == 1
    assert result2[0]["kind"] == "skill-candidate"


# ── F3: categorical pure-read/list denial ─────────────────────────────────────

def test_f3_pure_read_ngram_is_rejected(tmp_path, monkeypatch):
    """F3: n-grams with only read/list actions must never qualify as candidates."""
    monkeypatch.setenv("SELFEVO_SKILL_CANDIDATE_MIN_CYCLES", "8")
    monkeypatch.setenv("SELFEVO_SKILL_CANDIDATE_MIN_DAYS", "3")
    rows = [
        {
            "cycle_id": f"cycle-{i}",
            "ts": f"2026-08-{i + 1:02d}T12:00:00Z",
            "actions": ["read:scripts/*.py", "read:tests/*.py", "list:*"],
        }
        for i in range(1, 11)
    ]
    _write_rows(tmp_path, rows)
    candidates = mine(tmp_path, None)
    # all grams are pure read/list → nothing qualifies
    assert candidates == []


def test_f3_is_meaningful_helper():
    """F3: _is_meaningful correctly identifies meaningful vs pure-read sequences."""
    assert _is_meaningful(("exec:pytest", "read:scripts/*.py"))
    assert _is_meaningful(("edit:scripts/*.py", "read:tests/*.py"))
    assert _is_meaningful(("write:scripts/*.py",))
    assert not _is_meaningful(("read:scripts/*.py", "read:tests/*.py"))
    assert not _is_meaningful(("list:*", "read:var/*.py"))


# ── F4: legacy var/* template denial ─────────────────────────────────────────

def test_f4_legacy_var_template_ignored_by_miner(tmp_path, monkeypatch):
    """F4: action templates starting with var/ must be skipped by the miner."""
    monkeypatch.setenv("SELFEVO_SKILL_CANDIDATE_MIN_CYCLES", "8")
    monkeypatch.setenv("SELFEVO_SKILL_CANDIDATE_MIN_DAYS", "3")
    rows = [
        {
            "cycle_id": f"cycle-{i}",
            "ts": f"2026-08-{i + 1:02d}T12:00:00Z",
            "actions": ["read:var/*.py", "exec:cd", "exec:cd"],
        }
        for i in range(1, 11)
    ]
    _write_rows(tmp_path, rows)
    candidates = mine(tmp_path, None)
    # var/* grams must be excluded
    for candidate in candidates:
        for action in candidate["sequence"]:
            assert not action.startswith("var/"), f"Legacy var/* action leaked: {action}"


def test_f4_has_legacy_var_helper():
    """F4: _has_legacy_var detects legacy templates."""
    assert _has_legacy_var(("read:var/*.py", "exec:cd"))
    assert _has_legacy_var(("var/lib/something",))
    assert not _has_legacy_var(("read:scripts/*.py", "exec:grep"))
    assert not _has_legacy_var(("exec:cd", "edit:tests/*.py"))


def test_f4_state_root_stripped_produces_state_template(tmp_path):
    """F4: action_index strips state_root so paths produce state/* templates."""
    from nanobot.runtime.action_index import _known_workspace_roots, _path_template
    state_root = tmp_path / "state"
    workspace_roots = _known_workspace_roots(state_root)
    # A path inside state_root should strip to a relative path
    test_path = str(state_root / "demand" / "skill_candidates.json")
    result = _path_template(test_path, workspace_roots)
    # Must not start with "var/" — should be stripped to something relative
    assert result is not None
    assert not result.startswith("var/"), f"Path template leaked var/: {result}"


def test_f4_force_regen_rebuilds_existing_day_files(tmp_path):
    """F4: force_regenerate=True deletes and rebuilds day files from source prompts."""
    from datetime import datetime, timezone

    from nanobot.runtime.action_index import build_action_index

    # Use today's date to prevent immediate archival (archive_cutoff > 7 days ago)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Create a fake prompt day file
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    prompt_path = prompts_dir / f"{today}.jsonl"
    ledger_dir = tmp_path / "ledger"
    ledger_dir.mkdir()
    # Write a ledger row
    cycle_id = "cycle-force-regen-test"
    (ledger_dir / "cycles.jsonl").write_text(
        json.dumps({"cycle_id": cycle_id, "phase": "outcome", "outcome": "success", "ts": f"{today}T12:00:00Z"}) + "\n",
        encoding="utf-8",
    )
    # Write a prompt with a tool call
    msg = {"role": "assistant", "tool_calls": [{"function": {"name": "exec", "arguments": '{"command": "grep foo scripts/"}'}}]}
    prompt_path.write_text(
        json.dumps({"cycle_id": cycle_id, "messages": [msg], "seq": 1}) + "\n",
        encoding="utf-8",
    )
    # First normal build
    s1 = build_action_index(tmp_path, prompts_dir)
    assert s1["written"] == 1

    # The day file should be a plain .jsonl (today, not archived)
    index_file = tmp_path / "action_index" / f"{today}.jsonl"
    assert index_file.exists(), "Index file should be a plain .jsonl for today's date"

    # Insert stale content into the day file
    original = index_file.read_text(encoding="utf-8")
    index_file.write_text(
        original + json.dumps({"cycle_id": "stale-extra", "ts": "", "actions": ["var/stale"]}) + "\n",
        encoding="utf-8",
    )

    # Force regen should delete and rebuild from source
    s2 = build_action_index(tmp_path, prompts_dir, force_regenerate=True)
    assert s2["force_regenerated"] >= 1
    rebuilt = [json.loads(line) for line in index_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    # stale-extra should be gone
    assert not any(r.get("cycle_id") == "stale-extra" for r in rebuilt)
