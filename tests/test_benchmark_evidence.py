"""Tests for #813: benchmark-evidence gate.

Covers the schema+measurement validator (:func:`validate_benchmark`), the
explicit structured optimization-claim signal (:func:`is_optimization_claim`),
the operator trust switch (:func:`benchmark_trust_enabled`,
``SELFEVO_BENCHMARK_TRUST``), the fail-closed existence/validity check
(:func:`has_valid_benchmark`, :func:`benchmark_file_exists`), and the
cycle_id path-traversal rejection.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

from nanobot.runtime import benchmark_evidence


_GOOD_BENCHMARK = {
    "metric": "p95_latency_ms",
    "baseline": 420,
    "new_value": 180,
    "method": "wrk -t2 -c50 -d30s against /health, median of 3 runs",
    "direction": "lower_is_better",
}


def _write_benchmark(state_dir: Path, cycle_id: str, payload: dict) -> None:
    path = benchmark_evidence.benchmark_path(state_dir, cycle_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


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
        """#813 HIGH-2: fail-closed default — a perfectly valid artifact is
        still not trusted until the operator opts in."""
        monkeypatch.delenv("SELFEVO_BENCHMARK_TRUST", raising=False)
        _write_benchmark(tmp_path, "cycle-abc123", _GOOD_BENCHMARK)
        assert benchmark_evidence.has_valid_benchmark(tmp_path, "cycle-abc123") is False

    def test_true_for_schema_valid_file_when_trusted(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SELFEVO_BENCHMARK_TRUST", "1")
        _write_benchmark(tmp_path, "cycle-abc123", _GOOD_BENCHMARK)
        assert benchmark_evidence.has_valid_benchmark(tmp_path, "cycle-abc123") is True

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
        _write_benchmark(tmp_path, "cycle.abc123", _GOOD_BENCHMARK)
        assert benchmark_evidence.has_valid_benchmark(tmp_path, "cycle.abc123") is True


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
