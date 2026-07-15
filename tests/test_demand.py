"""Tests for #760: the deterministic, LLM-free demand collector.

Covers each demand kind (priority / defect / hypothesis), the boilerplate-
hypothesis exclusion (exact-title regression pins), the py_compile scan's
HEAD watermark no-op, bounded reads, exhaustion (2+ self-dedup rejects per
demand_id) with its HEAD-move / 7-day expiry, and fail-open behavior on
unreadable state.
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from nanobot.runtime import cycle_ledger, demand
from tests.test_goal_backlog_routing import GOAL_TEXT_JSON, _make_git_repo_with_commit

# The two chronic boilerplate candidates observed live (#760): re-stamped
# every cycle by the deterministic generator, carrying no measurement
# evidence — an echo, not demand. Pinned verbatim.
BOILERPLATE_TITLES = (
    "Use one bounded subagent-assisted review to verify the materialized improvement artifact",
    "Synthesize one new bounded improvement candidate from retired lanes",
)


def _state_dir(tmp_path: Path) -> Path:
    state_dir = tmp_path / "state"
    (state_dir / "goals").mkdir(parents=True)
    return state_dir


def _write_goal_text(state_dir: Path, text: str) -> None:
    (state_dir / "goals" / "goal_text.json").write_text(
        json.dumps({"text": text}), encoding="utf-8"
    )


def _now_iso(minutes_ago: int = 0) -> str:
    return (
        (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago))
        .isoformat()
        .replace("+00:00", "Z")
    )


def _git_repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=repo, check=True)
    return repo


def _commit_all(repo: Path, message: str = "more") -> None:
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=repo, check=True)


# ─── kind: priority ─────────────────────────────────────────────────────────


class TestPriorityDemand:
    def test_remaining_priorities_become_demand(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, json.loads(GOAL_TEXT_JSON)["text"])

        items = demand.collect_demand(state_dir, None)
        priorities = [i for i in items if i["kind"] == "priority"]
        assert len(priorities) == 2
        assert priorities[0]["summary"].startswith("Priority 5 — Write scripts/cycle_logger.py")
        assert priorities[1]["summary"].startswith("Priority 6 — Write scripts/smoke_test_loop.py")
        assert "append_cycle_summary" in priorities[0]["evidence"]
        # Stable id: hash of kind+summary.
        assert priorities[0]["id"] == demand.item_id("priority", priorities[0]["summary"])

    def test_completed_priorities_are_not_demand(self, tmp_path):
        """Done-detection is DELEGATED to cycle_planning's #748 filter, not
        reimplemented: priorities whose target files exist with commit
        evidence are filtered out before demand collection sees them."""
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, json.loads(GOAL_TEXT_JSON)["text"])
        repo = _make_git_repo_with_commit(
            tmp_path,
            "feat: write scripts/cycle_logger.py — confirmed done for cycle-999",
            "feat: write scripts/smoke_test_loop.py — confirmed done for cycle-1000",
            create_files=("scripts/cycle_logger.py", "scripts/smoke_test_loop.py"),
        )
        items = demand.collect_demand(state_dir, repo)
        assert [i for i in items if i["kind"] == "priority"] == []

    def test_no_goal_text_no_priority_demand(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        items = demand.collect_demand(state_dir, None)
        assert [i for i in items if i["kind"] == "priority"] == []


# ─── kind: defect ───────────────────────────────────────────────────────────


class TestLedgerDefects:
    def test_recent_failed_outcome_is_demand(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        cycle_ledger.append_event(
            state_dir,
            {"phase": "outcome", "cycle_id": "c1", "outcome": "failed", "reason": "gate_failed", "ts": _now_iso(30)},
        )
        items = demand.collect_demand(state_dir, None)
        defects = [i for i in items if i["kind"] == "defect"]
        assert len(defects) == 1
        assert "gate_failed" in defects[0]["summary"]
        assert "c1" in defects[0]["evidence"]

    def test_skipped_duplicate_outcomes_are_not_defects(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        cycle_ledger.append_event(
            state_dir,
            {"phase": "outcome", "cycle_id": "c1", "outcome": "skipped-duplicate", "ts": _now_iso(30)},
        )
        assert demand.collect_demand(state_dir, None) == []

    def test_old_failures_outside_48h_window_excluded(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        old_ts = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat().replace("+00:00", "Z")
        cycle_ledger.append_event(
            state_dir,
            {"phase": "outcome", "cycle_id": "c-old", "outcome": "failed", "reason": "ancient", "ts": old_ts},
        )
        assert demand.collect_demand(state_dir, None) == []

    def test_success_outcomes_are_not_defects(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        cycle_ledger.append_event(
            state_dir,
            {"phase": "outcome", "cycle_id": "c1", "outcome": "success", "ts": _now_iso(10)},
        )
        assert demand.collect_demand(state_dir, None) == []

    def test_duplicate_failure_reasons_collapse_to_one_item(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        for i in range(5):
            cycle_ledger.append_event(
                state_dir,
                {"phase": "outcome", "cycle_id": "c1", "outcome": "failed", "reason": "gate_failed", "ts": _now_iso(30 - i)},
            )
        defects = [i for i in demand.collect_demand(state_dir, None) if i["kind"] == "defect"]
        assert len(defects) == 1


class TestResultFileDefects:
    def _write_result(self, state_dir: Path, name: str, payload: dict) -> Path:
        results_dir = state_dir / "subagents" / "results"
        results_dir.mkdir(parents=True, exist_ok=True)
        path = results_dir / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_failed_result_with_error_text_is_demand(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        self._write_result(
            state_dir,
            "result-1.json",
            {"status": "failed", "task_title": "Fix the flaky import", "error": "ImportError: no module named foo"},
        )
        defects = [i for i in demand.collect_demand(state_dir, None) if i["kind"] == "defect"]
        assert len(defects) == 1
        assert "Fix the flaky import" in defects[0]["summary"]
        assert "ImportError" in defects[0]["evidence"]

    def test_completed_results_are_not_defects(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        self._write_result(state_dir, "result-1.json", {"status": "completed", "task_title": "Done thing"})
        assert demand.collect_demand(state_dir, None) == []

    def test_read_is_bounded_to_max_result_files(self, tmp_path, monkeypatch):
        """Only the _MAX_RESULT_FILES most recently modified result files are
        even opened (existence_index._MAX_LEDGER_RESULTS discipline)."""
        state_dir = _state_dir(tmp_path)
        monkeypatch.setattr(demand, "_MAX_RESULT_FILES", 3)
        import os
        import time

        now = time.time()
        for i in range(6):
            path = self._write_result(
                state_dir,
                f"result-{i}.json",
                {"status": "failed", "task_title": f"failure {i}", "error": "boom"},
            )
            os.utime(path, (now - (600 - i), now - (600 - i)))
        defects = [i for i in demand.collect_demand(state_dir, None) if i["kind"] == "defect"]
        assert len(defects) == 3
        # The most recent files (highest i) won the bounded read.
        assert {d["summary"] for d in defects} == {
            "subagent result failed: failure 3",
            "subagent result failed: failure 4",
            "subagent result failed: failure 5",
        }


class TestCompileDefects:
    def test_broken_script_is_demand(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        repo = _git_repo(tmp_path)
        (repo / "scripts").mkdir()
        (repo / "scripts" / "broken.py").write_text("def broken(:\n", encoding="utf-8")
        _commit_all(repo, "add broken script")

        defects = [i for i in demand.collect_demand(state_dir, repo) if i["kind"] == "defect"]
        assert len(defects) == 1
        assert defects[0]["summary"] == "script fails to compile: scripts/broken.py"
        assert defects[0]["affected_path"] == "scripts/broken.py"
        assert "SyntaxError" in defects[0]["evidence"]

    def test_watermark_no_ops_on_unchanged_head(self, tmp_path):
        """When HEAD hasn't moved, the scan is a no-op: cached findings are
        reused and NEW uncommitted breakage is not even looked at."""
        state_dir = _state_dir(tmp_path)
        repo = _git_repo(tmp_path)
        (repo / "scripts").mkdir()
        (repo / "scripts" / "ok.py").write_text("x = 1\n", encoding="utf-8")
        _commit_all(repo, "add ok script")

        assert demand.collect_demand(state_dir, repo) == []
        wm = json.loads((state_dir / "demand" / "py_compile_watermark.json").read_text(encoding="utf-8"))
        assert wm["failures"] == []

        # Break a script WITHOUT committing — HEAD unchanged, so the cached
        # (clean) findings must be reused: no defect surfaces.
        (repo / "scripts" / "ok.py").write_text("def broken(:\n", encoding="utf-8")
        assert demand.collect_demand(state_dir, repo) == []

        # Commit it — HEAD moves, watermark invalidates, rescan finds it.
        _commit_all(repo, "break the script")
        defects = [i for i in demand.collect_demand(state_dir, repo) if i["kind"] == "defect"]
        assert len(defects) == 1
        assert defects[0]["affected_path"] == "scripts/ok.py"

    def test_no_repo_no_compile_demand(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        assert demand.collect_demand(state_dir, None) == []
        assert demand.collect_demand(state_dir, tmp_path / "not-a-repo") == []


# ─── kind: hypothesis ───────────────────────────────────────────────────────


class TestHypothesisDemand:
    def _write_backlog(self, state_dir: Path, entries: list[dict]) -> None:
        d = state_dir / "hypotheses"
        d.mkdir(parents=True, exist_ok=True)
        (d / "backlog.json").write_text(json.dumps({"entries": entries}), encoding="utf-8")

    def test_hypothesis_with_metric_field_qualifies(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        self._write_backlog(
            state_dir,
            [{"task_title": "Reduce cycle disk writes", "metric": "ledger writes per cycle: 40 -> target 10"}],
        )
        hyps = [i for i in demand.collect_demand(state_dir, None) if i["kind"] == "hypothesis"]
        assert len(hyps) == 1
        assert hyps[0]["summary"] == "Reduce cycle disk writes"
        assert "40 -> target 10" in hyps[0]["evidence"]

    def test_hypothesis_with_evidence_field_qualifies(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        self._write_backlog(
            state_dir,
            [{"task_title": "Fix the metrics report crash", "evidence": "Traceback in journal 2026-07-14"}],
        )
        assert len([i for i in demand.collect_demand(state_dir, None) if i["kind"] == "hypothesis"]) == 1

    def test_hypothesis_with_acceptance_referencing_repo_file_qualifies(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        repo = _git_repo(tmp_path)
        (repo / "scripts").mkdir()
        (repo / "scripts" / "loop_report.py").write_text("x = 1\n", encoding="utf-8")
        _commit_all(repo)
        self._write_backlog(
            state_dir,
            [{
                "task_title": "Harden the loop report",
                "acceptance": "scripts/loop_report.py exits 0 on an empty ledger",
            }],
        )
        assert len([i for i in demand.collect_demand(state_dir, repo) if i["kind"] == "hypothesis"]) == 1

    def test_hypothesis_with_acceptance_referencing_missing_file_does_not_qualify(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        repo = _git_repo(tmp_path)
        self._write_backlog(
            state_dir,
            [{
                "task_title": "Ship a brand new dashboard",
                "acceptance": "scripts/does_not_exist.py renders the dashboard",
            }],
        )
        assert [i for i in demand.collect_demand(state_dir, repo) if i["kind"] == "hypothesis"] == []

    def test_boilerplate_titles_are_never_demand(self, tmp_path):
        """#760 exact-title regression pin: the two chronic boilerplate
        candidates carry no measurement evidence and MUST NOT qualify."""
        state_dir = _state_dir(tmp_path)
        self._write_backlog(
            state_dir,
            [{"task_title": title} for title in BOILERPLATE_TITLES],
        )
        research_dir = state_dir / "research"
        research_dir.mkdir(parents=True)
        (research_dir / "hypotheses.json").write_text(
            json.dumps(
                [{"cycle_id": "c1", "candidates": [{"title": title} for title in BOILERPLATE_TITLES]}]
            ),
            encoding="utf-8",
        )
        assert demand.collect_demand(state_dir, None) == []

    def test_research_candidate_with_metric_qualifies(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        research_dir = state_dir / "research"
        research_dir.mkdir(parents=True)
        (research_dir / "hypotheses.json").write_text(
            json.dumps(
                [{
                    "cycle_id": "c1",
                    "candidates": [{"title": "Trim ledger rotation cost", "metric": "rotation p95 800ms"}],
                }]
            ),
            encoding="utf-8",
        )
        hyps = [i for i in demand.collect_demand(state_dir, None) if i["kind"] == "hypothesis"]
        assert len(hyps) == 1


# ─── exhaustion ─────────────────────────────────────────────────────────────


def _append_self_dedup_reject(state_dir: Path, demand_id: str, ts: str | None = None) -> None:
    event = {"phase": "proposer_reject", "reason": "self_dedup", "demand_id": demand_id}
    if ts:
        event["ts"] = ts
    cycle_ledger.append_event(state_dir, event)


class TestExhaustion:
    def test_two_self_dedup_rejects_exhaust_the_item(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, json.loads(GOAL_TEXT_JSON)["text"])

        items = demand.collect_demand(state_dir, None)
        target = [i for i in items if i["kind"] == "priority"][0]

        _append_self_dedup_reject(state_dir, target["id"])
        assert any(i["id"] == target["id"] for i in demand.collect_demand(state_dir, None))

        _append_self_dedup_reject(state_dir, target["id"])
        remaining = demand.collect_demand(state_dir, None)
        assert not any(i["id"] == target["id"] for i in remaining)
        # The OTHER priority item is still presented.
        assert any(i["kind"] == "priority" for i in remaining)

        sidecar = json.loads((state_dir / "demand" / "exhausted.json").read_text(encoding="utf-8"))
        assert sidecar["schema_version"] == "demand-exhausted-v1"
        assert sidecar["entries"][target["id"]]["status"] == "exhausted"

    def test_exhaustion_expires_when_head_moves(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, json.loads(GOAL_TEXT_JSON)["text"])
        repo = _git_repo(tmp_path)

        items = demand.collect_demand(state_dir, repo)
        target = [i for i in items if i["kind"] == "priority"][0]
        _append_self_dedup_reject(state_dir, target["id"])
        _append_self_dedup_reject(state_dir, target["id"])
        assert not any(i["id"] == target["id"] for i in demand.collect_demand(state_dir, repo))

        # HEAD moves — the exhaustion expires and the item is presented
        # again; the OLD reject rows must not instantly re-exhaust it.
        (repo / "new_file.txt").write_text("x\n", encoding="utf-8")
        _commit_all(repo, "move head")
        assert any(i["id"] == target["id"] for i in demand.collect_demand(state_dir, repo))
        # Stays presented on the next pass too (reset_at gates old rejects).
        assert any(i["id"] == target["id"] for i in demand.collect_demand(state_dir, repo))

    def test_exhaustion_expires_after_seven_days(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, json.loads(GOAL_TEXT_JSON)["text"])

        items = demand.collect_demand(state_dir, None)
        target = [i for i in items if i["kind"] == "priority"][0]
        old_ts = (datetime.now(timezone.utc) - timedelta(days=9)).isoformat().replace("+00:00", "Z")
        _append_self_dedup_reject(state_dir, target["id"], ts=old_ts)
        _append_self_dedup_reject(state_dir, target["id"], ts=old_ts)
        assert not any(i["id"] == target["id"] for i in demand.collect_demand(state_dir, None))

        # Age the exhaustion record past the 7-day expiry.
        sidecar_path = state_dir / "demand" / "exhausted.json"
        sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
        sidecar["entries"][target["id"]]["exhausted_at"] = old_ts
        sidecar_path.write_text(json.dumps(sidecar), encoding="utf-8")

        assert any(i["id"] == target["id"] for i in demand.collect_demand(state_dir, None))

    def test_rejects_without_demand_id_do_not_exhaust(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, json.loads(GOAL_TEXT_JSON)["text"])
        for _ in range(5):
            cycle_ledger.append_event(state_dir, {"phase": "proposer_reject", "reason": "self_dedup"})
        assert len([i for i in demand.collect_demand(state_dir, None) if i["kind"] == "priority"]) == 2


# ─── fail-open ──────────────────────────────────────────────────────────────


class TestFailOpen:
    def test_missing_state_dir_returns_empty(self, tmp_path):
        assert demand.collect_demand(tmp_path / "does-not-exist", None) == []

    def test_corrupt_goal_text_and_ledger_do_not_raise(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        (state_dir / "goals" / "goal_text.json").write_text("{{{not json", encoding="utf-8")
        ledger_dir = state_dir / "ledger"
        ledger_dir.mkdir(parents=True)
        (ledger_dir / "cycles.jsonl").write_text("garbage\n\x00\xff\n", encoding="utf-8", errors="ignore")
        assert demand.collect_demand(state_dir, None) == []

    def test_corrupt_hypothesis_and_result_files_do_not_raise(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        hyp_dir = state_dir / "hypotheses"
        hyp_dir.mkdir(parents=True)
        (hyp_dir / "backlog.json").write_text("[not a dict", encoding="utf-8")
        results_dir = state_dir / "subagents" / "results"
        results_dir.mkdir(parents=True)
        (results_dir / "bad.json").write_text("{{{", encoding="utf-8")
        assert demand.collect_demand(state_dir, None) == []

    def test_corrupt_exhausted_sidecar_is_ignored(self, tmp_path):
        state_dir = _state_dir(tmp_path)
        _write_goal_text(state_dir, json.loads(GOAL_TEXT_JSON)["text"])
        d = state_dir / "demand"
        d.mkdir(parents=True)
        (d / "exhausted.json").write_text("not json at all", encoding="utf-8")
        assert len([i for i in demand.collect_demand(state_dir, None) if i["kind"] == "priority"]) == 2

    def test_item_id_is_stable_and_kind_prefixed(self):
        a = demand.item_id("defect", "some summary")
        b = demand.item_id("defect", "some summary")
        c = demand.item_id("priority", "some summary")
        assert a == b
        assert a != c
        assert a.startswith("defect-")
        assert c.startswith("priority-")
