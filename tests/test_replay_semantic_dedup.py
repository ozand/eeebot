"""#1451: the semantic-dedup replay, and the coverage gate in front of it.

The replay's value is that its number is reproducible. These tests pin the
three things that could make it silently wrong: the candidate reconstruction
(it must reproduce #1328's key), the bridge's real-failure criterion (ported
here, so it must still agree with the runtime), and the coverage gate (a
partially adjudicable candidate set must be reported as such, never scored as
if it were the set the issue names).
"""
from __future__ import annotations

import gzip
import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "replay_semantic_dedup", REPO_ROOT / "scripts" / "replay_semantic_dedup.py"
)
assert _spec and _spec.loader
replay = importlib.util.module_from_spec(_spec)
# ``@dataclass`` resolves annotations through ``sys.modules[cls.__module__]``,
# so a spec-loaded module must be registered before it is executed.
sys.modules[_spec.name] = replay
_spec.loader.exec_module(replay)

BASE = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)


def _rows(*specs: dict) -> list[dict]:
    """Ledger rows for a list of ``{offset_h, cycle, demand, path, title, outcome}``."""
    rows: list[dict] = []
    for spec in specs:
        ts = (BASE + timedelta(hours=spec.get("offset_h", 0))).isoformat().replace("+00:00", "Z")
        rows.append(
            {
                "phase": "proposed",
                "cycle_id": spec["cycle"],
                "demand_id": spec["demand"],
                "target_path": spec["path"],
                "task_title": spec.get("title", "t"),
                "expected_outcome_claim": spec.get("claim", ""),
                "ts": ts,
            }
        )
        if spec.get("outcome"):
            rows.append(
                {
                    "phase": "outcome",
                    "cycle_id": spec["cycle"],
                    "outcome": spec["outcome"],
                    "reason": spec.get("reason"),
                    "ts": ts,
                }
            )
    return rows


# ─── the candidate reconstruction ──────────────────────────────────────────

