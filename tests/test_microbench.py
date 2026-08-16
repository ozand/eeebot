"""Tests for #822: harness-run per-cycle causal micro-benchmark.

Covers the seed spec (:data:`microbench.MICROBENCHES`'s
``nanobot/runtime/existence_index.py`` entry) actually measuring something
real on the CURRENT repo, :func:`microbench.measure_cycle`'s happy path and
fail-open axes (unregistered file, unsafe cycle_id, candidate bytes
unavailable, a failed measurement), the 200-entry cap, and the
:func:`benchmark_evidence.verify_benchmark` integration: a well-formed
harness microbench entry is AUTHORITATIVE (never falls through to the
legacy 7-day-aggregate corroboration #819 already covers), a
malformed/absent one defers to it unchanged.

#822 REWORK (live-host finding): the original design measured baseline via
``git worktree add <tmp> base_sha`` against the INSTANCE repo — on the real
eeepc host that repo never contains ``nanobot/`` at all (runtime-tier
cycles never integrate; #812 promotion-candidate only), so baseline
measurement failed 100% of the time in production. The fix: baseline is now
the harness's own INSTALLED product tree (:data:`microbench._product_root`,
resolved from ``nanobot.__file__``), copied fresh per measurement; only the
CANDIDATE run overwrites one file with bytes read via ``git show
<head_ref>:<module_path>``. Tests below monkeypatch ``_product_root`` to a
synthetic tree for isolated fixture cases, and use the REAL current repo
(where ``_product_root`` already resolves correctly) for the smoke test.
"""
from __future__ import annotations

import json
import math
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from nanobot.runtime import benchmark_evidence
from nanobot.runtime.heldout import microbench

_REPO_ROOT = Path(__file__).resolve().parents[1]

# A trivial, deterministic spec used in place of the real existence_index
# spec for measure_cycle tests — reads a marker out of
# nanobot/fake_module.py (relative to the scratch tree's cwd) so
# baseline/candidate ms are known exactly (no timing noise), while still
# exercising the REAL run_measurement tree-copy + subprocess path.
_TRIVIAL_SPEC = (
    'from pathlib import Path\n'
    'content = Path("nanobot/fake_module.py").read_text(encoding="utf-8")\n'
    'print(10.0 if "FAST" in content else 100.0)\n'
)


def _fake_product_root(tmp_path: Path, *, marker: str = "SLOW") -> Path:
    """A synthetic installed product tree: ``<root>/nanobot/fake_module.py``
    — this is what a monkeypatched :data:`microbench._product_root` points
    at, standing in for the harness's real installed ``nanobot/`` package."""
    root = tmp_path / "product"
    (root / "nanobot").mkdir(parents=True, exist_ok=True)
    (root / "nanobot" / "fake_module.py").write_text(f"# {marker} marker\n", encoding="utf-8")
    return root


def _init_git_repo(repo_dir: Path) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.name", "Microbench Test"],
        cwd=str(repo_dir), check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "microbench-test@example.com"],
        cwd=str(repo_dir), check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"],
        cwd=str(repo_dir), check=True, capture_output=True,
    )


def _commit_module(repo_dir: Path, filename: str, content: str, message: str) -> str:
    path = repo_dir / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", filename], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", message], cwd=str(repo_dir), check=True, capture_output=True)
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo_dir),
        capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def _fixture_repo_with_candidate(tmp_path: Path, *, marker: str = "FAST") -> tuple[Path, str]:
    """A tiny one-commit git repo whose HEAD contains
    ``nanobot/fake_module.py`` with the CANDIDATE content — the ONLY thing
    measure_cycle reads from this repo now (via ``git show
    <head_ref>:<module_path>``); the baseline never comes from here."""
    repo_dir = tmp_path / "repo"
    _init_git_repo(repo_dir)
    head_sha = _commit_module(repo_dir, "nanobot/fake_module.py", f"# {marker} marker\n", "candidate commit")
    return repo_dir, head_sha


# ─── 1. seed-spec smoke: run_measurement against the CURRENT (installed) repo ──


