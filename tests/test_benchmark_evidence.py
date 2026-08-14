"""Tests for #813/#819: benchmark-evidence gate.

Covers the schema+measurement validator (:func:`validate_benchmark`), the
explicit structured optimization-claim signal (:func:`is_optimization_claim`),
the operator trust switch (:func:`benchmark_trust_enabled`,
``SELFEVO_BENCHMARK_TRUST``), the fail-closed existence/validity check
(:func:`has_valid_benchmark`, :func:`benchmark_file_exists`), the cycle_id
path-traversal rejection, and (#819) the forge-proof harness-history
corroboration (:func:`verify_benchmark`) that :func:`has_valid_benchmark`
now delegates to — a benchmark artifact is only ever a CLAIM; the harness's
own ``scorecard/history.jsonl`` decides whether the named, allowlisted
metric actually moved.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

from nanobot.runtime import benchmark_evidence


_GOOD_BENCHMARK = {
    "metric": "p95_latency_ms",
    "baseline": 420,
    "new_value": 180,
    "method": "wrk -t2 -c50 -d30s against /health, median of 3 runs",
    "direction": "lower_is_better",
}

# #819: an artifact naming a metric that IS in the harness-verifiable
# allowlist (benchmark_evidence._HARNESS_METRICS) — used by every test that
# needs to get past the metric/direction gate into history corroboration.
_VERIFIABLE_BENCHMARK = {
    "metric": "tokens_per_integration",
    "baseline": 1000,
    "new_value": 400,
    "method": "scorecard cost section, before/after the integration cycle",
    "direction": "lower_is_better",
}


def _write_benchmark(state_dir: Path, cycle_id: str, payload: dict) -> None:
    path = benchmark_evidence.benchmark_path(state_dir, cycle_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _now_iso(days_ago: float = 0) -> str:
    return _iso(datetime.now(timezone.utc) - timedelta(days=days_ago))


def _write_history(state_dir: Path, snapshots: list[dict]) -> None:
    """Write ``snapshots`` (each a full scorecard-history-line dict) to
    ``<state_dir>/scorecard/history.jsonl`` — the harness trust root
    :func:`verify_benchmark` reads."""
    path = Path(state_dir) / "scorecard" / "history.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for snap in snapshots:
            fh.write(json.dumps(snap) + "\n")


def _corroborating_history(
    state_dir: Path,
    *,
    section: str = "cost",
    metric: str = "tokens_per_integration",
    before_value: float = 1000,
    after_value: float = 400,
    before_days_ago: float = 3,
    after_days_ago: float = 0,
) -> None:
    """The harness's own history showing ``metric`` moved from
    ``before_value`` to ``after_value`` — the corroborating shape
    :func:`verify_benchmark` needs regardless of what the artifact itself
    claims."""
    _write_history(
        state_dir,
        [
            {"computed_at_utc": _now_iso(before_days_ago), section: {metric: before_value}},
            {"computed_at_utc": _now_iso(after_days_ago), section: {metric: after_value}},
        ],
    )


class TestValidateBenchmark:
    def test_good_artifact_has_no_violations(self):
        assert benchmark_evidence.validate_benchmark(dict(_GOOD_BENCHMARK)) == []

    def test_good_artifact_higher_is_better_has_no_violations(self):
        obj = dict(_GOOD_BENCHMARK, baseline=100, new_value=250, direction="higher_is_better")
        assert benchmark_evidence.validate_benchmark(obj) == []

    def test_not_a_dict_is_a_violation(self):
        assert benchmark_evidence.validate_benchmark("not a dict") != []
        assert benchmark_evidence.validate_benchmark(None) != []
        assert benchmark_evidence.validate_benchmark([1, 2, 3]) != []

    def test_missing_metric_is_rejected(self):
        obj = dict(_GOOD_BENCHMARK)
        del obj["metric"]
        violations = benchmark_evidence.validate_benchmark(obj)
        assert any("metric" in v for v in violations)

    def test_empty_metric_is_rejected(self):
        obj = dict(_GOOD_BENCHMARK, metric="   ")
        violations = benchmark_evidence.validate_benchmark(obj)
        assert any("metric" in v for v in violations)

    def test_non_string_metric_is_rejected(self):
        obj = dict(_GOOD_BENCHMARK, metric=123)
        violations = benchmark_evidence.validate_benchmark(obj)
        assert any("metric" in v for v in violations)

    def test_missing_method_is_rejected(self):
        obj = dict(_GOOD_BENCHMARK)
        del obj["method"]
        violations = benchmark_evidence.validate_benchmark(obj)
        assert any("method" in v for v in violations)

    def test_empty_method_is_rejected(self):
        obj = dict(_GOOD_BENCHMARK, method="")
        violations = benchmark_evidence.validate_benchmark(obj)
        assert any("method" in v for v in violations)

    def test_missing_baseline_is_rejected(self):
        obj = dict(_GOOD_BENCHMARK)
        del obj["baseline"]
        violations = benchmark_evidence.validate_benchmark(obj)
        assert any("baseline" in v for v in violations)

    def test_non_numeric_baseline_is_rejected(self):
        obj = dict(_GOOD_BENCHMARK, baseline="420ms")
        violations = benchmark_evidence.validate_benchmark(obj)
        assert any("baseline" in v for v in violations)

    def test_bool_baseline_is_rejected(self):
        """bool is technically an int subclass in Python — must not slip
        through the numeric check."""
        obj = dict(_GOOD_BENCHMARK, baseline=True)
        violations = benchmark_evidence.validate_benchmark(obj)
        assert any("baseline" in v for v in violations)

    def test_missing_new_value_is_rejected(self):
        obj = dict(_GOOD_BENCHMARK)
        del obj["new_value"]
        violations = benchmark_evidence.validate_benchmark(obj)
        assert any("new_value" in v for v in violations)

    def test_non_numeric_new_value_is_rejected(self):
        obj = dict(_GOOD_BENCHMARK, new_value=None)
        violations = benchmark_evidence.validate_benchmark(obj)
        assert any("new_value" in v for v in violations)

    def test_float_values_are_accepted(self):
        obj = dict(_GOOD_BENCHMARK, baseline=420.5, new_value=180.25)
        assert benchmark_evidence.validate_benchmark(obj) == []

    # ─── MED-1: direction, nan/inf, no-change, regression ──────────────────

    def test_missing_direction_is_rejected(self):
        obj = dict(_GOOD_BENCHMARK)
        del obj["direction"]
        violations = benchmark_evidence.validate_benchmark(obj)
        assert any("direction" in v for v in violations)

    def test_invalid_direction_value_is_rejected(self):
        obj = dict(_GOOD_BENCHMARK, direction="sideways")
        violations = benchmark_evidence.validate_benchmark(obj)
        assert any("direction" in v for v in violations)

    def test_nan_baseline_is_rejected(self):
        obj = dict(_GOOD_BENCHMARK, baseline=float("nan"))
        violations = benchmark_evidence.validate_benchmark(obj)
        assert any("baseline" in v and "finite" in v for v in violations)

    def test_inf_new_value_is_rejected(self):
        obj = dict(_GOOD_BENCHMARK, new_value=float("inf"))
        violations = benchmark_evidence.validate_benchmark(obj)
        assert any("new_value" in v and "finite" in v for v in violations)

    def test_neg_inf_baseline_is_rejected(self):
        obj = dict(_GOOD_BENCHMARK, baseline=float("-inf"))
        violations = benchmark_evidence.validate_benchmark(obj)
        assert any("baseline" in v and "finite" in v for v in violations)

    def test_no_change_measurement_is_rejected(self):
        obj = dict(_GOOD_BENCHMARK, baseline=420, new_value=420)
        violations = benchmark_evidence.validate_benchmark(obj)
        assert any("differ" in v or "no-change" in v for v in violations)

    def test_regression_lower_is_better_is_rejected(self):
        """direction says lower is better, but new_value went UP."""
        obj = dict(_GOOD_BENCHMARK, baseline=180, new_value=420, direction="lower_is_better")
        violations = benchmark_evidence.validate_benchmark(obj)
        assert any("regression" in v for v in violations)

    def test_regression_higher_is_better_is_rejected(self):
        """direction says higher is better, but new_value went DOWN."""
        obj = dict(_GOOD_BENCHMARK, baseline=250, new_value=100, direction="higher_is_better")
        violations = benchmark_evidence.validate_benchmark(obj)
        assert any("regression" in v for v in violations)

    def test_nan_short_circuits_direction_check(self):
        """A non-finite value must not also spuriously fire the
        equality/regression checks (which would produce confusing double
        violations); it should raise exactly the finiteness violation for
        that field, no direction/regression noise for it."""
        obj = dict(_GOOD_BENCHMARK, baseline=float("nan"), new_value=180)
        violations = benchmark_evidence.validate_benchmark(obj)
        assert any("baseline" in v and "finite" in v for v in violations)
        assert not any("regression" in v for v in violations)


class TestIsOptimizationClaim:
    def test_bare_optimization_is_true(self):
        assert benchmark_evidence.is_optimization_claim("optimization") is True

    def test_optimization_with_metric_is_true(self):
        assert benchmark_evidence.is_optimization_claim("optimization latency") is True

    def test_case_insensitive(self):
        assert benchmark_evidence.is_optimization_claim("OPTIMIZATION latency") is True
        assert benchmark_evidence.is_optimization_claim("Optimization: p95") is True

    def test_leading_whitespace_is_stripped(self):
        assert benchmark_evidence.is_optimization_claim("  optimization latency") is True

    def test_priority_serves_is_false(self):
        assert benchmark_evidence.is_optimization_claim("priority 5") is False

    def test_demand_serves_is_false(self):
        assert benchmark_evidence.is_optimization_claim("demand defect-1a2b3c4d5e6f") is False

    def test_hypothesis_serves_is_false(self):
        assert benchmark_evidence.is_optimization_claim("hypothesis h3") is False

    def test_empty_and_none_are_false(self):
        assert benchmark_evidence.is_optimization_claim("") is False
        assert benchmark_evidence.is_optimization_claim(None) is False

    def test_word_containing_optimization_but_not_prefixed_is_false(self):
        assert benchmark_evidence.is_optimization_claim("vector 1: optimization-adjacent") is False


class TestBenchmarkTrustSwitch:
    def test_default_is_off(self, monkeypatch):
        monkeypatch.delenv("SELFEVO_BENCHMARK_TRUST", raising=False)
        assert benchmark_evidence.benchmark_trust_enabled() is False

    def test_explicit_zero_is_off(self, monkeypatch):
        monkeypatch.setenv("SELFEVO_BENCHMARK_TRUST", "0")
        assert benchmark_evidence.benchmark_trust_enabled() is False

    def test_truthy_values_turn_it_on(self, monkeypatch):
        for value in ("1", "true", "TRUE", "yes", "on"):
            monkeypatch.setenv("SELFEVO_BENCHMARK_TRUST", value)
            assert benchmark_evidence.benchmark_trust_enabled() is True

    def test_garbage_value_is_off(self, monkeypatch):
        monkeypatch.setenv("SELFEVO_BENCHMARK_TRUST", "sure why not")
        assert benchmark_evidence.benchmark_trust_enabled() is False


class TestHasValidBenchmark:
    def test_false_by_default_even_for_schema_valid_file(self, tmp_path, monkeypatch):
        """#813 HIGH-2: fail-closed default — a perfectly valid, even
        harness-history-corroborated artifact is still not trusted until the
        operator opts in."""
        monkeypatch.delenv("SELFEVO_BENCHMARK_TRUST", raising=False)
        _write_benchmark(tmp_path, "cycle-abc123", _VERIFIABLE_BENCHMARK)
        integration_ts = _now_iso(days_ago=2)
        _corroborating_history(tmp_path)
        assert benchmark_evidence.has_valid_benchmark(tmp_path, "cycle-abc123", integration_ts) is False

    def test_true_for_schema_valid_file_when_trusted_and_corroborated(self, tmp_path, monkeypatch):
        """#819: trust ON + a schema-valid artifact naming an allowlisted
        metric is STILL not enough — the harness's own scorecard history
        must corroborate the improvement around the integration ts."""
        monkeypatch.setenv("SELFEVO_BENCHMARK_TRUST", "1")
        _write_benchmark(tmp_path, "cycle-abc123", _VERIFIABLE_BENCHMARK)
        integration_ts = _now_iso(days_ago=2)
        _corroborating_history(tmp_path)  # before=1000 (3d ago) -> after=400 (now)
        assert benchmark_evidence.has_valid_benchmark(tmp_path, "cycle-abc123", integration_ts) is True

    def test_false_when_file_missing_even_if_trusted(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SELFEVO_BENCHMARK_TRUST", "1")
        assert benchmark_evidence.has_valid_benchmark(tmp_path, "cycle-nope") is False

    def test_false_when_file_schema_invalid_even_if_trusted(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SELFEVO_BENCHMARK_TRUST", "1")
        _write_benchmark(tmp_path, "cycle-bad", {"metric": "x"})  # missing fields
        assert benchmark_evidence.has_valid_benchmark(tmp_path, "cycle-bad") is False

    def test_false_when_file_corrupt_json(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SELFEVO_BENCHMARK_TRUST", "1")
        path = benchmark_evidence.benchmark_path(tmp_path, "cycle-corrupt")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        assert benchmark_evidence.has_valid_benchmark(tmp_path, "cycle-corrupt") is False

    def test_false_for_empty_cycle_id(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SELFEVO_BENCHMARK_TRUST", "1")
        assert benchmark_evidence.has_valid_benchmark(tmp_path, "") is False
        assert benchmark_evidence.has_valid_benchmark(tmp_path, None) is False

    def test_benchmark_path_shape(self, tmp_path):
        path = benchmark_evidence.benchmark_path(tmp_path, "cycle-xyz")
        assert path == tmp_path / "benchmarks" / "cycle-xyz.json"

    # ─── MED-2: cycle_id path traversal ──────────────────────────────────

    def test_path_traversal_cycle_id_never_valid(self, tmp_path, monkeypatch):
        """A crafted cycle_id must never let has_valid_benchmark escape
        <state_dir>/benchmarks/ to read an arbitrary on-disk JSON file."""
        monkeypatch.setenv("SELFEVO_BENCHMARK_TRUST", "1")
        # Plant a valid-looking benchmark OUTSIDE the benchmarks/ dir that a
        # traversal payload might target.
        outside = tmp_path / "secret.json"
        outside.write_text(json.dumps(_GOOD_BENCHMARK), encoding="utf-8")
        for payload in ("../secret", "..\\secret", "../../secret", "a/b", "a\\b", ".."):
            assert benchmark_evidence.has_valid_benchmark(tmp_path, payload) is False

    def test_path_traversal_cycle_id_benchmark_file_exists_is_false(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SELFEVO_BENCHMARK_TRUST", "1")
        outside = tmp_path / "secret.json"
        outside.write_text(json.dumps(_GOOD_BENCHMARK), encoding="utf-8")
        assert benchmark_evidence.benchmark_file_exists(tmp_path, "../secret") is False
        assert benchmark_evidence.benchmark_file_exists(tmp_path, "..") is False

    def test_ordinary_cycle_id_with_dots_but_no_traversal_is_fine(self, tmp_path, monkeypatch):
        """A cycle_id merely containing a single dot (e.g. as part of a
        uuid-like id) is not a traversal attempt and must still resolve —
        only an actual '..' sequence, or a path separator, is rejected."""
        monkeypatch.setenv("SELFEVO_BENCHMARK_TRUST", "1")
        _write_benchmark(tmp_path, "cycle.abc123", _VERIFIABLE_BENCHMARK)
        integration_ts = _now_iso(days_ago=2)
        _corroborating_history(tmp_path)
        assert benchmark_evidence.has_valid_benchmark(tmp_path, "cycle.abc123", integration_ts) is True


class TestBenchmarkFileExists:
    def test_true_when_present_regardless_of_validity_or_trust(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SELFEVO_BENCHMARK_TRUST", raising=False)
        _write_benchmark(tmp_path, "cycle-1", {"metric": "x"})  # schema-invalid
        assert benchmark_evidence.benchmark_file_exists(tmp_path, "cycle-1") is True

    def test_false_when_absent(self, tmp_path):
        assert benchmark_evidence.benchmark_file_exists(tmp_path, "cycle-none") is False

    def test_false_for_empty_or_none_cycle_id(self, tmp_path):
        assert benchmark_evidence.benchmark_file_exists(tmp_path, "") is False
        assert benchmark_evidence.benchmark_file_exists(tmp_path, None) is False


# ─── #819: harness-history-corroborated verification ────────────────────────


class TestVerifyBenchmark:
    """:func:`verify_benchmark` is the actual #819 trust root — the
    artifact is only ever a claim; these tests pin that the harness's own
    ``scorecard/history.jsonl`` (never the artifact's own numbers) decides
    the verdict."""

    def test_true_when_registered_metric_direction_correct_and_history_improved(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SELFEVO_BENCHMARK_TRUST", "1")
        _write_benchmark(tmp_path, "cyc-good", _VERIFIABLE_BENCHMARK)
        integration_ts = _now_iso(days_ago=2)
        _corroborating_history(tmp_path, before_value=1000, after_value=400)
        assert benchmark_evidence.verify_benchmark(tmp_path, "cyc-good", integration_ts) is True

    def test_higher_is_better_metric_also_verifies(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SELFEVO_BENCHMARK_TRUST", "1")
        obj = dict(
            _VERIFIABLE_BENCHMARK,
            metric="compile_clean_ratio",
            direction="higher_is_better",
            baseline=0.5,
            new_value=0.9,
        )
        _write_benchmark(tmp_path, "cyc-hib", obj)
        integration_ts = _now_iso(days_ago=2)
        _corroborating_history(
            tmp_path,
            section="quality",
            metric="compile_clean_ratio",
            before_value=0.5,
            after_value=0.95,
        )
        assert benchmark_evidence.verify_benchmark(tmp_path, "cyc-hib", integration_ts) is True

    def test_false_when_metric_not_in_allowlist(self, tmp_path, monkeypatch):
        """A metric name the harness does not track can NEVER verify, no
        matter how well-shaped the artifact or how favorable any history."""
        monkeypatch.setenv("SELFEVO_BENCHMARK_TRUST", "1")
        _write_benchmark(tmp_path, "cyc-bad-metric", dict(_GOOD_BENCHMARK))  # p95_latency_ms
        integration_ts = _now_iso(days_ago=2)
        _corroborating_history(tmp_path)
        assert benchmark_evidence.verify_benchmark(tmp_path, "cyc-bad-metric", integration_ts) is False

    def test_false_when_direction_mismatches_canonical(self, tmp_path, monkeypatch):
        """tokens_per_integration is canonically lower_is_better — an
        artifact claiming higher_is_better for it must be rejected outright,
        even if its own baseline/new_value are internally consistent with
        that (lying) direction."""
        monkeypatch.setenv("SELFEVO_BENCHMARK_TRUST", "1")
        obj = dict(
            _VERIFIABLE_BENCHMARK,
            direction="higher_is_better",
            baseline=400,
            new_value=1000,
        )
        _write_benchmark(tmp_path, "cyc-wrong-dir", obj)
        integration_ts = _now_iso(days_ago=2)
        _corroborating_history(tmp_path, before_value=1000, after_value=400)
        assert benchmark_evidence.verify_benchmark(tmp_path, "cyc-wrong-dir", integration_ts) is False

    def test_false_when_history_shows_no_improvement(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SELFEVO_BENCHMARK_TRUST", "1")
        _write_benchmark(tmp_path, "cyc-flat", _VERIFIABLE_BENCHMARK)
        integration_ts = _now_iso(days_ago=2)
        _corroborating_history(tmp_path, before_value=1000, after_value=1000)
        assert benchmark_evidence.verify_benchmark(tmp_path, "cyc-flat", integration_ts) is False

    def test_false_when_history_shows_a_regression(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SELFEVO_BENCHMARK_TRUST", "1")
        _write_benchmark(tmp_path, "cyc-regress", _VERIFIABLE_BENCHMARK)
        integration_ts = _now_iso(days_ago=2)
        _corroborating_history(tmp_path, before_value=400, after_value=1000)  # got WORSE
        assert benchmark_evidence.verify_benchmark(tmp_path, "cyc-regress", integration_ts) is False

    def test_false_when_no_history_at_all(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SELFEVO_BENCHMARK_TRUST", "1")
        _write_benchmark(tmp_path, "cyc-nohist", _VERIFIABLE_BENCHMARK)
        integration_ts = _now_iso(days_ago=2)
        assert benchmark_evidence.verify_benchmark(tmp_path, "cyc-nohist", integration_ts) is False

    def test_false_when_trust_off_even_with_corroborating_history(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SELFEVO_BENCHMARK_TRUST", raising=False)
        _write_benchmark(tmp_path, "cyc-untrusted", _VERIFIABLE_BENCHMARK)
        integration_ts = _now_iso(days_ago=2)
        _corroborating_history(tmp_path, before_value=1000, after_value=400)
        assert benchmark_evidence.verify_benchmark(tmp_path, "cyc-untrusted", integration_ts) is False

    def test_forged_artifact_numbers_do_not_matter_when_history_is_flat(self, tmp_path, monkeypatch):
        """The non-forgeability invariant: an internally-consistent,
        schema-valid artifact claiming a huge improvement is worthless if
        the harness's own history shows the real metric flat/regressed —
        the artifact's baseline/new_value are never part of the decision."""
        monkeypatch.setenv("SELFEVO_BENCHMARK_TRUST", "1")
        forged = dict(_VERIFIABLE_BENCHMARK, baseline=10_000, new_value=1)  # fabricated 10000x win
        _write_benchmark(tmp_path, "cyc-forged", forged)
        integration_ts = _now_iso(days_ago=2)
        _corroborating_history(tmp_path, before_value=1000, after_value=1000)  # actually flat
        assert benchmark_evidence.verify_benchmark(tmp_path, "cyc-forged", integration_ts) is False

    def test_false_when_no_snapshot_after_integration_yet(self, tmp_path, monkeypatch):
        """Both history entries are BEFORE the integration ts — nothing has
        been observed since the claimed change landed, so there is nothing
        to corroborate against yet."""
        monkeypatch.setenv("SELFEVO_BENCHMARK_TRUST", "1")
        _write_benchmark(tmp_path, "cyc-stale", _VERIFIABLE_BENCHMARK)
        integration_ts = _now_iso(days_ago=0)
        _corroborating_history(
            tmp_path, before_value=1000, after_value=400,
            before_days_ago=3, after_days_ago=2,
        )
        assert benchmark_evidence.verify_benchmark(tmp_path, "cyc-stale", integration_ts) is False

    def test_false_when_immediate_after_flat_even_if_a_later_snapshot_improved(self, tmp_path, monkeypatch):
        """#819 HIGH fix: verify_benchmark must corroborate against the
        CYCLE'S OWN immediate before/after snapshots — the latest snapshot
        with computed_at_utc <= ts, and the EARLIEST one strictly after ts —
        never the global-latest snapshot. An improvement that only shows up
        in a LATER, unrelated cycle must not retroactively corroborate this
        claim (pre-fix, using the global-latest snapshot as "after" would
        have made this pass)."""
        monkeypatch.setenv("SELFEVO_BENCHMARK_TRUST", "1")
        _write_benchmark(tmp_path, "cyc-window", _VERIFIABLE_BENCHMARK)
        integration_ts = _now_iso(days_ago=2)
        _write_history(
            tmp_path,
            [
                {"computed_at_utc": _now_iso(3), "cost": {"tokens_per_integration": 1000}},  # before
                {"computed_at_utc": _now_iso(1.5), "cost": {"tokens_per_integration": 1000}},  # immediate after: flat
                {"computed_at_utc": _now_iso(0), "cost": {"tokens_per_integration": 400}},  # later, unrelated improvement
            ],
        )
        assert benchmark_evidence.verify_benchmark(tmp_path, "cyc-window", integration_ts) is False

    def test_true_when_immediate_after_improved_even_with_a_later_regression(self, tmp_path, monkeypatch):
        """The mirror case: the cycle's own immediate before/after DOES show
        improvement — a later snapshot regressing again must not retroactively
        invalidate this cycle's own corroboration (the window is anchored to
        the cycle, not to whatever the metric does afterward)."""
        monkeypatch.setenv("SELFEVO_BENCHMARK_TRUST", "1")
        _write_benchmark(tmp_path, "cyc-window2", _VERIFIABLE_BENCHMARK)
        integration_ts = _now_iso(days_ago=2)
        _write_history(
            tmp_path,
            [
                {"computed_at_utc": _now_iso(3), "cost": {"tokens_per_integration": 1000}},  # before
                {"computed_at_utc": _now_iso(1.5), "cost": {"tokens_per_integration": 400}},  # immediate after: improved
                {"computed_at_utc": _now_iso(0), "cost": {"tokens_per_integration": 1000}},  # later regression
            ],
        )
        assert benchmark_evidence.verify_benchmark(tmp_path, "cyc-window2", integration_ts) is True

    def test_false_for_unparseable_integration_ts(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SELFEVO_BENCHMARK_TRUST", "1")
        _write_benchmark(tmp_path, "cyc-badts", _VERIFIABLE_BENCHMARK)
        _corroborating_history(tmp_path)
        assert benchmark_evidence.verify_benchmark(tmp_path, "cyc-badts", "not-a-timestamp") is False
        assert benchmark_evidence.verify_benchmark(tmp_path, "cyc-badts", None) is False

    def test_false_for_corrupt_history_lines(self, tmp_path, monkeypatch):
        """A corrupt history file must degrade to False, never raise."""
        monkeypatch.setenv("SELFEVO_BENCHMARK_TRUST", "1")
        _write_benchmark(tmp_path, "cyc-corrupthist", _VERIFIABLE_BENCHMARK)
        integration_ts = _now_iso(days_ago=2)
        hist = tmp_path / "scorecard" / "history.jsonl"
        hist.parent.mkdir(parents=True, exist_ok=True)
        hist.write_text("{not json\nalso not json\n", encoding="utf-8")
        assert benchmark_evidence.verify_benchmark(tmp_path, "cyc-corrupthist", integration_ts) is False