class TestCandidateReconstruction:
    def test_prior_failure_on_the_same_demand_and_path_cools_the_later_proposal(self):
        cycles = replay.build_cycles(
            _rows(
                {"cycle": "c1", "demand": "d1", "path": "scripts/a.py", "outcome": "failed", "offset_h": 0},
                {"cycle": "c2", "demand": "d1", "path": "scripts/a.py", "outcome": "success", "offset_h": 2},
            )
        )
        candidates = replay.name_key_candidates(cycles)
        assert [c.blocked.cycle_id for c in candidates] == ["c2"]
        assert candidates[0].prior.cycle_id == "c1"
        assert candidates[0].later_success is True

    def test_a_different_path_under_the_same_demand_is_not_cooled(self):
        cycles = replay.build_cycles(
            _rows(
                {"cycle": "c1", "demand": "d1", "path": "scripts/a.py", "outcome": "failed", "offset_h": 0},
                {"cycle": "c2", "demand": "d1", "path": "scripts/b.py", "outcome": "success", "offset_h": 2},
            )
        )
        assert replay.name_key_candidates(cycles) == []

    def test_a_prior_outside_the_window_does_not_cool(self):
        cycles = replay.build_cycles(
            _rows(
                {"cycle": "c1", "demand": "d1", "path": "scripts/a.py", "outcome": "failed", "offset_h": 0},
                {"cycle": "c2", "demand": "d1", "path": "scripts/a.py", "outcome": "success", "offset_h": 30},
            )
        )
        assert replay.name_key_candidates(cycles) == []
        assert len(replay.name_key_candidates(cycles, window_hours=48)) == 1

    def test_a_prior_success_does_not_cool(self):
        cycles = replay.build_cycles(
            _rows(
                {"cycle": "c1", "demand": "d1", "path": "scripts/a.py", "outcome": "success", "offset_h": 0},
                {"cycle": "c2", "demand": "d1", "path": "scripts/a.py", "outcome": "success", "offset_h": 2},
            )
        )
        assert replay.name_key_candidates(cycles) == []

    def test_skipped_outcomes_are_the_dedup_stack_working_not_failures(self):
        cycles = replay.build_cycles(
            _rows(
                {"cycle": "c1", "demand": "d1", "path": "scripts/a.py", "outcome": "skipped-duplicate", "offset_h": 0},
                {"cycle": "c2", "demand": "d1", "path": "scripts/a.py", "outcome": "success", "offset_h": 2},
            )
        )
        assert replay.name_key_candidates(cycles) == []

    def test_gzipped_archives_and_the_live_file_are_both_read(self, tmp_path):
        ledger = tmp_path / "ledger"
        ledger.mkdir()
        archived = _rows({"cycle": "c1", "demand": "d1", "path": "scripts/a.py", "outcome": "failed"})
        with gzip.open(ledger / "cycles-2026-08-01.jsonl.gz", "wt", encoding="utf-8") as handle:
            for row in archived:
                handle.write(json.dumps(row) + "\n")
        live = _rows({"cycle": "c2", "demand": "d1", "path": "scripts/a.py", "outcome": "success", "offset_h": 2})
        (ledger / "cycles.jsonl").write_text(
            "\n".join(json.dumps(row) for row in live) + "\n", encoding="utf-8"
        )
        rows = replay.load_ledger_rows(ledger)
        assert len(rows) == len(archived) + len(live)
        assert len(replay.name_key_candidates(replay.build_cycles(rows))) == 1

    def test_a_torn_last_line_is_skipped_not_fatal(self, tmp_path):
        ledger = tmp_path / "ledger"
        ledger.mkdir()
        good = _rows({"cycle": "c1", "demand": "d1", "path": "scripts/a.py", "outcome": "failed"})
        text = "\n".join(json.dumps(row) for row in good) + '\n{"phase": "outc'
        (ledger / "cycles.jsonl").write_text(text, encoding="utf-8")
        assert len(replay.load_ledger_rows(ledger)) == len(good)

    def test_until_reproduces_a_historical_window(self):
        rows = _rows(
            {"cycle": "c1", "demand": "d1", "path": "scripts/a.py", "outcome": "failed", "offset_h": 0},
            {"cycle": "c2", "demand": "d1", "path": "scripts/a.py", "outcome": "success", "offset_h": 2},
        )
        cutoff = BASE + timedelta(hours=1)
        assert len(replay.build_cycles(rows, until=cutoff)) == 1
        assert replay.name_key_candidates(replay.build_cycles(rows, until=cutoff)) == []


# ─── the bridge's criterion, ported ────────────────────────────────────────

class TestRealFailureCriterion:
    @pytest.mark.parametrize(
        "record,expected",
        [
            ({"result_status": "completed", "materialized_from": "bridge_llm_execution"}, True),
            ({"result_status": "blocked"}, False),
            ({"status": "blocked"}, False),
            ({"result_status": "completed", "terminal_reason": "local_executor_unavailable"}, False),
            ({"result_status": "completed", "materialized_from": "queued_request_terminalizer"}, False),
            ({"result_status": "completed", "blocker": {"reason": "local_executor_unavailable"}}, False),
            ({"result_status": "completed", "blocker_reason": "local_executor_unavailable"}, False),
            ({}, True),
        ],
    )
    def test_matches_the_documented_cases(self, record, expected):
        assert replay.is_real_result(record) is expected

    def test_agrees_with_the_runtime_implementation(self):
        """Ported, not imported, so this must be asserted rather than assumed."""
        from nanobot.runtime.bridge import _is_real_result

        cases = [
            {"result_status": "completed", "materialized_from": "bridge_llm_execution"},
            {"result_status": "blocked"},
            {"status": "blocked"},
            {"result_status": "completed", "terminal_reason": "local_executor_unavailable"},
            {"result_status": "completed", "materialized_from": "queued_request_terminalizer"},
            {"result_status": "completed", "blocker": {"reason": "local_executor_unavailable"}},
            {},
        ]
        for case in cases:
            assert replay.is_real_result(case) == _is_real_result(case), case


