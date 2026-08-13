"""Tests for #813: benchmark-evidence gate.

Covers the schema validator (:func:`validate_benchmark`), the explicit
structured optimization-claim signal (:func:`is_optimization_claim`), and
the fail-closed existence+validity check (:func:`has_valid_benchmark`).
"""
from __future__ import annotations

import json
from pathlib import Path

from nanobot.runtime import benchmark_evidence


_GOOD_BENCHMARK = {
    "metric": "p95_latency_ms",
    "baseline": 420,
    "new_value": 180,
    "method": "wrk -t2 -c50 -d30s against /health, median of 3 runs",
}


def _write_benchmark(state_dir: Path, cycle_id: str, payload: dict) -> None:
    path = benchmark_evidence.benchmark_path(state_dir, cycle_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


class TestValidateBenchmark:
    def test_good_artifact_has_no_violations(self):
        assert benchmark_evidence.validate_benchmark(dict(_GOOD_BENCHMARK)) == []

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


class TestHasValidBenchmark:
    def test_true_for_schema_valid_file(self, tmp_path):
        _write_benchmark(tmp_path, "cycle-abc123", _GOOD_BENCHMARK)
        assert benchmark_evidence.has_valid_benchmark(tmp_path, "cycle-abc123") is True

    def test_false_when_file_missing(self, tmp_path):
        assert benchmark_evidence.has_valid_benchmark(tmp_path, "cycle-nope") is False

    def test_false_when_file_schema_invalid(self, tmp_path):
        _write_benchmark(tmp_path, "cycle-bad", {"metric": "x"})  # missing fields
        assert benchmark_evidence.has_valid_benchmark(tmp_path, "cycle-bad") is False

    def test_false_when_file_corrupt_json(self, tmp_path):
        path = benchmark_evidence.benchmark_path(tmp_path, "cycle-corrupt")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        assert benchmark_evidence.has_valid_benchmark(tmp_path, "cycle-corrupt") is False

    def test_false_for_empty_cycle_id(self, tmp_path):
        assert benchmark_evidence.has_valid_benchmark(tmp_path, "") is False
        assert benchmark_evidence.has_valid_benchmark(tmp_path, None) is False

    def test_benchmark_path_shape(self, tmp_path):
        path = benchmark_evidence.benchmark_path(tmp_path, "cycle-xyz")
        assert path == tmp_path / "benchmarks" / "cycle-xyz.json"