def test_seed_spec_measures_existence_index_on_current_repo():
    # No monkeypatching: in this dev checkout, microbench._product_root
    # already resolves to this repo (via nanobot.__file__), so this measures
    # the REAL seed spec's baseline against the REAL installed tree.
    value = microbench.run_measurement("nanobot/runtime/existence_index.py", timeout=180)
    assert value is not None
    assert math.isfinite(value)
    assert value > 0


def test_git_show_bytes_reads_candidate_content_from_current_repo():
    data = microbench._git_show_bytes(_REPO_ROOT, "HEAD", "nanobot/runtime/existence_index.py")
    assert data is not None
    assert len(data) > 0


def test_git_show_bytes_returns_none_for_missing_path(tmp_path: Path):
    repo_dir, head_sha = _fixture_repo_with_candidate(tmp_path)
    assert microbench._git_show_bytes(repo_dir, head_sha, "nanobot/does_not_exist.py") is None


# ─── 2. measure_cycle happy path ────────────────────────────────────────────


def test_measure_cycle_happy_path(tmp_path: Path, monkeypatch):
    product_root = _fake_product_root(tmp_path, marker="SLOW")
    monkeypatch.setattr(microbench, "_product_root", product_root)
    monkeypatch.setattr(microbench, "MICROBENCHES", {"nanobot/fake_module.py": _TRIVIAL_SPEC})
    repo_dir, head_sha = _fixture_repo_with_candidate(tmp_path, marker="FAST")

    state_dir = tmp_path / "state"
    entry = microbench.measure_cycle(
        state_dir, repo_dir, "cyc-1", "basefakesha", head_sha, ["nanobot/fake_module.py"],
    )

    assert entry is not None
    assert entry["module"] == "nanobot/fake_module.py"
    assert entry["metric"] == "wall_ms_best_of_5"
    assert entry["baseline_ms"] == 100.0
    assert entry["baseline_ms_runs"] == [100.0, 100.0]  # MED-2 drift guard: both installed-tree runs
    assert entry["baseline_source"] == "installed"
    assert entry["candidate_ms"] == 10.0
    assert entry["improvement_pct"] == pytest.approx(90.0)
    assert entry["direction"] == "lower"
    assert entry["base_sha"] == "basefakesha"  # provenance only — not used to measure baseline
    assert entry["head_sha"] == head_sha
    assert entry["schema"] == "heldout-microbench-entry-v1"
    assert "measured_at_utc" in entry

    on_disk = json.loads((state_dir / "heldout" / "microbench.json").read_text(encoding="utf-8"))
    assert on_disk["schema_version"] == "heldout-microbench-v1"
    assert on_disk["entries"]["cyc-1"] == entry


def test_measure_cycle_caps_entries_at_200(tmp_path: Path, monkeypatch):
    product_root = _fake_product_root(tmp_path, marker="SLOW")
    monkeypatch.setattr(microbench, "_product_root", product_root)
    monkeypatch.setattr(microbench, "MICROBENCHES", {"nanobot/fake_module.py": _TRIVIAL_SPEC})
    repo_dir, head_sha = _fixture_repo_with_candidate(tmp_path, marker="FAST")

    state_dir = tmp_path / "state"
    path = state_dir / "heldout" / "microbench.json"
    path.parent.mkdir(parents=True, exist_ok=True)

    base_time = datetime(2020, 1, 1, tzinfo=timezone.utc)
    entries = {}
    for i in range(200):
        ts = (base_time + timedelta(minutes=i)).strftime("%Y-%m-%dT%H:%M:%SZ")
        entries[f"old-{i:04d}"] = {
            "module": "nanobot/fake_module.py",
            "metric": "wall_ms_best_of_5",
            "baseline_ms": 100.0,
            "candidate_ms": 90.0,
            "improvement_pct": 10.0,
            "direction": "lower",
            "base_sha": "x",
            "head_sha": "y",
            "measured_at_utc": ts,
            "schema": "heldout-microbench-entry-v1",
        }
    path.write_text(
        json.dumps({"schema_version": "heldout-microbench-v1", "entries": entries}),
        encoding="utf-8",
    )

    entry = microbench.measure_cycle(
        state_dir, repo_dir, "cyc-new", "basefakesha", head_sha, ["nanobot/fake_module.py"],
    )
    assert entry is not None

    data = json.loads(path.read_text(encoding="utf-8"))
    assert len(data["entries"]) == 200
    assert "cyc-new" in data["entries"]  # newest kept
    assert "old-0000" not in data["entries"]  # oldest evicted
    assert "old-0001" in data["entries"]  # second-oldest survives