# ─── the coverage gate ─────────────────────────────────────────────────────

class TestCoverageGate:
    def _candidates(self):
        cycles = replay.build_cycles(
            _rows(
                {"cycle": "p1", "demand": "d1", "path": "scripts/a.py", "outcome": "failed", "offset_h": 0},
                {"cycle": "b1", "demand": "d1", "path": "scripts/a.py", "outcome": "success", "offset_h": 1},
                {"cycle": "p2", "demand": "d2", "path": "scripts/b.py", "outcome": "failed", "offset_h": 2},
                {"cycle": "b2", "demand": "d2", "path": "scripts/b.py", "outcome": "success", "offset_h": 3},
            )
        )
        return replay.name_key_candidates(cycles)

    def test_full_coverage_reports_complete_and_an_exact_set(self):
        index = {
            "p1": {"result_status": "completed"},
            "p2": {"result_status": "blocked"},
        }
        audit = replay.audit_coverage(self._candidates(), index)
        assert audit.total == 2 and audit.missing == 0 and audit.blocked_priors == 1
        assert audit.complete is True
        assert audit.lower_bound() == audit.upper_bound() == 1

    def test_a_missing_prior_artifact_makes_the_set_a_range_not_a_number(self):
        """The decisive point of #1451's blocker: an unaudited prior might have
        been a blocked stub, so the bridge-criterion set size is bounded, not
        known, and no single number may be reported as the comparison set."""
        audit = replay.audit_coverage(self._candidates(), {"p1": {"result_status": "completed"}})
        assert audit.complete is False
        assert audit.missing == 1
        assert audit.lower_bound() == 1
        assert audit.upper_bound() == 2
        assert audit.missing_ts_range is not None

    def test_the_audit_names_the_dates_of_what_is_missing(self):
        audit = replay.audit_coverage(self._candidates(), {})
        assert audit.missing == 2
        assert audit.missing_cycle_ids == ["p1", "p2"]
        low, high = audit.missing_ts_range
        assert low.startswith("2026-08-01T12:00")
        assert high.startswith("2026-08-01T14:00")

    def test_the_audit_subcommand_exits_nonzero_when_incomplete(self, tmp_path, capsys):
        ledger = tmp_path / "ledger"
        ledger.mkdir()
        rows = _rows(
            {"cycle": "p1", "demand": "d1", "path": "scripts/a.py", "outcome": "failed", "offset_h": 0},
            {"cycle": "b1", "demand": "d1", "path": "scripts/a.py", "outcome": "success", "offset_h": 1},
        )
        (ledger / "cycles.jsonl").write_text(
            "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8"
        )
        assert replay.main(["audit", "--ledger-dir", str(ledger)]) == 2
        report = json.loads(capsys.readouterr().out)
        assert report["complete"] is False
        assert report["missing_result_artifact"] == 1


# ─── the threshold rule, fixed before scoring ──────────────────────────────

