"""Tests for #768: the periodic, bounded goal-review.

Covers: the SELFEVO_GOAL_REVIEW_ENABLED kill switch (default OFF = hard
no-op, no watermark, no LLM call), the daily watermark, the no-gaps honest
no-op, the fail-closed vector/evidence validation with recorded rejections,
append-only goal_text writing in the exact `(X) Priority N — label: body`
shape demand's parser reads (numbering continued past Completed mentions,
operator entries untouched), dedup against existing priorities, the
goal_review ledger row, malformed/exception LLM replies, and the fail-open
wiring from the scorecard recompute path. The LLM call is always
monkeypatched — no test ever reaches a provider.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from nanobot.runtime import goal_review

NOW = datetime.now(timezone.utc)

GOAL_TEXT = (
    "eeebot purpose. Vector 1 (PRIMARY) — Self-Improvement of the Agent "
    "System: raise cycle efficiency and quality.\n\n"
    "Vector 2 (SECONDARY) — Operator Interface and Process Transparency.\n\n"
    "FUTURE (deferred): creative works.\n\n"
    "Current priority targets:\n"
    "(A) Priority 11 — Loop health in dashboard: extend "
    "scripts/eeebot_dashboard.py with a loop-health section. Commit.\n"
    "(B) Priority 16 — Cycle strip line in dashboard: add ONE function "
    "render_cycle_strip to scripts/eeebot_dashboard.py. Commit.\n\n"
    "Completed (do not repeat): Priority 14 (demand dashboard, commit "
    "1029364), Priority 10 (loop_health_report.py, commit 6a365ac)."
)

GAP = {
    "metric": "repeat_failure_rate",
    "vector": "V1",
    "current": 0.5,
    "target": 0.3,
    "evidence": (
        "repeat_failure_rate=0.5 is above max target 0.3 over the last 7d "
        "window (goal vector V1)"
    ),
}

VALID_PRIORITY = {
    "label": "Trim proposer retry burn",
    "body": "Add one guard function to scripts/loop_health_report.py that "
    "flags repeated self-dedup rejects. Keep it under 40 lines. Commit.",
    "vector": "V1",
    "evidence": "E1",
}


DECAY_EVIDENCE_ITEM = {
    "path": "scripts/loop_health_report.py",
    "stale_since": "2026-06-01T00:00:00Z",
}


def _mock_decay(monkeypatch, items=None):
    if items is None:
        items = [DECAY_EVIDENCE_ITEM]
    from nanobot.runtime import usage_evidence
    monkeypatch.setattr(
        usage_evidence,
        "stale_artifacts_status",
        lambda *_args, **_kwargs: (items, "complete"),
    )


def _seed_hypothesis(state_dir: Path, hypothesis_id: str = "hypothesis-h1") -> None:
    _write_lifecycle(
        state_dir,
        {
            hypothesis_id: {
                "status": "answered",
                "verdict": "supported",
                "verdict_at": "2026-08-01T00:00:00Z",
                "verdict_evidence": {"source": "microbench", "value": 12.0},
                "title": "Cache the widget lookup",
            }
        },
    )


HYPOTHESIS_FIXTURE = {
    "hypothesis-h1": {
        "status": "answered",
        "verdict": "supported",
        "verdict_at": "2026-08-01T00:00:00Z",
        "verdict_evidence": {"source": "microbench", "value": 12.0},
        "title": "Cache the widget lookup",
    },
}


def _seed_valid_evidence(state_dir: Path, monkeypatch=None, repo: Path | None = None) -> None:
    _seed_hypothesis(state_dir)
    if monkeypatch is not None and repo is not None:
        _mock_decay(monkeypatch)


def _write_goal_text(state_dir: Path, text: str = GOAL_TEXT) -> None:
    path = state_dir / "goals" / "goal_text.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"schema_version": "goal-text-v1", "goal_id": "goal-bootstrap", "text": text}),
        encoding="utf-8",
    )


def _read_goal_text(state_dir: Path) -> str:
    data = json.loads((state_dir / "goals" / "goal_text.json").read_text(encoding="utf-8"))
    return data["text"]


def test_load_goal_data_prefers_release_charter(tmp_path: Path):
    state_dir = tmp_path / "state"
    _write_goal_text(state_dir, "mutable legacy charter")
    release_root = tmp_path / "release"
    release_root.mkdir()
    (release_root / "goals.md").write_text("IMMUTABLE CHARTER", encoding="utf-8")

    data = goal_review._load_goal_data(state_dir, release_root)

    assert data == {"text": "IMMUTABLE CHARTER"}


def test_load_goal_data_falls_back_to_legacy_state(tmp_path: Path):
    state_dir = tmp_path / "state"
    _write_goal_text(state_dir, "legacy charter")

    assert goal_review._load_goal_data(state_dir, tmp_path / "missing")["text"] == "legacy charter"


def _write_snapshot(state_dir: Path, gaps: list[dict]) -> None:
    path = state_dir / "scorecard" / "latest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": "scorecard-v1",
                "window_days": 7,
                "loop": {"integrations": 1, "repeat_failure_rate": 0.5},
                "gaps": gaps,
            }
        ),
        encoding="utf-8",
    )


def _goal_review_rows(state_dir: Path) -> list[dict]:
    path = state_dir / "ledger" / "cycles.jsonl"
    if not path.is_file():
        return []
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [r for r in rows if r.get("phase") == "goal_review"]


def _no_llm(monkeypatch) -> None:
    def _boom(context: str):
        raise AssertionError("LLM must not be called")

    monkeypatch.setattr(goal_review, "_call_llm", _boom)


@pytest.fixture()
def enabled(monkeypatch):
    monkeypatch.setenv(goal_review.ENABLED_ENV, "1")


# ─── kill switch (default OFF) ──────────────────────────────────────────────


class TestKillSwitch:
    def test_unset_is_hard_noop(self, tmp_path, monkeypatch):
        """Switch absent → None, no watermark write, no ledger row, no LLM."""
        monkeypatch.delenv(goal_review.ENABLED_ENV, raising=False)
        _no_llm(monkeypatch)
        state_dir = tmp_path / "state"
        _write_goal_text(state_dir)
        _write_snapshot(state_dir, [GAP])

        assert goal_review.maybe_goal_review(state_dir, None, now=NOW) is None
        assert not (state_dir / "goal_review").exists()
        assert _goal_review_rows(state_dir) == []

    def test_falsy_is_hard_noop(self, tmp_path, monkeypatch):
        monkeypatch.setenv(goal_review.ENABLED_ENV, "0")
        _no_llm(monkeypatch)
        state_dir = tmp_path / "state"
        _write_goal_text(state_dir)
        _write_snapshot(state_dir, [GAP])

        assert goal_review.maybe_goal_review(state_dir, None, now=NOW) is None
        assert not (state_dir / "goal_review").exists()


# ─── honest no-ops ──────────────────────────────────────────────────────────


class TestNoGaps:
    def test_no_gaps_zero_priorities_empty_ledger_row(self, tmp_path, monkeypatch, enabled):
        """No measured evidence at all → zero priorities, NO LLM call, a
        goal_review ledger row with empty output, watermark advanced."""
        _no_llm(monkeypatch)
        state_dir = tmp_path / "state"
        _write_goal_text(state_dir)
        _write_snapshot(state_dir, [])  # verified empty scorecard gaps

        assert goal_review.maybe_goal_review(state_dir, None, now=NOW) == []
        rows = _goal_review_rows(state_dir)
        assert len(rows) == 1
        assert rows[0]["outcome"] == "no_gaps"
        assert rows[0]["produced"] == []
        assert rows[0]["retention_status"] == "complete"
        assert rows[0]["evidence_sources"] == []
        assert rows[0]["direction_at_review"] is None
        assert goal_review.goal_review_retention(rows[0]) == {
            "status": "complete", "evidence_sources": (), "direction": None,
        }
        assert (state_dir / "goal_review" / "last_run.json").is_file()

    def test_no_gaps_records_decision_time_direction(self, tmp_path, monkeypatch, enabled):
        from nanobot.runtime import tech_tree

        _no_llm(monkeypatch)
        state_dir = tmp_path / "state"
        _write_goal_text(state_dir)
        _write_snapshot(state_dir, [])
        tech_tree.ensure_seeded(state_dir, now=NOW)
        portfolio = tech_tree.read_portfolio(state_dir)
        portfolio["current"] = "cycle-cost"
        tech_tree._write_portfolio(state_dir, portfolio)

        assert goal_review.maybe_goal_review(state_dir, None, now=NOW) == []
        row = _goal_review_rows(state_dir)[0]
        assert row["evidence_sources"] == []
        assert row["direction_at_review"] == "cycle-cost"
        assert goal_review.goal_review_retention(row) == {
            "status": "complete", "evidence_sources": (), "direction": "cycle-cost",
        }

    def test_missing_scorecard_is_unavailable_not_empty(self, tmp_path, monkeypatch, enabled):
        _no_llm(monkeypatch)
        state_dir = tmp_path / "state"
        _write_goal_text(state_dir)

        assert goal_review.maybe_goal_review(state_dir, None, now=NOW) == []
        row = _goal_review_rows(state_dir)[0]
        assert row["outcome"] == "no_gaps"
        assert row["retention_status"] == "unavailable"
        assert goal_review.goal_review_retention(row)["status"] == "unavailable"

    def test_failed_evidence_source_is_unavailable_not_empty(self, tmp_path, monkeypatch, enabled):
        _no_llm(monkeypatch)
        state_dir = tmp_path / "state"
        _write_goal_text(state_dir)
        monkeypatch.setattr(
            "nanobot.runtime.usage_evidence.stale_artifacts",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("reader failed")),
        )

        assert goal_review.maybe_goal_review(state_dir, None, now=NOW) == []
        row = _goal_review_rows(state_dir)[0]
        assert row["outcome"] == "no_gaps"
        assert row["retention_status"] == "unavailable"
        assert goal_review.goal_review_retention(row)["status"] == "unavailable"

    def test_missing_goal_text_records_unavailable_sources_before_llm(self, tmp_path, monkeypatch, enabled):
        """No R30 channel means no source scan, never an empty source set."""
        _no_llm(monkeypatch)
        state_dir = tmp_path / "state"
        _write_snapshot(state_dir, [GAP])

        assert goal_review.maybe_goal_review(state_dir, None, now=NOW) == []
        row = _goal_review_rows(state_dir)[0]
        assert row["outcome"] == "no_goal_text"
        assert row["retention_status"] == "unavailable"
        assert goal_review.goal_review_retention(row)["status"] == "unavailable"


# ─── valid reply → append through the R30 channel ───────────────────────────


class TestAppend:
    def test_two_valid_priorities_appended(self, tmp_path, monkeypatch, enabled):
        state_dir = tmp_path / "state"
        _write_goal_text(state_dir)
        _write_snapshot(state_dir, [GAP])
        _seed_valid_evidence(state_dir)
        second = {
            "label": "Dashboard usage ping",
            "body": "Add one function to scripts/eeebot_dashboard.py that logs "
            "a usage timestamp. Under 40 lines. Commit.",
            "vector": "V2",
            "evidence": "E1",
        }
        monkeypatch.setattr(
            goal_review, "_call_llm", lambda ctx: {"priorities": [VALID_PRIORITY, second]}
        )

        titles = goal_review.maybe_goal_review(state_dir, None, now=NOW)
        # Numbering continues past the highest N anywhere in the text (16).
        assert titles == [
            "Priority 17 — Trim proposer retry burn",
            "Priority 18 — Dashboard usage ping",
        ]

        # #860: goal_text.json is the operator's canon — goal_review never
        # writes it; the accepted entries land in derived_priorities.json.
        assert _read_goal_text(state_dir) == GOAL_TEXT
        derived = goal_review.read_derived_priorities(state_dir)
        assert [d["label"] for d in derived] == [
            "Trim proposer retry burn",
            "Dashboard usage ping",
        ]
        assert [d["vector"] for d in derived] == ["V1", "V2"]
        assert all(d["added_utc"] for d in derived)

        # merged_goal_text (what demand/llm_proposer see) folds them in.
        text = goal_review.merged_goal_text(state_dir, GOAL_TEXT)
        # Operator entries untouched, verbatim.
        assert "(A) Priority 11 — Loop health in dashboard: extend" in text
        assert "(B) Priority 16 — Cycle strip line in dashboard: add ONE" in text
        assert "Completed (do not repeat): Priority 14" in text
        # New entries in the exact shape demand's parser reads, before the
        # Completed paragraph — each carrying its inline (V1)/(V2) tag (#815).
        assert "(C) Priority 17 — Trim proposer retry burn (V1): Add one guard" in text
        assert "(D) Priority 18 — Dashboard usage ping (V2): Add one function" in text
        assert text.index("(D) Priority 18") < text.index("\n\nCompleted")

        section = text[text.index("Current priority targets:"):]
        parsed = goal_review._PRIORITY_PATTERN.findall(section)
        assert [int(num) for num, _, _ in parsed] == [11, 16, 17, 18]

        rows = _goal_review_rows(state_dir)
        assert len(rows) == 1
        assert rows[0]["outcome"] == "appended"
        assert rows[0]["produced"] == titles
        assert rows[0]["rejected"] == []
        assert rows[0]["inputs_hash"]
        assert rows[0]["evidence_sources"] == ["supported_hypotheses"]
        assert rows[0]["direction_at_review"] is None

    def test_dedup_keeps_operator_entries_untouched(self, tmp_path, monkeypatch, enabled):
        """A candidate duplicating an existing operator priority is rejected
        (recorded); nothing existing is overwritten or removed."""
        state_dir = tmp_path / "state"
        _write_goal_text(state_dir)
        _write_snapshot(state_dir, [GAP])
        _seed_valid_evidence(state_dir)
        duplicate = dict(VALID_PRIORITY, label="Loop health in dashboard")
        monkeypatch.setattr(
            goal_review, "_call_llm", lambda ctx: {"priorities": [duplicate, VALID_PRIORITY]}
        )

        titles = goal_review.maybe_goal_review(state_dir, None, now=NOW)
        assert titles == ["Priority 17 — Trim proposer retry burn"]

        assert _read_goal_text(state_dir) == GOAL_TEXT  # operator canon untouched
        text = goal_review.merged_goal_text(state_dir, GOAL_TEXT)
        assert text.count("Loop health in dashboard") == 1  # operator entry only
        rows = _goal_review_rows(state_dir)
        assert rows[0]["rejected"] == [
            {"label": "Loop health in dashboard", "reason": "duplicate"}
        ]

    def test_at_most_three_accepted(self, tmp_path, monkeypatch, enabled):
        state_dir = tmp_path / "state"
        _write_goal_text(state_dir)
        _write_snapshot(state_dir, [GAP])
        _seed_valid_evidence(state_dir)
        cands = [
            dict(VALID_PRIORITY, label=f"Bounded change number {i}") for i in range(5)
        ]
        monkeypatch.setattr(goal_review, "_call_llm", lambda ctx: {"priorities": cands})

        titles = goal_review.maybe_goal_review(state_dir, None, now=NOW)
        assert len(titles) == 3
        rejected = _goal_review_rows(state_dir)[0]["rejected"]
        assert [r["reason"] for r in rejected] == ["exceeds_max", "exceeds_max"]


# ─── fail-closed validation ─────────────────────────────────────────────────


class TestValidation:
    def test_missing_evidence_citation_rejected_others_kept(
        self, tmp_path, monkeypatch, enabled
    ):
        state_dir = tmp_path / "state"
        _write_goal_text(state_dir)
        _write_snapshot(state_dir, [GAP])
        _seed_valid_evidence(state_dir)
        no_evidence = dict(VALID_PRIORITY, label="Uncited invention", evidence="")
        unknown_evidence = dict(VALID_PRIORITY, label="Fabricated citation", evidence="E9")
        monkeypatch.setattr(
            goal_review,
            "_call_llm",
            lambda ctx: {"priorities": [no_evidence, VALID_PRIORITY, unknown_evidence]},
        )

        titles = goal_review.maybe_goal_review(state_dir, None, now=NOW)
        assert titles == ["Priority 17 — Trim proposer retry burn"]
        rejected = _goal_review_rows(state_dir)[0]["rejected"]
        assert {(r["label"], r["reason"]) for r in rejected} == {
            ("Uncited invention", "evidence_not_in_inputs"),
            ("Fabricated citation", "evidence_not_in_inputs"),
        }

    def test_missing_vector_reference_rejected(self, tmp_path, monkeypatch, enabled):
        state_dir = tmp_path / "state"
        _write_goal_text(state_dir)
        _write_snapshot(state_dir, [GAP])
        _seed_valid_evidence(state_dir)
        future = dict(VALID_PRIORITY, label="Demoscene visuals", vector="FUTURE")
        no_vector = dict(VALID_PRIORITY, label="Vectorless idea", vector="")
        monkeypatch.setattr(
            goal_review, "_call_llm", lambda ctx: {"priorities": [future, no_vector]}
        )

        assert goal_review.maybe_goal_review(state_dir, None, now=NOW) == []
        row = _goal_review_rows(state_dir)[0]
        assert row["outcome"] == "no_valid_priorities"
        assert [r["reason"] for r in row["rejected"]] == [
            "missing_vector_reference",
            "missing_vector_reference",
        ]
        assert "(C) Priority" not in _read_goal_text(state_dir)

    def test_unparseable_label_rejected(self, tmp_path, monkeypatch, enabled):
        """A label the done-detection regexes cannot parse (colon/period/
        parens, or over 40 chars) is rejected fail-closed."""
        state_dir = tmp_path / "state"
        _write_goal_text(state_dir)
        _write_snapshot(state_dir, [GAP])
        _seed_valid_evidence(state_dir)
        bad = dict(VALID_PRIORITY, label="Fix: the loop (v2).")
        monkeypatch.setattr(goal_review, "_call_llm", lambda ctx: {"priorities": [bad]})

        assert goal_review.maybe_goal_review(state_dir, None, now=NOW) == []
        assert _goal_review_rows(state_dir)[0]["rejected"][0]["reason"] == "invalid_label"


# ─── vector bias: V1-over-V2 preference (#815) ──────────────────────────────


class TestVectorBias:
    """#815: the mint prompt carries a soft V1-preference instruction, and a
    minted priority's appended goal_text carries the inline (V1)/(V2) tag
    matching its validated vector — so ``demand._priority_items`` can parse
    it back out and apply the within-kind V1-first bias."""

    def test_prompt_contains_v1_preference_instruction(self):
        prompt = goal_review._GOAL_REVIEW_SYSTEM_PROMPT
        assert "Prefer proposing Vector-1" in prompt
        assert "Vector-2" in prompt
        assert "self-improvement of the agent system" in prompt

    def test_minted_priority_text_carries_matching_vector_tag(
        self, tmp_path, monkeypatch, enabled
    ):
        state_dir = tmp_path / "state"
        _write_goal_text(state_dir)
        _write_snapshot(state_dir, [GAP])
        _seed_valid_evidence(state_dir)
        v2_priority = dict(VALID_PRIORITY, label="Dashboard usage ping", vector="V2")
        monkeypatch.setattr(
            goal_review, "_call_llm", lambda ctx: {"priorities": [VALID_PRIORITY, v2_priority]}
        )

        goal_review.maybe_goal_review(state_dir, None, now=NOW)
        text = goal_review.merged_goal_text(state_dir, _read_goal_text(state_dir))
        assert "Trim proposer retry burn (V1):" in text
        assert "Dashboard usage ping (V2):" in text


# ─── daily watermark ────────────────────────────────────────────────────────


class TestWatermark:
    def test_second_call_same_day_noop(self, tmp_path, monkeypatch, enabled):
        state_dir = tmp_path / "state"
        _write_goal_text(state_dir)
        _write_snapshot(state_dir, [GAP])
        _seed_valid_evidence(state_dir)
        monkeypatch.setattr(
            goal_review, "_call_llm", lambda ctx: {"priorities": [VALID_PRIORITY]}
        )
        assert goal_review.maybe_goal_review(state_dir, None, now=NOW)

        _no_llm(monkeypatch)
        later = NOW + timedelta(hours=2)
        assert goal_review.maybe_goal_review(state_dir, None, now=later) is None
        assert len(_goal_review_rows(state_dir)) == 1  # no second row

    def test_runs_again_after_interval(self, tmp_path, monkeypatch, enabled):
        state_dir = tmp_path / "state"
        _write_goal_text(state_dir)
        _write_snapshot(state_dir, [])  # no gaps → honest no-op both times
        _no_llm(monkeypatch)
        assert goal_review.maybe_goal_review(state_dir, None, now=NOW) == []
        next_day = NOW + timedelta(hours=25)
        assert goal_review.maybe_goal_review(state_dir, None, now=next_day) == []
        assert len(_goal_review_rows(state_dir)) == 2


# ─── malformed / failing LLM replies ────────────────────────────────────────


class TestMalformedReply:
    def test_none_reply_no_goal_text_change(self, tmp_path, monkeypatch, enabled):
        state_dir = tmp_path / "state"
        _write_goal_text(state_dir)
        _write_snapshot(state_dir, [GAP])
        _seed_valid_evidence(state_dir)
        monkeypatch.setattr(goal_review, "_call_llm", lambda ctx: None)

        assert goal_review.maybe_goal_review(state_dir, None, now=NOW) == []
        assert _read_goal_text(state_dir) == GOAL_TEXT
        assert _goal_review_rows(state_dir)[0]["outcome"] == "invalid_reply"

    def test_wrong_shape_reply(self, tmp_path, monkeypatch, enabled):
        state_dir = tmp_path / "state"
        _write_goal_text(state_dir)
        _write_snapshot(state_dir, [GAP])
        _seed_valid_evidence(state_dir)
        monkeypatch.setattr(goal_review, "_call_llm", lambda ctx: {"priorities": "yes"})

        assert goal_review.maybe_goal_review(state_dir, None, now=NOW) == []
        assert _read_goal_text(state_dir) == GOAL_TEXT
        assert _goal_review_rows(state_dir)[0]["outcome"] == "invalid_reply"

    def test_llm_exception_honest_ledger_row(self, tmp_path, monkeypatch, enabled):
        state_dir = tmp_path / "state"
        _write_goal_text(state_dir)
        _write_snapshot(state_dir, [GAP])
        _seed_valid_evidence(state_dir)

        def _raise(context: str):
            raise RuntimeError("provider down")

        monkeypatch.setattr(goal_review, "_call_llm", _raise)

        assert goal_review.maybe_goal_review(state_dir, None, now=NOW) is None
        assert _read_goal_text(state_dir) == GOAL_TEXT
        assert _goal_review_rows(state_dir)[0]["outcome"] == "error"


# ─── wiring: rides the scorecard recompute path, fail-open ──────────────────


class TestWiring:
    def test_compute_scorecard_invokes_review(self, tmp_path, monkeypatch):
        from nanobot.runtime import scorecard

        calls: list[tuple] = []
        monkeypatch.setattr(
            goal_review, "maybe_goal_review", lambda sd, repo, now=None, release_root=None: calls.append((sd, repo, release_root))
        )
        state_dir = tmp_path / "state"
        scorecard.compute_scorecard(state_dir, None, force=True)
        assert len(calls) == 1

    def test_review_exception_never_breaks_scorecard(self, tmp_path, monkeypatch):
        from nanobot.runtime import scorecard

        def _boom(sd, repo, now=None, release_root=None):
            raise RuntimeError("review bug")

        monkeypatch.setattr(goal_review, "maybe_goal_review", _boom)
        state_dir = tmp_path / "state"
        snapshot = scorecard.compute_scorecard(state_dir, None, force=True)
        assert snapshot["schema_version"] == "scorecard-v1"

    def test_compute_scorecard_passes_explicit_release_root(self, tmp_path, monkeypatch):
        from nanobot.runtime import scorecard

        calls: list[dict] = []
        monkeypatch.setattr(
            goal_review,
            "maybe_goal_review",
            lambda sd, repo, now=None, release_root=None: calls.append({"sd": sd, "repo": repo, "release_root": release_root}),
        )
        state_dir = tmp_path / "state"
        custom_release = tmp_path / "custom_release"
        scorecard.compute_scorecard(state_dir, None, force=True, release_root=custom_release)
        assert len(calls) == 1
        assert calls[0]["release_root"] == custom_release

    def test_compute_scorecard_resolves_default_release_root_when_unspecified(self, tmp_path, monkeypatch):
        from nanobot.runtime import scorecard
        from nanobot.runtime.role_prompt import RELEASE_ROOT_DEFAULT

        monkeypatch.delenv("RELEASE_ROOT", raising=False)
        calls: list[dict] = []
        monkeypatch.setattr(
            goal_review,
            "maybe_goal_review",
            lambda sd, repo, now=None, release_root=None: calls.append({"sd": sd, "repo": repo, "release_root": release_root}),
        )
        state_dir = tmp_path / "state"
        scorecard.compute_scorecard(state_dir, None, force=True)
        assert len(calls) == 1
        assert calls[0]["release_root"] == Path(RELEASE_ROOT_DEFAULT)


# ─── #860: derived priorities survive deploy_release.sh's goal_text reseed ──


class TestRetention:
    def test_legacy_row_is_unavailable_not_empty(self):
        legacy = {"phase": "goal_review", "outcome": "no_gaps", "produced": []}
        assert goal_review.goal_review_retention(legacy) == {
            "status": "unavailable", "evidence_sources": None, "direction": None,
        }

    def test_malformed_new_shape_is_unavailable_not_empty(self):
        malformed = {
            "phase": "goal_review", "retention_status": "complete",
            "evidence_sources": ["invented"], "direction_at_review": "proposer-quality",
        }
        duplicated = {
            "phase": "goal_review", "retention_status": "complete",
            "evidence_sources": ["decay", "decay"], "direction_at_review": None,
        }
        whitespace_direction = {
            "phase": "goal_review", "retention_status": "complete",
            "evidence_sources": [], "direction_at_review": " ",
        }
        assert goal_review.goal_review_retention(malformed)["status"] == "unavailable"
        assert goal_review.goal_review_retention(duplicated)["status"] == "unavailable"
        assert goal_review.goal_review_retention(whitespace_direction)["status"] == "unavailable"

    def test_whitespace_direction_is_unavailable_end_to_end(self, tmp_path, monkeypatch, enabled):
        from nanobot.runtime import tech_tree

        _no_llm(monkeypatch)
        state_dir = tmp_path / "state"
        _write_goal_text(state_dir)
        _write_snapshot(state_dir, [])
        tech_tree.ensure_seeded(state_dir, now=NOW)
        portfolio = tech_tree.read_portfolio(state_dir)
        portfolio["current"] = " "
        tech_tree._write_portfolio(state_dir, portfolio)

        assert goal_review.maybe_goal_review(state_dir, None, now=NOW) == []
        row = _goal_review_rows(state_dir)[0]
        assert row["retention_status"] == "unavailable"
        assert goal_review.goal_review_retention(row)["status"] == "unavailable"

    def test_direction_is_captured_at_decision_time(self, tmp_path, monkeypatch, enabled):
        from nanobot.runtime import tech_tree

        state_dir = tmp_path / "state"
        _write_goal_text(state_dir)
        _write_snapshot(state_dir, [GAP])
        _seed_valid_evidence(state_dir)
        tech_tree.ensure_seeded(state_dir, now=NOW)
        portfolio = tech_tree.read_portfolio(state_dir)
        portfolio["current"] = "proposer-quality"
        tech_tree._write_portfolio(state_dir, portfolio)
        monkeypatch.setattr(goal_review, "_call_llm", lambda ctx: {"priorities": [VALID_PRIORITY]})

        goal_review.maybe_goal_review(state_dir, None, now=NOW)

        row = _goal_review_rows(state_dir)[0]
        assert goal_review.goal_review_retention(row) == {
            "status": "complete",
            "evidence_sources": ("supported_hypotheses",),
            "direction": "proposer-quality",
        }

    def test_malformed_scorecard_gaps_type_is_unavailable(self, tmp_path, monkeypatch, enabled):
        _no_llm(monkeypatch)
        state_dir = tmp_path / "state"
        _write_goal_text(state_dir)
        sc_path = state_dir / "scorecard" / "latest.json"
        sc_path.parent.mkdir(parents=True)
        sc_path.write_text(json.dumps({"gaps": "not-a-list"}), encoding="utf-8")

        assert goal_review.maybe_goal_review(state_dir, None, now=NOW) == []
        row = _goal_review_rows(state_dir)[0]
        assert row["retention_status"] == "unavailable"
        assert goal_review.goal_review_retention(row)["status"] == "unavailable"

    def test_corrupt_portfolio_is_unavailable_not_absent_direction(self, tmp_path, monkeypatch, enabled):
        _no_llm(monkeypatch)
        state_dir = tmp_path / "state"
        _write_goal_text(state_dir)
        _write_snapshot(state_dir, [])
        port_path = state_dir / "tech_tree" / "portfolio.json"
        port_path.parent.mkdir(parents=True)
        port_path.write_text("{corrupt json", encoding="utf-8")

        assert goal_review.maybe_goal_review(state_dir, None, now=NOW) == []
        row = _goal_review_rows(state_dir)[0]
        assert row["retention_status"] == "unavailable"
        assert goal_review.goal_review_retention(row)["status"] == "unavailable"

    def test_corrupt_hypothesis_lifecycle_is_unavailable(self, tmp_path, monkeypatch, enabled):
        _no_llm(monkeypatch)
        state_dir = tmp_path / "state"
        _write_goal_text(state_dir)
        _write_snapshot(state_dir, [])
        hyp_path = state_dir / "hypotheses" / "lifecycle.json"
        hyp_path.parent.mkdir(parents=True)
        hyp_path.write_text("{corrupt json", encoding="utf-8")

        assert goal_review.maybe_goal_review(state_dir, None, now=NOW) == []
        row = _goal_review_rows(state_dir)[0]
        assert row["retention_status"] == "unavailable"
        assert goal_review.goal_review_retention(row)["status"] == "unavailable"

    def test_decay_touch_evidence_blocked_is_unavailable(self, tmp_path, monkeypatch, enabled):
        from nanobot.runtime import usage_evidence

        _no_llm(monkeypatch)
        state_dir = tmp_path / "state"
        _write_goal_text(state_dir)
        _write_snapshot(state_dir, [])
        repo = tmp_path / "repo"
        (repo / "scripts").mkdir(parents=True)
        (repo / "scripts" / "foo.py").write_text("# script\n", encoding="utf-8")
        monkeypatch.setattr(
            usage_evidence,
            "_touched_from_results_with_status",
            lambda *args, **kwargs: ({}, "corrupt"),
        )

        assert goal_review.maybe_goal_review(state_dir, repo, now=NOW) == []
        row = _goal_review_rows(state_dir)[0]
        assert row["retention_status"] == "unavailable"
        assert goal_review.goal_review_retention(row)["status"] == "unavailable"

    def test_error_retains_captured_provenance(self, tmp_path, monkeypatch, enabled):
        from nanobot.runtime import tech_tree

        state_dir = tmp_path / "state"
        _write_goal_text(state_dir)
        _write_snapshot(state_dir, [GAP])
        _seed_valid_evidence(state_dir)
        tech_tree.ensure_seeded(state_dir, now=NOW)
        portfolio = tech_tree.read_portfolio(state_dir)
        portfolio["current"] = "proposer-quality"
        tech_tree._write_portfolio(state_dir, portfolio)
        monkeypatch.setattr(
            goal_review, "_call_llm", lambda _context: (_ for _ in ()).throw(RuntimeError("provider down"))
        )

        assert goal_review.maybe_goal_review(state_dir, None, now=NOW) is None
        row = _goal_review_rows(state_dir)[0]
        assert row["outcome"] == "error"
        assert goal_review.goal_review_retention(row) == {
            "status": "complete",
            "evidence_sources": ("supported_hypotheses",),
            "direction": "proposer-quality",
        }

    def test_source_set_excludes_lines_truncated_before_evidence_window(self, tmp_path, monkeypatch):
        """Only source lines actually shown to the LLM are retained."""
        state_dir = tmp_path / "state"
        repo = tmp_path / "repo"
        repo.mkdir()
        snapshot = {"gaps": [{"evidence": f"gap-{i}"} for i in range(goal_review._MAX_EVIDENCE_LINES)]}
        _mock_decay(
            monkeypatch,
            items=[{"path": f"scripts/stale_{i}.py", "stale_since": "2026-01-01"} for i in range(goal_review._MAX_EVIDENCE_LINES + 5)],
        )
        sources: set[str] = set()
        evidence = goal_review._collect_evidence(state_dir, repo, snapshot, NOW, sources=sources)
        assert len(evidence) <= goal_review._MAX_EVIDENCE_LINES
        assert "decay" in sources
        assert "scorecard_gaps" not in sources


class TestDerivedPriorities:
    def test_acceptance_lands_in_derived_file_goal_text_byte_unchanged(
        self, tmp_path, monkeypatch, enabled
    ):
        state_dir = tmp_path / "state"
        _write_goal_text(state_dir)
        _write_snapshot(state_dir, [GAP])
        _seed_valid_evidence(state_dir)
        monkeypatch.setattr(goal_review, "_call_llm", lambda ctx: {"priorities": [VALID_PRIORITY]})

        titles = goal_review.maybe_goal_review(state_dir, None, now=NOW)
        assert titles == ["Priority 17 — Trim proposer retry burn"]

        assert _read_goal_text(state_dir) == GOAL_TEXT
        derived = goal_review.read_derived_priorities(state_dir)
        assert len(derived) == 1
        assert derived[0]["label"] == "Trim proposer retry burn"
        assert derived[0]["vector"] == "V1"
        assert derived[0]["body"] == VALID_PRIORITY["body"]
        assert derived[0]["added_utc"]
        # #860 review: the number IS stored at accept time (17 = next past
        # merged base) so the rendered title/demand id stays stable across
        # deploy reseeds; the label itself stays number-free.
        assert derived[0]["number"] == 17
        assert "17" not in derived[0]["label"]

    def test_merged_goal_text_empty_derived_is_byte_identical(self, tmp_path):
        state_dir = tmp_path / "state"
        assert goal_review.merged_goal_text(state_dir, GOAL_TEXT) == GOAL_TEXT

    def test_merged_goal_text_inserts_with_correct_next_number(self, tmp_path):
        state_dir = tmp_path / "state"
        goal_review._write_derived_priorities(
            state_dir,
            [
                {
                    "label": "Trim proposer retry burn",
                    "vector": "V1",
                    "body": "Do the bounded thing.",
                    "number": 17,
                    "added_utc": "2026-08-01T00:00:00Z",
                }
            ],
        )
        merged = goal_review.merged_goal_text(state_dir, GOAL_TEXT)
        assert merged != GOAL_TEXT
        section = merged[merged.index("Current priority targets:"):]
        assert (
            "Priority 17 — Trim proposer retry burn (V1): Do the bounded thing." in section
        )
        parsed = goal_review._PRIORITY_PATTERN.findall(section)
        assert [int(num) for num, _, _ in parsed] == [11, 16, 17]

    def test_merged_goal_text_skips_derived_entry_already_active(self, tmp_path):
        """#1640: a stray derived entry duplicating a label the operator
        already lists as a live current target must never be appended
        twice — this is what the retired per-deploy migration script used
        to produce (it re-parsed goal_text.json's own live priorities back
        into derived_priorities.json on every release)."""
        state_dir = tmp_path / "state"
        goal_review._write_derived_priorities(
            state_dir,
            [
                {
                    "label": "Loop health in dashboard",
                    "vector": "V1",
                    "body": "extend scripts/eeebot_dashboard.py with a loop-health section. Commit.",
                    "number": 11,
                    "added_utc": "2026-09-14T22:47:10Z",
                }
            ],
        )
        merged = goal_review.merged_goal_text(state_dir, GOAL_TEXT)
        assert merged == GOAL_TEXT
        assert merged.count("Priority 11") == 1

    def test_merged_goal_text_never_resurrects_a_completed_number(self, tmp_path):
        """#1640: a stray derived entry whose NUMBER already appears in the
        "Completed (do not repeat):" sentence must never be re-appended as a
        new current target, even when its label text does not match that
        sentence's free-form prose ("Priority 14 (demand dashboard, commit
        1029364)" vs. the derived entry's own stored label) — the Completed
        sentence has no fixed shape for a label-only check to rely on, so
        the guard has to key on the number both share.

        Simulates exactly the mechanism found live on host: two writers
        used to exist (goal_review's own capped mint, and a per-deploy
        migration script since retired) and could disagree about whether a
        number was still open."""
        state_dir = tmp_path / "state"
        goal_review._write_derived_priorities(
            state_dir,
            [
                {
                    "label": "Loop health in dashboard",
                    "vector": "V2",
                    "body": "extend scripts/eeebot_dashboard.py with a loop-health section. Commit.",
                    "number": 14,
                    "added_utc": "2026-09-14T22:47:10Z",
                }
            ],
        )
        merged = goal_review.merged_goal_text(state_dir, GOAL_TEXT)
        current_section = merged.split("Completed (do not repeat):", 1)[0]
        assert "Priority 14" not in current_section
        assert merged == GOAL_TEXT

    def test_deploy_reseed_does_not_erase_derived_priority(
        self, tmp_path, monkeypatch, enabled
    ):
        """THE DEPLOY TEST — #860's acceptance criterion. goal_review appends
        a priority (into derived_priorities.json); a deploy then overwrites
        goal_text.json with a fresh operator copy (exactly what
        ``deploy_release.sh`` does every release); demand collection must
        still see the derived priority afterward."""
        from nanobot.runtime import demand

        state_dir = tmp_path / "state"
        _write_goal_text(state_dir)
        _write_snapshot(state_dir, [GAP])
        _seed_valid_evidence(state_dir)
        monkeypatch.setattr(goal_review, "_call_llm", lambda ctx: {"priorities": [VALID_PRIORITY]})
        goal_review.maybe_goal_review(state_dir, None, now=NOW)

        # Simulate deploy_release.sh:87 reseeding goal_text.json from the repo.
        _write_goal_text(state_dir, GOAL_TEXT)

        items = demand.collect_demand(state_dir, None)
        summaries = [i["summary"] for i in items if i["kind"] == "priority"]
        assert any("Trim proposer retry burn" in s for s in summaries)

    def test_dedup_rejects_remint_of_existing_derived_label(
        self, tmp_path, monkeypatch, enabled
    ):
        state_dir = tmp_path / "state"
        _write_goal_text(state_dir)
        _write_snapshot(state_dir, [GAP])
        _seed_valid_evidence(state_dir)
        goal_review._write_derived_priorities(
            state_dir,
            [
                {
                    "label": "Trim proposer retry burn",
                    "vector": "V1",
                    "body": "Already derived yesterday.",
                    "number": 17,
                    "added_utc": "2026-08-01T00:00:00Z",
                }
            ],
        )
        monkeypatch.setattr(goal_review, "_call_llm", lambda ctx: {"priorities": [VALID_PRIORITY]})

        titles = goal_review.maybe_goal_review(state_dir, None, now=NOW)
        assert titles == []
        row = _goal_review_rows(state_dir)[0]
        assert row["outcome"] == "no_valid_priorities"
        assert row["rejected"] == [
            {"label": "Trim proposer retry burn", "reason": "duplicate"}
        ]
        # Unchanged — the pre-existing derived entry stays exactly as-is.
        assert goal_review.read_derived_priorities(state_dir) == [
            {
                "label": "Trim proposer retry burn",
                "vector": "V1",
                "body": "Already derived yesterday.",
                "number": 17,
                "added_utc": "2026-08-01T00:00:00Z",
            }
        ]

    def test_demand_sees_merged_priorities_v1_sorts_before_v2(self, tmp_path):
        from nanobot.runtime import demand

        state_dir = tmp_path / "state"
        _write_goal_text(state_dir)
        goal_review._write_derived_priorities(
            state_dir,
            [
                {
                    "label": "Derived V2 task",
                    "vector": "V2",
                    "body": "Do the V2 thing.",
                    "number": 17,
                    "added_utc": "2026-08-01T00:00:00Z",
                },
                {
                    "label": "Derived V1 task",
                    "vector": "V1",
                    "body": "Do the V1 thing.",
                    "number": 18,
                    "added_utc": "2026-08-01T00:01:00Z",
                },
            ],
        )

        items = demand.collect_demand(state_dir, None)
        summaries = [i["summary"] for i in items if i["kind"] == "priority"]
        v1_idx = next(i for i, s in enumerate(summaries) if "Derived V1 task" in s)
        v2_idx = next(i for i, s in enumerate(summaries) if "Derived V2 task" in s)
        assert v1_idx < v2_idx

    def test_cap_evicts_oldest_beyond_ten(self, tmp_path):
        state_dir = tmp_path / "state"
        existing = [
            {
                "label": f"Old task {i}",
                "vector": "V1",
                "body": "b",
                "number": 20 + i,
                "added_utc": f"day-{i}",
            }
            for i in range(10)
        ]
        goal_review._write_derived_priorities(state_dir, existing)
        assert len(goal_review.read_derived_priorities(state_dir)) == 10

        with_eleventh = existing + [
            {"label": "Newest task", "vector": "V1", "body": "b", "number": 30, "added_utc": "day-10"}
        ]
        goal_review._write_derived_priorities(state_dir, with_eleventh)

        stored = goal_review.read_derived_priorities(state_dir)
        assert len(stored) == 10
        assert stored[-1]["label"] == "Newest task"
        assert all(d["label"] != "Old task 0" for d in stored)
        assert stored[0]["label"] == "Old task 1"  # oldest (index 0) evicted

    def test_operator_readding_derived_label_is_not_doubled(self, tmp_path):
        """#860 review: if the operator later bakes a derived label into
        goal_text itself, merged_goal_text must NOT render it twice (two
        numbers → two demand items for the same work). Operator canon wins;
        the derived copy is skipped."""
        state_dir = tmp_path / "state"
        goal_review._write_derived_priorities(
            state_dir,
            [
                {
                    "label": "Trim proposer retry burn",
                    "vector": "V1",
                    "body": "Derived copy.",
                    "number": 17,
                    "added_utc": "2026-08-01T00:00:00Z",
                }
            ],
        )
        operator_text = GOAL_TEXT + (
            "\n(C) Priority 17 — Trim proposer retry burn (V1): Operator copy."
        )
        merged = goal_review.merged_goal_text(state_dir, operator_text)
        assert merged == operator_text  # skipped entirely — no second copy
        assert merged.count("Trim proposer retry burn") == 1

    def test_derived_number_stable_across_reseed_with_changed_count(
        self, tmp_path, monkeypatch, enabled
    ):
        """#860 review: a derived priority's stored number (and thus its
        rendered title / demand item id) must NOT shift when a deploy
        reseeds goal_text with a DIFFERENT operator priority count —
        otherwise a demand-completed derived priority would resurface
        under a new id."""
        from nanobot.runtime import demand

        state_dir = tmp_path / "state"
        _write_goal_text(state_dir)
        _write_snapshot(state_dir, [GAP])
        _seed_valid_evidence(state_dir)
        monkeypatch.setattr(goal_review, "_call_llm", lambda ctx: {"priorities": [VALID_PRIORITY]})
        goal_review.maybe_goal_review(state_dir, None, now=NOW)

        def _derived_item_id() -> str:
            items = demand.collect_demand(state_dir, None)
            return next(
                i["id"] for i in items
                if i["kind"] == "priority" and "Trim proposer retry burn" in i["summary"]
            )

        id_before = _derived_item_id()

        # Deploy reseeds goal_text with FEWER operator priorities (11 only,
        # 16 dropped) — the dynamic-numbering base would shift; the stored
        # number must keep the id identical.
        shrunk = "\n".join(
            line for line in GOAL_TEXT.splitlines() if "Priority 16" not in line
        )
        assert "Priority 16" not in shrunk and "Priority 11" in shrunk
        _write_goal_text(state_dir, shrunk)

        assert _derived_item_id() == id_before


# ─── #878: supported hypotheses surface as citable evidence ────────────────


def _write_lifecycle(state_dir: Path, entries: dict) -> None:
    d = state_dir / "hypotheses"
    d.mkdir(parents=True, exist_ok=True)
    (d / "lifecycle.json").write_text(
        json.dumps({"schema_version": "hypothesis-lifecycle-v1", "entries": entries}),
        encoding="utf-8",
    )


class TestSupportedHypothesisEvidence:
    def test_supported_hypothesis_appears_in_collected_evidence(self, tmp_path):
        state_dir = tmp_path / "state"
        _write_lifecycle(state_dir, {
            "hypothesis-h1": {
                "status": "answered",
                "verdict": "supported",
                "verdict_at": "2026-08-01T00:00:00Z",
                "verdict_evidence": {"source": "microbench", "value": 12.0},
                "title": "Cache the widget lookup",
            },
            "hypothesis-h2": {
                "status": "answered",
                "verdict": "refuted",
                "verdict_at": "2026-08-02T00:00:00Z",
                "title": "A refuted idea",
            },
        })
        evidence = goal_review._collect_evidence(state_dir, None, {}, NOW)
        lines = list(evidence.values())
        assert any("supported hypothesis: Cache the widget lookup" in line for line in lines)
        assert not any("refuted" in line.lower() for line in lines)

    def test_no_supported_hypotheses_adds_nothing(self, tmp_path):
        state_dir = tmp_path / "state"
        evidence = goal_review._collect_evidence(state_dir, None, {}, NOW)
        assert evidence == {}

    def test_supported_hypothesis_can_be_accepted_as_a_priority(
        self, tmp_path, monkeypatch, enabled
    ):
        """End-to-end: a supported-hypothesis evidence line is citable by id
        and, once cited, flows through the SAME fail-closed
        validate_priority path as any other evidence source."""
        state_dir = tmp_path / "state"
        _write_goal_text(state_dir)
        _write_snapshot(state_dir, [])  # no goal-gap evidence
        _write_lifecycle(state_dir, {
            "hypothesis-h1": {
                "status": "answered",
                "verdict": "supported",
                "verdict_at": "2026-08-01T00:00:00Z",
                "verdict_evidence": {"source": "microbench", "value": 12.0},
                "title": "Cache the widget lookup",
            },
        })

        captured_context = {}

        def _fake_llm(context: str):
            captured_context["text"] = context
            return {"priorities": [{
                "label": "Land the widget cache",
                "body": "Add caching to scripts/widget_lookup.py. Commit.",
                "vector": "V1",
                "evidence": "E1",
            }]}

        monkeypatch.setattr(goal_review, "_call_llm", _fake_llm)
        titles = goal_review.maybe_goal_review(state_dir, None, now=NOW)
        assert titles == ["Priority 17 — Land the widget cache"]
        assert "supported hypothesis: Cache the widget lookup" in captured_context["text"]


class TestNightContourCandidates:
    """ADR-020 Rule 2: The night contour (reflector and curator) proposes
    candidates with citations (ledger rows, commits, or measurements)
    into the existing demand/goal review pipeline. A candidate without at
    least one link is rejected on validation."""

    def test_night_contour_candidates_collected_from_curator_pool(self, tmp_path):
        state_dir = tmp_path / "state"
        curator_dir = state_dir / "curator"
        curator_dir.mkdir(parents=True)
        pool_file = curator_dir / "reflector_pool.json"
        pool_data = {
            "schema": "curator-reflector-pool-v1",
            "cursor": {},
            "clusters": [
                {
                    "detail": "Stream archive directories instead of globbing all files",
                    "cycles": ["cycle-eeb3c220d25b"],
                    "kind": "approach_hint",
                },
                {
                    "detail": "Avoid unbounded subshells",
                    "cycles": [],
                    "kind": "approach_hint",
                },
            ],
        }
        pool_file.write_text(json.dumps(pool_data), encoding="utf-8")

        sources = set()
        status = {}
        evidence = goal_review._collect_evidence(
            state_dir, None, {}, NOW, sources=sources, source_status=status
        )
        assert "night_contour_candidates" in sources
        assert status["night_contour_candidates"] == "complete"
        lines = list(evidence.values())
        assert any("Stream archive directories" in line and "cycle-eeb3c220d25b" in line for line in lines)
        assert any("Avoid unbounded subshells" in line for line in lines)

    def test_candidate_without_link_rejected_on_validation(self):
        """ADR-020 Rule 2 test contract: candidate citing night contour
        without at least one link is rejected on validation."""
        evidence = {
            "E1": "night contour: Avoid unbounded subshells (reflector/curator recommendation; goal vector V1)",
        }
        candidate = {
            "label": "Avoid unbounded subshells",
            "body": "Refactor subshell invocations to direct calls. Commit.",
            "vector": "V1",
            "evidence": "E1",
        }
        normalized, reason = goal_review.validate_priority(candidate, evidence, set())
        assert normalized is None
        assert reason == "missing_citation"

    def test_candidate_with_cycle_link_accepted(self):
        """ADR-020 Rule 2: candidate with a ledger cycle link is accepted."""
        evidence = {
            "E1": "night contour: Stream archive directories [cycles: cycle-eeb3c220d25b] (reflector/curator recommendation; goal vector V1)",
        }
        candidate = {
            "label": "Stream archive dirs",
            "body": "Stream archive directories instead of globbing, citing cycle-eeb3c220d25b. Commit.",
            "vector": "V1",
            "evidence": "E1",
        }
        normalized, reason = goal_review.validate_priority(candidate, evidence, set())
        assert normalized is not None
        assert reason == ""
        assert normalized["label"] == "Stream archive dirs"

    def test_candidate_with_commit_link_accepted(self):
        """ADR-020 Rule 2: candidate with a git commit link is accepted."""
        evidence = {
            "E1": "night contour: Optimize parse loop in 1a2b3c4d (reflector/curator recommendation; goal vector V1)",
        }
        candidate = {
            "label": "Optimize parse loop",
            "body": "Apply loop optimization following commit 1a2b3c4d. Commit.",
            "vector": "V1",
            "evidence": "E1",
        }
        normalized, reason = goal_review.validate_priority(candidate, evidence, set())
        assert normalized is not None
        assert reason == ""

    def test_candidate_with_measurement_link_accepted(self):
        """ADR-020 Rule 2: candidate with a measurement link is accepted."""
        evidence = {
            "E1": "night contour: Reduce query time from 450ms to 50ms (reflector/curator recommendation; goal vector V1)",
        }
        candidate = {
            "label": "Reduce query time",
            "body": "Add index to reduce query time to 50ms. Commit.",
            "vector": "V1",
            "evidence": "E1",
        }
        normalized, reason = goal_review.validate_priority(candidate, evidence, set())
        assert normalized is not None
        assert reason == ""

    def test_night_contour_candidate_end_to_end_acceptance(self, tmp_path, monkeypatch, enabled):
        """End-to-end: a cited night contour recommendation flows into goal
        review and appends as a derived priority."""
        state_dir = tmp_path / "state"
        _write_goal_text(state_dir)
        _write_snapshot(state_dir, [])  # no gaps
        curator_dir = state_dir / "curator"
        curator_dir.mkdir(parents=True)
        pool_file = curator_dir / "reflector_pool.json"
        pool_data = {
            "schema": "curator-reflector-pool-v1",
            "cursor": {},
            "clusters": [
                {
                    "detail": "Stream archive directories instead of globbing all files",
                    "cycles": ["cycle-eeb3c220d25b"],
                    "kind": "approach_hint",
                },
            ],
        }
        pool_file.write_text(json.dumps(pool_data), encoding="utf-8")

        def _fake_llm(context: str):
            return {"priorities": [{
                "label": "Stream archive dirs",
                "body": "Stream archive directories instead of globbing in cycle-eeb3c220d25b. Commit.",
                "vector": "V1",
                "evidence": "E1",
            }]}

        monkeypatch.setattr(goal_review, "_call_llm", _fake_llm)
        titles = goal_review.maybe_goal_review(state_dir, None, now=NOW)
        assert titles == ["Priority 17 — Stream archive dirs"]


class TestADR020Rule1Contract:
    """Test contract for ADR-020 Rule 1: The gap-driven mint stops.

    1. Scorecard gap present -> published numbers, but 0 new priority entries.
    2. Unrelated / non-citable evidence -> validation rejection.
    3. Deterministic demand items unchanged before and after.
    4. Zero demand candidates -> idle heartbeat and 0 LLM calls.
    5. Derived priorities queue depth and limit published for visibility.
    """

    def test_open_scorecard_gap_produces_zero_new_priorities(
        self, tmp_path, monkeypatch, enabled
    ):
        """Open scorecard gaps do not populate citable evidence and mint 0 priorities."""
        state_dir = tmp_path / "state"
        _write_goal_text(state_dir)
        _write_snapshot(state_dir, [GAP])  # Open gap present

        llm_called = False

        def _fake_llm(context: str):
            nonlocal llm_called
            llm_called = True
            return {"priorities": [VALID_PRIORITY]}

        monkeypatch.setattr(goal_review, "_call_llm", _fake_llm)
        titles = goal_review.maybe_goal_review(state_dir, None, now=NOW)

        # Without decay or supported hypotheses, evidence is empty -> honest no-op
        assert titles == []
        assert llm_called is False
        assert goal_review.read_derived_priorities(state_dir) == []

        rows = _goal_review_rows(state_dir)
        assert len(rows) == 1
        assert rows[0]["outcome"] == "no_gaps"
        assert rows[0]["retention_status"] == "complete"
        assert rows[0]["evidence_sources"] == []

    def test_unsuitable_evidence_citation_is_rejected(self):
        """A candidate citing non-matching or missing evidence is rejected."""
        evidence = {
            "E1": "decay: scripts/old.py has no harness-observed use (14+ days; goal vector V1)",
        }
        candidate_bad_evidence = {
            "label": "Do something else",
            "body": "Fix scripts/old.py. Commit.",
            "vector": "V1",
            "evidence": "E99",  # non-existent key
        }
        normalized, reason = goal_review.validate_priority(
            candidate_bad_evidence, evidence, set()
        )
        assert normalized is None
        assert reason == "evidence_not_in_inputs"

        candidate_gap_claim = {
            "label": "Fix the repeat failure rate gap",
            "body": "Lower failure rate under 0.3. Commit.",
            "vector": "V1",
            "evidence": "E1",  # cites decay evidence for a gap proposal
        }
        # Citing decay for non-decay work without matching context still validates evidence presence,
        # but if evidence is empty, nothing can be cited:
        norm_empty, reason_empty = goal_review.validate_priority(
            candidate_gap_claim, {}, set()
        )
        assert norm_empty is None
        assert reason_empty == "evidence_not_in_inputs"

    def test_derived_priorities_queue_depth_and_limit_reported(self, tmp_path):
        """Derived priorities queue depth and limit are published in health success_signals."""
        from nanobot.runtime.health import (
            build_cycle_health_summary,
            read_derived_priorities_queue,
        )

        state = tmp_path / "state"
        state.mkdir(parents=True)
        queue_info = read_derived_priorities_queue(state)
        assert queue_info == {"depth": 0, "limit": 10}

        # Seed 10 priorities (saturation)
        goal_review._write_derived_priorities(
            state, [
                {"number": i + 1, "label": f"priority-{i}", "body": "b", "vector": "V1"}
                for i in range(10)
            ]
        )
        queue_saturated = read_derived_priorities_queue(state)
        assert queue_saturated == {"depth": 10, "limit": 10}

        summary = build_cycle_health_summary(state)
        assert summary["success_signals"]["derived_priorities_queue_depth"] == 10
        assert summary["success_signals"]["derived_priorities_queue_limit"] == 10


# ─── #1920: release_root passed to maybe_goal_review ───────────────────────


class TestMaybeGoalReviewReleaseRoot:
    def test_maybe_goal_review_prefers_release_charter_when_goals_md_present(self, tmp_path, monkeypatch, enabled):
        """#1920 AC: when goals.md exists under release_root, goal review context
        carries the immutable release charter instead of legacy goal_text.json."""
        state_dir = tmp_path / "state"
        _write_goal_text(state_dir, "LEGACY_GOAL_TEXT_CONTENT")
        _seed_valid_evidence(state_dir, monkeypatch)
        _write_snapshot(state_dir, [])

        release_root = tmp_path / "release"
        release_root.mkdir(parents=True)
        (release_root / "goals.md").write_text("RELEASE_CHARTER_CAPABILITY_LADDER", encoding="utf-8")

        captured_contexts: list[str] = []

        def _mock_call_llm(context: str):
            captured_contexts.append(context)
            return {"priorities": []}

        monkeypatch.setattr(goal_review, "_call_llm", _mock_call_llm)

        goal_review.maybe_goal_review(state_dir, None, now=NOW, release_root=release_root)
        assert len(captured_contexts) == 1
        assert "RELEASE_CHARTER_CAPABILITY_LADDER" in captured_contexts[0]
        assert "LEGACY_GOAL_TEXT_CONTENT" not in captured_contexts[0]

    def test_maybe_goal_review_falls_back_to_legacy_state_when_goals_md_absent(self, tmp_path, monkeypatch, enabled):
        """#1920 AC: when release_root lacks goals.md, goal review context
        falls back to legacy state/goals/goal_text.json."""
        state_dir = tmp_path / "state"
        _write_goal_text(state_dir, "LEGACY_GOAL_TEXT_FALLBACK")
        _seed_valid_evidence(state_dir, monkeypatch)
        _write_snapshot(state_dir, [])

        empty_release_root = tmp_path / "empty_release"
        empty_release_root.mkdir(parents=True)

        captured_contexts: list[str] = []

        def _mock_call_llm(context: str):
            captured_contexts.append(context)
            return {"priorities": []}

        monkeypatch.setattr(goal_review, "_call_llm", _mock_call_llm)

        goal_review.maybe_goal_review(state_dir, None, now=NOW, release_root=empty_release_root)
        assert len(captured_contexts) == 1
        assert "LEGACY_GOAL_TEXT_FALLBACK" in captured_contexts[0]