# ─── 3. measure_cycle fail-open: writes nothing ─────────────────────────────


def test_measure_cycle_returns_none_when_no_registered_file(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(microbench, "MICROBENCHES", {"nanobot/fake_module.py": _TRIVIAL_SPEC})
    state_dir = tmp_path / "state"
    entry = microbench.measure_cycle(
        state_dir, tmp_path / "repo", "cyc-x", "deadbeef", "cafefeed", ["other_file.py"],
    )
    assert entry is None
    assert not (state_dir / "heldout" / "microbench.json").exists()


def test_measure_cycle_returns_none_when_candidate_bytes_unavailable(tmp_path: Path, monkeypatch):
    """#822 rework: candidate bytes come from `git show head_ref:module_path`
    against repo_root — a bad ref/path (or no git repo at all) must fail
    open to None, writing nothing, same as any other measurement failure."""
    monkeypatch.setattr(microbench, "MICROBENCHES", {"nanobot/fake_module.py": _TRIVIAL_SPEC})
    monkeypatch.setattr(microbench, "_git_show_bytes", lambda *a, **k: None)
    state_dir = tmp_path / "state"
    entry = microbench.measure_cycle(
        state_dir, tmp_path / "repo", "cyc-nogit", "deadbeef", "cafefeed", ["nanobot/fake_module.py"],
    )
    assert entry is None
    assert not (state_dir / "heldout" / "microbench.json").exists()


def test_measure_cycle_returns_none_when_measurement_fails(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(microbench, "MICROBENCHES", {"nanobot/fake_module.py": _TRIVIAL_SPEC})
    monkeypatch.setattr(microbench, "_git_show_bytes", lambda *a, **k: b"# FAST marker\n")
    monkeypatch.setattr(microbench, "run_measurement", lambda *a, **k: None)
    state_dir = tmp_path / "state"
    entry = microbench.measure_cycle(
        state_dir, tmp_path / "repo", "cyc-y", "deadbeef", "cafefeed", ["nanobot/fake_module.py"],
    )
    assert entry is None
    assert not (state_dir / "heldout" / "microbench.json").exists()


def test_measure_cycle_returns_none_without_base_or_head(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(microbench, "MICROBENCHES", {"nanobot/fake_module.py": _TRIVIAL_SPEC})
    state_dir = tmp_path / "state"
    assert microbench.measure_cycle(
        state_dir, tmp_path / "repo", "cyc-z", None, "cafefeed", ["nanobot/fake_module.py"],
    ) is None
    assert microbench.measure_cycle(
        state_dir, tmp_path / "repo", "cyc-z", "deadbeef", "", ["nanobot/fake_module.py"],
    ) is None
    assert not (state_dir / "heldout" / "microbench.json").exists()


def test_run_measurement_returns_none_when_spec_correctness_check_fails(monkeypatch):
    """opus-review RED-3: a spec that computes a (fake, fast) timing but then
    fails its own correctness check must sys.exit(1) — run_measurement must
    read that nonzero exit as a failed measurement (None), never as a valid
    (fast-but-wrong) number. Uses the REAL current repo as the product
    tree — the spec never touches the fake module file, so no fixture repo
    is needed."""
    wrong_but_fast_spec = (
        "import sys\n"
        "print(0.001)\n"  # a suspiciously fast timing ...
        "sys.exit(1)\n"  # ... but the spec's own correctness check failed
    )
    monkeypatch.setattr(microbench, "MICROBENCHES", {"nanobot/fake_module.py": wrong_but_fast_spec})
    result = microbench.run_measurement("nanobot/fake_module.py")
    assert result is None


# ─── MED-2: baseline drift guard (bracketed double-baseline measurement) ───


def _fake_run_measurement_sequence(values: list):
    """A run_measurement stand-in that returns ``values`` in call order,
    ignoring which module/candidate_bytes was requested — models
    measure_cycle's three real calls (baseline, candidate, baseline again)
    deterministically."""
    calls = {"n": 0}

    def _fake(module_path, candidate_bytes=None, *args, **kwargs):
        v = values[calls["n"]]
        calls["n"] += 1
        return v

    return _fake


def test_measure_cycle_drift_guard_rejects_noisy_baselines(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(microbench, "MICROBENCHES", {"nanobot/fake_module.py": _TRIVIAL_SPEC})
    monkeypatch.setattr(microbench, "_git_show_bytes", lambda *a, **k: b"# FAST marker\n")
    # baseline1=100, candidate=10, baseline2=120 -> drift = |100-120|/100 = 20% > 5%
    monkeypatch.setattr(
        microbench, "run_measurement", _fake_run_measurement_sequence([100.0, 10.0, 120.0]),
    )
    state_dir = tmp_path / "state"
    entry = microbench.measure_cycle(
        state_dir, tmp_path / "repo", "cyc-noisy", "deadbeef", "cafefeed", ["nanobot/fake_module.py"],
    )
    assert entry is None
    assert not (state_dir / "heldout" / "microbench.json").exists()


def test_measure_cycle_drift_guard_accepts_stable_baselines(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(microbench, "MICROBENCHES", {"nanobot/fake_module.py": _TRIVIAL_SPEC})
    monkeypatch.setattr(microbench, "_git_show_bytes", lambda *a, **k: b"# FAST marker\n")
    # baseline1=100, candidate=10, baseline2=102 -> drift = |100-102|/100 = 1.96% <= 5%
    monkeypatch.setattr(
        microbench, "run_measurement", _fake_run_measurement_sequence([100.0, 10.0, 102.0]),
    )
    state_dir = tmp_path / "state"
    entry = microbench.measure_cycle(
        state_dir, tmp_path / "repo", "cyc-stable", "deadbeef", "cafefeed", ["nanobot/fake_module.py"],
    )
    assert entry is not None
    assert entry["baseline_ms"] == 100.0  # min(100, 102)
    assert entry["baseline_ms_runs"] == [100.0, 102.0]
    assert entry["baseline_source"] == "installed"
    assert entry["candidate_ms"] == 10.0
    assert entry["improvement_pct"] == pytest.approx(90.0)


# ─── 4. benchmark_evidence.verify_benchmark integration ────────────────────


_DEFAULT_MICROBENCH_MODULE = "nanobot/runtime/existence_index.py"
_DEFAULT_MICROBENCH_METRIC = "wall_ms_best_of_5"


def _write_microbench_entry(
    state_dir: Path, cycle_id: str, *,
    baseline_ms: float, candidate_ms: float, improvement_pct: float,
    module: str = _DEFAULT_MICROBENCH_MODULE, metric: str = _DEFAULT_MICROBENCH_METRIC,
) -> None:
    path = state_dir / "heldout" / "microbench.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "schema_version": "heldout-microbench-v1",
        "entries": {
            cycle_id: {
                "module": module,
                "metric": metric,
                "baseline_ms": baseline_ms,
                "candidate_ms": candidate_ms,
                "improvement_pct": improvement_pct,
                "direction": "lower",
                "base_sha": "base123",
                "head_sha": "head456",
                "measured_at_utc": "2026-08-01T00:00:00Z",
                "schema": "heldout-microbench-entry-v1",
            }
        },
    }
    path.write_text(json.dumps(data), encoding="utf-8")


def _write_malformed_microbench_entry(state_dir: Path, cycle_id: str) -> None:
    path = state_dir / "heldout" / "microbench.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "schema_version": "heldout-microbench-v1",
        "entries": {
            cycle_id: {
                "module": _DEFAULT_MICROBENCH_MODULE,
                "baseline_ms": "not-a-number",
                "candidate_ms": 10.0,
                "improvement_pct": 90.0,
            }
        },
    }
    path.write_text(json.dumps(data), encoding="utf-8")


def _write_legacy_history(
    state_dir: Path, *, integration_ts: datetime, before_value: float = 1000, after_value: float = 400,
) -> None:
    before_ts = (integration_ts - timedelta(days=1)).isoformat().replace("+00:00", "Z")
    after_ts = (integration_ts + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    path = state_dir / "scorecard" / "history.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps({"computed_at_utc": before_ts, "cost": {"tokens_per_integration": before_value}}),
        json.dumps({"computed_at_utc": after_ts, "cost": {"tokens_per_integration": after_value}}),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_claim(
    state_dir: Path, cycle_id: str, *,
    metric: str = "tokens_per_integration", module: "str | None" = None,
    baseline: float = 1000, new_value: float = 400, direction: str = "lower_is_better",
) -> None:
    """Write the INSTANCE's optimization claim (state/benchmarks/{cycle_id}.json)
    — the file :func:`benchmark_evidence.verify_benchmark`'s RED-2 claim-match
    check reads to decide whether a present microbench entry actually applies
    to this claim (``metric``+``module`` equality)."""
    path = benchmark_evidence.benchmark_path(state_dir, cycle_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "metric": metric,
        "baseline": baseline,
        "new_value": new_value,
        "method": "test claim",
        "direction": direction,
    }
    if module is not None:
        payload["module"] = module
    path.write_text(json.dumps(payload), encoding="utf-8")


class TestVerifyBenchmarkMicrobenchAuthoritative:
    """A well-formed microbench entry is authoritative ONLY when the
    instance's own claim (state/benchmarks/{cycle_id}.json) references it
    by metric+module (opus-review RED-2) — otherwise it defers to the
    legacy 7-day-aggregate path exactly as if no entry existed."""

    def test_well_formed_entry_above_threshold_with_matching_claim_is_true(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("SELFEVO_BENCHMARK_TRUST", "1")
        _write_microbench_entry(
            tmp_path, "cyc-good", baseline_ms=100.0, candidate_ms=90.0, improvement_pct=10.0,
        )
        _write_claim(
            tmp_path, "cyc-good",
            metric=_DEFAULT_MICROBENCH_METRIC, module=_DEFAULT_MICROBENCH_MODULE,
        )
        assert benchmark_evidence.verify_benchmark(tmp_path, "cyc-good", "2026-08-01T00:00:00Z") is True

    def test_well_formed_entry_below_threshold_with_matching_claim_is_false_even_though_legacy_would_corroborate(
        self, tmp_path: Path, monkeypatch,
    ):
        monkeypatch.setenv("SELFEVO_BENCHMARK_TRUST", "1")
        integration_ts = datetime(2026, 8, 1, tzinfo=timezone.utc)
        # Legacy aggregates WOULD corroborate this exact metric (1000 -> 400)
        # — proves the microbench verdict is authoritative, not just additive,
        # once the claim actually matches the entry.
        _write_legacy_history(tmp_path, integration_ts=integration_ts, before_value=1000, after_value=400)
        _write_claim(tmp_path, "cyc-noop", metric="tokens_per_integration", module="scripts/whatever.py")
        _write_microbench_entry(
            tmp_path, "cyc-noop", baseline_ms=100.0, candidate_ms=99.0, improvement_pct=1.0,
            module="scripts/whatever.py", metric="tokens_per_integration",
        )
        assert benchmark_evidence.verify_benchmark(tmp_path, "cyc-noop", integration_ts) is False

    def test_malformed_entry_falls_through_to_legacy(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("SELFEVO_BENCHMARK_TRUST", "1")
        integration_ts = datetime(2026, 8, 1, tzinfo=timezone.utc)
        _write_legacy_history(tmp_path, integration_ts=integration_ts)
        _write_claim(tmp_path, "cyc-malformed")
        _write_malformed_microbench_entry(tmp_path, "cyc-malformed")
        assert benchmark_evidence.verify_benchmark(tmp_path, "cyc-malformed", integration_ts) is True

    def test_no_entry_legacy_path_unchanged(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("SELFEVO_BENCHMARK_TRUST", "1")
        integration_ts = datetime(2026, 8, 1, tzinfo=timezone.utc)
        _write_legacy_history(tmp_path, integration_ts=integration_ts)
        _write_claim(tmp_path, "cyc-legacy-only")
        assert benchmark_evidence.verify_benchmark(tmp_path, "cyc-legacy-only", integration_ts) is True

    def test_trust_off_is_false_regardless_of_microbench_entry(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("SELFEVO_BENCHMARK_TRUST", "0")
        _write_microbench_entry(
            tmp_path, "cyc-trustoff", baseline_ms=100.0, candidate_ms=10.0, improvement_pct=90.0,
        )
        _write_claim(
            tmp_path, "cyc-trustoff",
            metric=_DEFAULT_MICROBENCH_METRIC, module=_DEFAULT_MICROBENCH_MODULE,
        )
        assert benchmark_evidence.verify_benchmark(tmp_path, "cyc-trustoff", "2026-08-01T00:00:00Z") is False


class TestVerifyBenchmarkClaimMatching:
    """opus-review RED-2: the microbench short-circuit must key off the
    instance's OWN claim (metric+module), not just cycle_id — an unrelated
    claim in the same cycle must never be verified/revoked on a measurement
    it doesn't reference."""

    def test_unrelated_metric_claim_falls_through_to_legacy_unaffected_by_unrelated_entry(
        self, tmp_path: Path, monkeypatch,
    ):
        monkeypatch.setenv("SELFEVO_BENCHMARK_TRUST", "1")
        integration_ts = datetime(2026, 8, 1, tzinfo=timezone.utc)
        _write_legacy_history(tmp_path, integration_ts=integration_ts)  # corroborates True
        # The instance's claim is an ordinary tokens_per_integration claim —
        # it never mentions the microbench module/metric at all.
        _write_claim(tmp_path, "cyc-unrelated", metric="tokens_per_integration")
        # A huge, UNRELATED microbench win exists for this same cycle_id
        # (e.g. an incidental existence_index touch) — it must NOT leak in.
        _write_microbench_entry(
            tmp_path, "cyc-unrelated", baseline_ms=100.0, candidate_ms=10.0, improvement_pct=90.0,
        )
        assert benchmark_evidence.verify_benchmark(tmp_path, "cyc-unrelated", integration_ts) is True

    def test_absent_claim_with_well_formed_entry_falls_through_to_legacy(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("SELFEVO_BENCHMARK_TRUST", "1")
        integration_ts = datetime(2026, 8, 1, tzinfo=timezone.utc)
        # No claim file at all — a well-formed microbench entry must not be
        # treated as authoritative for a claim that doesn't exist.
        _write_microbench_entry(
            tmp_path, "cyc-noclaim", baseline_ms=100.0, candidate_ms=10.0, improvement_pct=90.0,
        )
        assert benchmark_evidence.verify_benchmark(tmp_path, "cyc-noclaim", integration_ts) is False

    def test_matching_claim_uses_harness_numbers_not_claims_own_forged_numbers(
        self, tmp_path: Path, monkeypatch,
    ):
        monkeypatch.setenv("SELFEVO_BENCHMARK_TRUST", "1")
        cycle_id = "cyc-match-forged"
        # Harness measured a genuine 10% win.
        _write_microbench_entry(
            tmp_path, cycle_id, baseline_ms=100.0, candidate_ms=90.0, improvement_pct=10.0,
        )
        # The claim references the SAME metric+module (so it is authoritative)
        # but carries wildly forged baseline/new_value numbers that must be
        # IGNORED — only the harness's improvement_pct decides.
        _write_claim(
            tmp_path, cycle_id,
            metric=_DEFAULT_MICROBENCH_METRIC, module=_DEFAULT_MICROBENCH_MODULE,
            baseline=999999, new_value=1,
        )
        assert benchmark_evidence.verify_benchmark(tmp_path, cycle_id, "2026-08-01T00:00:00Z") is True

    def test_matching_claim_below_threshold_is_false_despite_forged_claim_numbers(
        self, tmp_path: Path, monkeypatch,
    ):
        monkeypatch.setenv("SELFEVO_BENCHMARK_TRUST", "1")
        cycle_id = "cyc-match-forged-low"
        # Harness measured only a 1% win (below the 5% noise floor).
        _write_microbench_entry(
            tmp_path, cycle_id, baseline_ms=100.0, candidate_ms=99.0, improvement_pct=1.0,
        )
        # Claim matches by metric+module, but its OWN numbers claim a huge
        # win — must be ignored; the harness's 1% loses.
        _write_claim(
            tmp_path, cycle_id,
            metric=_DEFAULT_MICROBENCH_METRIC, module=_DEFAULT_MICROBENCH_MODULE,
            baseline=1, new_value=0.0001,
        )
        assert benchmark_evidence.verify_benchmark(tmp_path, cycle_id, "2026-08-01T00:00:00Z") is False


# ─── 5. cycle_id sanitization ───────────────────────────────────────────────


@pytest.mark.parametrize("bad_id", ["../evil", "..\\evil", "a/b", "a\\b", "", "   ", ".."])
def test_measure_cycle_rejects_unsafe_cycle_id(tmp_path: Path, bad_id, monkeypatch):
    monkeypatch.setattr(microbench, "MICROBENCHES", {"fake_module.py": _TRIVIAL_SPEC})
    state_dir = tmp_path / "state"
    entry = microbench.measure_cycle(
        state_dir, tmp_path / "repo", bad_id, "deadbeef", "cafefeed", ["fake_module.py"],
    )
    assert entry is None
    assert not (state_dir / "heldout" / "microbench.json").exists()


@pytest.mark.parametrize("bad_id", ["../evil", "..\\evil", "a/b", "a\\b", "", "..", None])
def test_load_microbench_entry_rejects_unsafe_cycle_id(tmp_path: Path, bad_id):
    assert microbench.load_microbench_entry(tmp_path, bad_id) is None


# ─── 6. FITNESS_SIDECARS ────────────────────────────────────────────────────


def test_fitness_sidecars_contains_microbench():
    from nanobot.runtime import scorecard

    assert "heldout/microbench.json" in scorecard.FITNESS_SIDECARS


# ─── 7. bridge.py call-site placement (opus-review RED-1/MED-1) ────────────
#
# Statically verifies (same lexical-order-in-source technique as
# test_bridge_subagent_workspace.py) that the measure_cycle call:
#   (a) runs AFTER the #789 integrity post-hash check — writing
#       heldout/microbench.json (itself a fitness sidecar) INSIDE the
#       _integrity_pre/_integrity_post window fired a false
#       'sidecar_write_during_spawn' incident on every measured cycle; and
#   (b) runs on the gate-PASS path only, immediately before
#       _record_runtime_slice_candidate — never on blocked/violation/
#       smoke-failed cycles, which would otherwise pay for 2 (now 3, #822
#       MED-2) worktrees + subprocesses and persist an AUTHORITATIVE entry
#       for code that never lands.

_BRIDGE_PATH = _REPO_ROOT / "nanobot" / "runtime" / "bridge.py"


def test_microbench_call_site_is_after_integrity_check_and_on_gate_pass_path():
    source = _BRIDGE_PATH.read_text(encoding="utf-8")

    integrity_post_pos = source.index("_integrity_post = _fitness_sidecar_hashes(STATE_DIR)")
    gate_pass_record_pos = source.index(
        "STATE_DIR, _cycle_id, True, 'runtime_slice_gate_passed', [],"
    )
    measure_call_pos = source.index("from nanobot.runtime.heldout.microbench import measure_cycle")
    candidate_call_pos = source.index("_cand_id = _record_runtime_slice_candidate(")

    assert integrity_post_pos < measure_call_pos, (
        "measure_cycle must run AFTER the #789 integrity post-hash check — "
        "otherwise every measured cycle writes heldout/microbench.json "
        "inside the pre/post hash window and fires a false "
        "sidecar_write_during_spawn incident (opus-review RED-1)"
    )
    assert gate_pass_record_pos < measure_call_pos < candidate_call_pos, (
        "measure_cycle must run strictly between the runtime-slice "
        "gate-PASS record_gate_decision(...) call and "
        "_record_runtime_slice_candidate(...) — i.e. only on the gate-PASS "
        "path, never for blocked/violation/smoke-failed cycles "
        "(opus-review RED-1/MED-1)"
    )
