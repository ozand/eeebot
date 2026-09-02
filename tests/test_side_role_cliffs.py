"""#1178: side-role size cliffs and read-blank-then-write-back sidecars.

Live on the host 2026-09-02: ``reflector/reflections.jsonl`` was 738,050 B with
no rotation (it crossed the curator's old 512 KiB bail-out around 2026-08-29,
#1183); ``knowledge_lift.compute_knowledge_digest`` skipped it and the 77,843 B
``lessons.yaml`` outright at 50,000 B, so the "knowledge unchanged" watermark
ignored every lesson and reflection change; five JSON sidecars and two JSONL
harness stores rewrote themselves from the blank default a corrupt or oversize
read returns. Every test here fails against the pre-#1178 tree for the reason
in its docstring.
"""
from __future__ import annotations

import gzip
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from nanobot.runtime import (
    demand,
    evolution_tree,
    heldout,
    knowledge_curator,
    knowledge_lift,
    reflector,
    skill_eval_harness,
    skill_fitness,
    state_access,
    tech_tree,
    validator_harness,
)
from nanobot.runtime.heldout import microbench

NOW = datetime.now(timezone.utc)


def _iso(hours_ago: float = 0) -> str:
    return (NOW - timedelta(hours=hours_ago)).isoformat().replace("+00:00", "Z")


def _row(cycle: str, detail: str, *, status: str = "", hours_ago: float = 1, rec_status: str = "") -> dict:
    row = {"cycle_id": cycle, "timestamp": _iso(hours_ago), "created_at": _iso(hours_ago), "summary": f"summary for {cycle}",
           "findings": [], "recommendations": [{"kind": "approach_hint", "detail": detail, "evidence": f"seen in {cycle}"}]}
    if status:
        row["status"] = status
    if rec_status:
        row["recommendations"][0]["status"] = rec_status
    return row


def _write_archive(state: Path, day: str, rows: list[dict], seq: str = "") -> Path:
    path = state / "reflector" / "archive" / f"reflections-{day}{seq}.jsonl.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        fh.writelines(json.dumps(r) + "\n" for r in rows)
    return path


def _write_live(state: Path, rows: list[dict]) -> Path:
    path = state / "reflector" / "reflections.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


# ─── rotation ────────────────────────────────────────────────────────────────