class TestThresholdRule:
    def test_the_rule_is_a_percentile_of_the_corpus_null_not_an_imported_constant(self):
        """The prose may name 0.95 to explain why it is refused; the executable
        code may not contain it as a threshold."""
        source = (REPO_ROOT / "scripts" / "replay_semantic_dedup.py").read_text(encoding="utf-8")
        code = "\n".join(
            line.split("#", 1)[0]
            for line in source.splitlines()
            if not line.lstrip().startswith("#")
        )
        assert "0.95" not in code, "the ShinkaEvolve constant must not be imported (#1451)"
        assert replay.NULL_PERCENTILE == 99.0
        assert isinstance(replay.null_threshold, type(replay.cosine))

    def test_the_null_uses_only_unrelated_pairs(self):
        # Two tight clusters that share neither demand nor path across clusters.
        keys = [("d1", "p1"), ("d1", "p1"), ("d2", "p2"), ("d2", "p2")]
        vectors = [[1.0, 0.0], [0.99, 0.01], [0.0, 1.0], [0.01, 0.99]]
        threshold, pairs = replay.null_threshold(keys, vectors, sample=200)
        assert pairs > 0
        # Cross-cluster pairs are near-orthogonal, so the null sits low and the
        # within-cluster similarity (~1.0) clears it.
        assert threshold < 0.5
        assert replay.cosine(vectors[0], vectors[1]) > threshold

    def test_the_null_refuses_when_every_pair_is_related(self):
        keys = [("d1", "p1"), ("d1", "p1")]
        with pytest.raises(ValueError):
            replay.null_threshold(keys, [[1.0, 0.0], [0.0, 1.0]], sample=10)

    def test_percentile_interpolates(self):
        assert replay.percentile([0.0, 1.0], 50.0) == pytest.approx(0.5)
        assert replay.percentile([0.0, 0.5, 1.0], 100.0) == 1.0

    def test_cosine_is_zero_for_degenerate_vectors(self):
        assert replay.cosine([], [1.0]) == 0.0
        assert replay.cosine([0.0, 0.0], [1.0, 1.0]) == 0.0
        assert replay.cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)


# ─── scoring and cost ──────────────────────────────────────────────────────

class TestScoringAndCost:
    def test_both_directions_are_reported(self):
        cycles = replay.build_cycles(
            _rows(
                {"cycle": "p1", "demand": "d1", "path": "scripts/a.py", "outcome": "failed", "offset_h": 0},
                {"cycle": "b1", "demand": "d1", "path": "scripts/a.py", "outcome": "success", "offset_h": 1},
                {"cycle": "p2", "demand": "d2", "path": "scripts/b.py", "outcome": "failed", "offset_h": 2},
                {"cycle": "b2", "demand": "d2", "path": "scripts/b.py", "outcome": "failed", "offset_h": 3},
            )
        )
        candidates = replay.name_key_candidates(cycles)
        vectors = {
            "p1": [1.0, 0.0], "b1": [0.0, 1.0],   # different work: semantic allows
            "p2": [1.0, 0.0], "b2": [1.0, 0.0],   # same work: semantic blocks
        }
        result = replay.score(candidates, vectors, threshold=0.9)
        assert result["name_key_blocks"] == 2
        assert result["name_key_kills_successes"] == 1
        assert result["semantic_blocks"] == 1
        assert result["semantic_kills_successes"] == 0
        assert result["successes_rescued"] == 1
        assert result["duplicates_released"] == 0

    def test_embedding_cost_counts_texts_and_requests_separately(self):
        calls: list[int] = []

        def fake_client(chunk, model):
            calls.append(len(chunk))
            return [[1.0, 0.0] for _ in chunk], 7 * len(chunk)

        vectors, cost = replay.embed_texts(
            [f"text {n}" for n in range(70)], model="fake", batch=32, client=fake_client
        )
        assert len(vectors) == 70
        assert cost.texts == 70
        assert cost.requests == 3
        assert cost.prompt_tokens == 490
        assert calls == [32, 32, 6]

    def test_a_short_gateway_response_is_fatal_not_silently_padded(self):
        with pytest.raises(SystemExit):
            replay.embed_texts(["a", "b"], model="fake", client=lambda chunk, model: ([[1.0]], 1))

    def test_no_model_configured_is_refused(self, monkeypatch):
        monkeypatch.delenv("SELFEVO_EMBEDDING_MODEL", raising=False)
        with pytest.raises(SystemExit):
            replay.embed_texts(["a"], client=lambda chunk, model: ([[1.0]], 1))

    def test_cycle_text_uses_the_fields_the_ledger_records(self):
        cycle = replay.Cycle(
            cycle_id="c", ts=BASE, demand_id="d", target_path="p",
            task_title="Add scripts/x.py", expected_outcome_claim="x.py exits zero",
            outcome="success", reason="",
        )
        assert cycle.text() == "Add scripts/x.py\nx.py exits zero"
