"""Tests for #812: bounded runtime-slice tier.

The bounded mutation surface historically forbade the loop from touching any
``nanobot/`` runtime code, so its PRIMARY goal (Vector 1: self-optimize its own
runtime) was structurally unreachable. #812 adds a SECOND tier: an operator-
approved slice of ``nanobot/runtime/*.py`` modules the loop MAY propose changes
to, behind a hardened, fail-closed classifier and a stricter gate — runtime
changes never auto-integrate, they land as promotion candidates for operator
review.

These tests cover the surface classifier + helpers (pure, env-driven) and the
promotion-candidate recorder. The gate wiring that routes a ``'runtime'`` tier to
the candidate path is exercised indirectly via ``_record_runtime_slice_candidate``.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from nanobot.runtime import bridge

_SLICE_ENV = "SELFEVO_RUNTIME_SLICE"
_ALLOWED_SLICE = "nanobot/runtime/probes.py"


# ─── _is_runtime_deny (immutable safety shell) ───────────────────────────────

def test_deny_explicit_files():
    for f in (
        "nanobot/runtime/bridge.py",
        "nanobot/runtime/promotion.py",
        "nanobot/runtime/coordinator.py",
    ):
        assert bridge._is_runtime_deny(f), f

def test_deny_token_match():
    # basename token match covers future gate/safety/approval modules
    for f in (
        "nanobot/runtime/some_gate.py",
        "nanobot/runtime/precheck_new.py",
        "nanobot/runtime/policy_approval.py",
        "nanobot/runtime/safety_shell.py",
        "nanobot/runtime/stop_guards.py",
    ):
        assert bridge._is_runtime_deny(f), f

def test_deny_allows_plain_compute_module():
    assert not bridge._is_runtime_deny("nanobot/runtime/probes.py")
    assert not bridge._is_runtime_deny("nanobot/runtime/system_map.py")

def test_deny_normalizes_backslashes():
    assert bridge._is_runtime_deny("nanobot\\runtime\\bridge.py")

def test_deny_case_insensitive_explicit_file():
    assert bridge._is_runtime_deny("nanobot/runtime/Bridge.py")
    assert bridge._is_runtime_deny("nanobot/runtime/PROMOTION.py")

def test_deny_collapses_traversal_to_real_safety_file():
    # a traversal that resolves onto a real deny file is still denied
    assert bridge._is_runtime_deny("nanobot/runtime/x/../promotion.py")
    assert bridge._is_runtime_deny("nanobot/runtime/./coordinator.py")

def test_slice_rejects_traversal_out_of_runtime(monkeypatch):
    # '../bridge.py' collapses to nanobot/bridge.py → not under runtime/ → dropped
    monkeypatch.setenv(_SLICE_ENV, "nanobot/runtime/../bridge.py")
    assert bridge._runtime_slice_paths() == set()


# ─── _runtime_slice_paths (operator env allow-list) ──────────────────────────

def test_slice_empty_when_unset(monkeypatch):
    monkeypatch.delenv(_SLICE_ENV, raising=False)
    assert bridge._runtime_slice_paths() == set()

def test_slice_parses_valid_runtime_paths(monkeypatch):
    monkeypatch.setenv(_SLICE_ENV, "nanobot/runtime/probes.py, nanobot/runtime/system_map.py")
    assert bridge._runtime_slice_paths() == {
        "nanobot/runtime/probes.py",
        "nanobot/runtime/system_map.py",
    }

def test_slice_ignores_non_runtime_and_non_py(monkeypatch):
    # env cannot re-open state/ or add non-.py paths
    monkeypatch.setenv(_SLICE_ENV, "state/goals/x.json,scripts/foo.py,nanobot/agent/x.py,nanobot/runtime/probes.py")
    assert bridge._runtime_slice_paths() == {"nanobot/runtime/probes.py"}

def test_slice_drops_deny_even_if_listed(monkeypatch):
    # deny-set always wins over the allow-slice env (fail-closed)
    monkeypatch.setenv(_SLICE_ENV, "nanobot/runtime/bridge.py,nanobot/runtime/probes.py")
    assert bridge._runtime_slice_paths() == {"nanobot/runtime/probes.py"}

def test_slice_normalizes_backslashes(monkeypatch):
    monkeypatch.setenv(_SLICE_ENV, "nanobot\\runtime\\probes.py")
    assert bridge._runtime_slice_paths() == {"nanobot/runtime/probes.py"}


# ─── _classify_mutation_surface (two-tier routing) ───────────────────────────

def test_classify_script_only_is_script_tier(monkeypatch):
    monkeypatch.delenv(_SLICE_ENV, raising=False)
    blocked, violations, tier = bridge._classify_mutation_surface(
        ["scripts/foo.py", "surfaces/x.json", "memory/MEMORY.md"]
    )
    assert blocked == []
    assert violations == []
    assert tier == "script"

def test_classify_runtime_slice_is_runtime_tier(monkeypatch):
    monkeypatch.setenv(_SLICE_ENV, _ALLOWED_SLICE)
    blocked, violations, tier = bridge._classify_mutation_surface([_ALLOWED_SLICE])
    assert blocked == []
    assert violations == []
    assert tier == "runtime"

def test_classify_runtime_file_not_in_slice_is_violation(monkeypatch):
    # feature off (empty env): a runtime file is outside every surface → violation
    monkeypatch.delenv(_SLICE_ENV, raising=False)
    blocked, violations, tier = bridge._classify_mutation_surface([_ALLOWED_SLICE])
    assert blocked == []
    assert len(violations) == 1
    assert tier == "script"

def test_classify_deny_path_is_violation_even_if_in_slice(monkeypatch):
    # operator mistakenly lists a deny path AND the loop touches it → hard block
    monkeypatch.setenv(_SLICE_ENV, "nanobot/runtime/bridge.py")
    blocked, violations, tier = bridge._classify_mutation_surface(["nanobot/runtime/bridge.py"])
    assert blocked == []
    assert len(violations) == 1
    assert "deny-set" in violations[0]

def test_classify_mixed_slice_plus_deny_is_rejected(monkeypatch):
    # a diff carrying any deny path is blocked as a whole, even alongside an
    # allowed slice file (fail-closed)
    monkeypatch.setenv(_SLICE_ENV, _ALLOWED_SLICE)
    blocked, violations, tier = bridge._classify_mutation_surface(
        [_ALLOWED_SLICE, "nanobot/runtime/coordinator.py"]
    )
    assert len(violations) == 1
    assert "deny-set" in violations[0]

def test_classify_mixed_slice_plus_script_is_runtime_tier(monkeypatch):
    # slice + ordinary script file is allowed but escalates the whole cycle to
    # the stricter runtime tier
    monkeypatch.setenv(_SLICE_ENV, _ALLOWED_SLICE)
    blocked, violations, tier = bridge._classify_mutation_surface(
        [_ALLOWED_SLICE, "scripts/foo.py"]
    )
    assert violations == []
    assert tier == "runtime"

def test_classify_blocked_pattern_still_enforced(monkeypatch):
    monkeypatch.setenv(_SLICE_ENV, _ALLOWED_SLICE)
    blocked, violations, tier = bridge._classify_mutation_surface(["surfaces/secret_key.json"])
    assert len(blocked) == 1
    assert "secret" in blocked[0]

def test_classify_outside_surface_is_violation(monkeypatch):
    monkeypatch.delenv(_SLICE_ENV, raising=False)
    blocked, violations, tier = bridge._classify_mutation_surface(["state/goals/history.json"])
    assert len(violations) == 1
    assert "outside allowed paths" in violations[0]


# ─── #863: gate extension policy (WHAT KIND of file may auto-integrate) ─────
# Prefix rules (_ALLOWED_PATH_PREFIXES) bound WHERE the instance may write.
# The smoke gate only py_compiles changed .py files and runs pytest — a
# non-Python/non-text file (e.g. scripts/foo.rs, scripts/blob.so) under an
# allowed prefix previously passed both checks unexercised. These tests cover
# the extension/basename allowlist enforced inside _classify_mutation_surface.

def test_classify_rust_extension_is_violation(monkeypatch):
    monkeypatch.delenv(_SLICE_ENV, raising=False)
    blocked, violations, tier = bridge._classify_mutation_surface(["scripts/foo.rs"])
    assert blocked == []
    assert len(violations) == 1
    assert "not gate-exercisable" in violations[0]
    assert "scripts/foo.rs" in violations[0]

def test_classify_shared_object_extension_is_violation(monkeypatch):
    monkeypatch.delenv(_SLICE_ENV, raising=False)
    blocked, violations, tier = bridge._classify_mutation_surface(["scripts/blob.so"])
    assert blocked == []
    assert len(violations) == 1
    assert "not gate-exercisable" in violations[0]
    assert "scripts/blob.so" in violations[0]

def test_classify_known_extensions_have_no_extension_violation(monkeypatch):
    monkeypatch.delenv(_SLICE_ENV, raising=False)
    for f in (
        "scripts/tool.sh",
        "docs/note.md",
        "surfaces/w.json",
        "memory/x.yaml",
    ):
        blocked, violations, tier = bridge._classify_mutation_surface([f])
        assert blocked == [], f
        assert violations == [], f
        assert tier == "script", f

def test_classify_makefile_basename_is_allowed(monkeypatch):
    # basename allowlist covers extension-less build files (checked before suffix).
    monkeypatch.delenv(_SLICE_ENV, raising=False)
    blocked, violations, tier = bridge._classify_mutation_surface(["scripts/Makefile"])
    assert blocked == []
    assert violations == []
    assert tier == "script"

def test_classify_example_suffix_is_allowed(monkeypatch):
    # multi-dot filename: Path.suffix is only the last dotted segment (".example").
    monkeypatch.delenv(_SLICE_ENV, raising=False)
    blocked, violations, tier = bridge._classify_mutation_surface(["surfaces/settings.example"])
    assert blocked == []
    assert violations == []
    assert tier == "script"

def test_classify_extensionless_file_is_violation(monkeypatch):
    # the most likely real bypass: an extension-less payload (suffix '') in an
    # allowed prefix whose basename is not allowlisted — must fail closed.
    monkeypatch.delenv(_SLICE_ENV, raising=False)
    blocked, violations, tier = bridge._classify_mutation_surface(["scripts/payload"])
    assert blocked == []
    assert len(violations) == 1
    assert "not gate-exercisable" in violations[0]
    assert "scripts/payload" in violations[0]


def test_classify_extension_violation_and_prefix_violation_are_independent(monkeypatch):
    # a file outside the allowed prefixes is still a plain "outside allowed
    # paths" violation, not an extension violation — the extension check only
    # runs once the prefix check already passed.
    monkeypatch.delenv(_SLICE_ENV, raising=False)
    blocked, violations, tier = bridge._classify_mutation_surface(["state/foo.rs"])
    assert blocked == []
    assert len(violations) == 1
    assert "outside allowed paths" in violations[0]
    assert "not gate-exercisable" not in violations[0]


# ─── _record_runtime_slice_candidate (promotion, not integration) ────────────

def test_record_candidate_writes_pending_promotion(tmp_path: Path):
    state_dir = tmp_path / "state"
    cand_id = bridge._record_runtime_slice_candidate(
        state_dir=state_dir,
        repo_root=tmp_path,          # not a git repo → diff empty, head None (graceful)
        cycle_id="cycle-abc123",
        cycle_branch="cycle/cycle-abc123",
        base_sha="deadbeef",
        changed_files=[_ALLOWED_SLICE],
    )
    path = state_dir / "promotions" / f"{cand_id}.json"
    assert path.exists()
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["tier"] == "runtime"
    assert record["review_status"] == "not_ready_for_policy_review"
    assert record["decision"] == "not_ready_for_policy_review"
    assert record["changed_files"] == [_ALLOWED_SLICE]
    assert record["rollback_record"]["cycle_branch"] == "cycle/cycle-abc123"
    assert record["rollback_record"]["base_sha"] == "deadbeef"
    assert record["rollback_record"]["retained_branch"] is True
    assert record["recommended_next_action"] == "operator_review_then_product_pr"

def test_record_candidate_never_raises_on_bad_repo(tmp_path: Path):
    # best-effort: a git/diff failure must not crash the gate
    cand_id = bridge._record_runtime_slice_candidate(
        state_dir=tmp_path / "state",
        repo_root=tmp_path / "does-not-exist",
        cycle_id="cycle-xyz",
        cycle_branch="cycle/cycle-xyz",
        base_sha=None,
        changed_files=[_ALLOWED_SLICE],
    )
    assert cand_id.startswith("promotion-runtime-")