def test_journal_rotates_past_512_kib_and_the_tail_reads_across_the_boundary(tmp_path):
    """Pre-fix: no rotation existed; the file grew without bound and _journal_tail read only the live file."""
    state = tmp_path / "state"
    padding = "x" * 2000
    for i in range(300):  # ~600 KB of rows
        reflector._append_journal(state, {**_row(f"c{i:03d}", f"hint {i}", hours_ago=300 - i), "pad": padding})
    archives = reflector._archives(state)
    assert len(archives) == 1 and archives[0].name.startswith("reflections-") and archives[0].name.endswith(".jsonl.gz")
    live = state / "reflector" / "reflections.jsonl"
    assert live.stat().st_size < reflector._MAX_JOURNAL_BYTES
    with gzip.open(archives[0], "rt", encoding="utf-8") as fh:
        archived = [json.loads(line) for line in fh if line.strip()]
    live_rows = [json.loads(line) for line in live.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(archived) + len(live_rows) == 300 and archived[0]["cycle_id"] == "c000"
    # the tail is the newest 10 rows regardless of which file holds them
    reflector._append_journal(state, _row("c300", "hint 300", hours_ago=0))
    tail = reflector._journal_tail(state)
    assert [r["cycle_id"] for r in tail] == [f"c{i:03d}" for i in range(291, 301)]


def test_readers_see_rows_that_live_only_in_the_newest_archives(tmp_path):
    """Pre-fix: _journal_tail, iter_lessons and demand._reflection_items read the live file only."""
    state = tmp_path / "state"
    _write_archive(state, "2026-08-20", [_row("old", "ancient hint", hours_ago=2)])
    _write_archive(state, "2026-08-30", [_row("mid", "middle hint", hours_ago=2)])
    _write_archive(state, "2026-09-01", [_row("new", "newest archived hint about scripts/foo.py", hours_ago=2)])
    _write_live(state, [_row("live", "live hint", hours_ago=1)])

    assert [r["cycle_id"] for r in reflector._journal_tail(state)] == ["mid", "new", "live"], "newest 2 archives + live"
    assert [p.name for p in reflector.reflection_files(state)][-1] == "reflections.jsonl"

    lessons = list(knowledge_curator.iter_lessons(tmp_path / "workspace", state))
    assert {entry["cycle_id"] for entry in lessons} == {"mid", "new", "live"}

    items = demand._reflection_items(state, NOW)
    assert {item["evidence"].split(":")[0] for item in items} == {"cycle mid", "cycle new", "cycle live"}
    assert any(item["affected_path"] == "scripts/foo.py" for item in items)


def test_mark_reflection_consumed_marks_a_row_that_rotated(tmp_path):
    """Pre-fix: only the live file was searched, so a rotated recommendation could never be consumed."""
    state = tmp_path / "state"
    archive = _write_archive(state, "2026-09-01", [_row("arc", "archived hint"), _row("arc2", "other hint")])
    _write_live(state, [_row("live", "live hint")])
    assert reflector.mark_reflection_consumed(state, recommendation_detail="archived hint") is True
    with gzip.open(archive, "rt", encoding="utf-8") as fh:
        rows = {json.loads(line)["cycle_id"]: json.loads(line) for line in fh if line.strip()}
    assert rows["arc"]["recommendations"][0]["status"] == "consumed" and rows["arc"]["status"] == "consumed"
    assert "status" not in rows["arc2"]["recommendations"][0]
    assert reflector.mark_reflection_consumed(state, recommendation_detail="nowhere") is False


def test_archive_reads_are_bounded_to_the_newest_two(tmp_path, monkeypatch):
    state = tmp_path / "state"
    for day in ("2026-08-10", "2026-08-20", "2026-08-30"):
        _write_archive(state, day, [_row(day, f"hint {day}")])
    _write_live(state, [_row("live", "live")])
    opened: list[str] = []
    real_open = gzip.open

    def recording(path, *args, **kwargs):
        opened.append(Path(path).name)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(gzip, "open", recording)
    list(reflector.iter_reflection_rows(state))
    assert opened == ["reflections-2026-08-20.jsonl.gz", "reflections-2026-08-30.jsonl.gz"]


def test_promotion_tops_up_a_freshly_rotated_journal_from_the_archive(tmp_path):
    """Pre-fix: the bounded tail read the live file only; right after a rotation it saw nothing to promote.
    Since #1171 the mint reads every row after its cursor across the archives, and a first-seen
    recommendation waits in the pool rather than minting — so the archive read is observed there."""
    state = tmp_path / "state"
    workspace = tmp_path / "workspace"
    (workspace / "lessons").mkdir(parents=True)
    _write_archive(state, "2026-09-01", [
        _row("a1", "Prefer bounded reads over whole-file reads in verification steps"),
        _row("a2", "Run the targeted test module before committing a script change"),
    ])
    _write_live(state, [])
    assert knowledge_curator.promote_reflector_recommendations_to_v2(workspace, state) == 0
    pool = knowledge_curator.load_reflector_pool(state)
    assert pool["last_run"]["rows_read"] == 2 and pool["last_run"]["items"] == 2
    assert sorted(c["cycles"][0] for c in pool["clusters"]) == ["a1", "a2"]


# ─── digest ──────────────────────────────────────────────────────────────────

def test_digest_tracks_files_past_50_kb(tmp_path):
    """Pre-fix: any file over MAX_FILE_BYTES was skipped, so appending to a 700 KB journal changed nothing."""
    repo = tmp_path / "repo"
    (repo / "lessons").mkdir(parents=True)
    (repo / "lessons" / "lessons.yaml").write_text("- id: L1\n  problem: p\n  solution: s\n" * 3000, encoding="utf-8")  # > 50 KB
    state = tmp_path / "state"
    _write_live(state, [{**_row(f"c{i}", f"hint {i}"), "pad": "x" * 500} for i in range(200)])  # > 50 KB
    before = knowledge_lift.compute_knowledge_digest(repo, state)
    reflector._append_journal(state, _row("new", "a new reflection"))
    after_reflection = knowledge_lift.compute_knowledge_digest(repo, state)
    assert after_reflection != before
    with (repo / "lessons" / "lessons.yaml").open("a", encoding="utf-8") as fh:
        fh.write("- id: L-new\n  problem: new problem\n  solution: new solution\n")
    assert knowledge_lift.compute_knowledge_digest(repo, state) != after_reflection


# ─── write guards ────────────────────────────────────────────────────────────

def _corrupt(path: Path) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    return path.read_bytes()


def test_rewrite_status_names_every_reason(tmp_path):
    """Pre-fix: no such contract."""
    path = tmp_path / "x.json"
    assert state_access.rewrite_status(path) == "absent"
    path.write_text("{}", encoding="utf-8")
    assert state_access.rewrite_status(path) == "present"
    assert state_access.rewrite_status(path, max_bytes=1) == "oversize"
    path.write_text("{oops", encoding="utf-8")
    assert state_access.rewrite_status(path) == "corrupt"
    assert state_access.rewrite_status(path, json_object=False) == "present"
    assert state_access.rewrite_status(tmp_path) == "permission"


def test_corrupt_tree_is_not_overwritten_by_record_node(tmp_path, caplog):
    """Pre-fix: read_tree returned an empty tree and record_node wrote it back — lineage erased."""
    state = tmp_path / "state"
    before = _corrupt(state / "evolution" / "tree.json")
    with caplog.at_level(logging.WARNING):
        evolution_tree.record_node(state, sha="abc1", parent_sha=None, branch="main", cycle_id="c-1")
    assert (state / "evolution" / "tree.json").read_bytes() == before
    assert any("evolution_tree: write skipped, existing file is corrupt" in r.message for r in caplog.records)
    fresh = tmp_path / "fresh"
    evolution_tree.record_node(fresh, sha="abc1", parent_sha=None, branch="main", cycle_id="c-1")
    assert "abc1" in evolution_tree.read_tree(fresh)["nodes"]


def test_corrupt_portfolio_is_not_overwritten(tmp_path, caplog):
    state = tmp_path / "state"
    before = _corrupt(state / "tech_tree" / "portfolio.json")
    with caplog.at_level(logging.WARNING):
        tech_tree.ensure_seeded(state)
    assert (state / "tech_tree" / "portfolio.json").read_bytes() == before
    assert any("tech_tree: write skipped" in r.message for r in caplog.records)
    tech_tree.ensure_seeded(tmp_path / "fresh")
    assert (tmp_path / "fresh" / "tech_tree" / "portfolio.json").is_file()


def test_corrupt_skill_reads_heldout_results_and_microbench_are_not_overwritten(tmp_path, caplog):
    state = tmp_path / "state"
    reads_path = state / skill_fitness.SIDECAR_REL
    results_path = state / "heldout" / "results.json"
    bench_path = microbench._microbench_path(state)
    befores = {p: _corrupt(p) for p in (reads_path, results_path, bench_path)}
    with caplog.at_level(logging.WARNING):
        skill_fitness._write_sidecar_atomic(state, {"schema_version": skill_fitness.SCHEMA_VERSION, "reads": []})
        heldout._save_results(state, {"schema_version": heldout.HELDOUT_SCHEMA, "results": {}})
        microbench._save_microbench_file(state, {"schema_version": microbench._SCHEMA, "entries": {}})
    for path, before in befores.items():
        assert path.read_bytes() == before, path
    messages = " ".join(r.message for r in caplog.records)
    assert "skill_fitness: write skipped" in messages and "heldout: write skipped" in messages and "microbench: write skipped" in messages
    fresh = tmp_path / "fresh"
    skill_fitness._write_sidecar_atomic(fresh, {"schema_version": skill_fitness.SCHEMA_VERSION, "reads": []})
    heldout._save_results(fresh, {"schema_version": heldout.HELDOUT_SCHEMA, "results": {}})
    microbench._save_microbench_file(fresh, {"schema_version": microbench._SCHEMA, "entries": {}})
    assert (fresh / skill_fitness.SIDECAR_REL).is_file() and (fresh / "heldout" / "results.json").is_file() and microbench._microbench_path(fresh).is_file()


def test_oversize_jsonl_stores_are_left_alone_not_truncated(tmp_path, caplog):
    """Pre-fix: _load_rows / _read_eval_rows returned [] past the cap and the rewrite persisted [];
    validator_harness._prune_last_runs wrote "" on an oversize file."""
    state = tmp_path / "state"
    sidecar = skill_eval_harness._sidecar_path(state)
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    big = (json.dumps({"pad": "x" * 1000}) + "\n") * (skill_eval_harness.MAX_SIDECAR_BYTES // 1000 + 2)
    sidecar.write_text(big, encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        skill_eval_harness._rewrite_rows(state, [], [{"row": 1}])
    assert sidecar.read_text(encoding="utf-8") == big

    evals = tmp_path / "knowledge_lift" / "evals.jsonl"
    evals.parent.mkdir()
    big_evals = (json.dumps({"pad": "y" * 1000}) + "\n") * (knowledge_lift.MAX_FILE_BYTES * 40 // 1000 + 2)
    evals.write_text(big_evals, encoding="utf-8")
    knowledge_lift._atomic_write_eval_rows(evals, [{"row": 1}])
    assert evals.read_text(encoding="utf-8") == big_evals

    last_runs = validator_harness._last_runs_path(state)
    last_runs.parent.mkdir(parents=True, exist_ok=True)
    payload = ("z" * 4000 + "\n") * (validator_harness._MAX_READ_BYTES // 4000 + 2)
    last_runs.write_text(payload, encoding="utf-8")
    validator_harness._prune_last_runs(state, {"scripts/a.py"})
    assert last_runs.read_text(encoding="utf-8") == payload, "left exactly as it was, not emptied"
    messages = " ".join(r.message for r in caplog.records)
    assert "skill_eval_harness: rows not rewritten" in messages and "knowledge_lift: eval rows not rewritten" in messages
    assert "last_runs.jsonl is" in messages and "left untouched" in messages
