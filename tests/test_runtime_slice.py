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

import hashlib
import json
from pathlib import Path

import pytest

from nanobot.runtime import bridge, gate, runtime_deny

_SLICE_ENV = "SELFEVO_RUNTIME_SLICE"
_ALLOWED_SLICE = "nanobot/runtime/probes.py"
# #876: bridge._runtime_slice_paths() now delegates to
# promoted_overlay.effective_runtime_slice (env slice UNION earned ladder
# rungs). With zero active promotions (the case in every test below — no
# PROMOTED_TREE is set up) the ladder contributes nothing at all
# (runtime_deny.earned_ladder_slice(set()) == set()), so every exact-set
# assertion below is UNCHANGED from pre-#876 — this is the
# byte-identical-at-zero-promotions invariant.


# ─── #875: deny-set logic extracted to nanobot.runtime.runtime_deny ──────────
# bridge.py re-exports the same names UNCHANGED so every test above (written
# against bridge._is_runtime_deny / bridge._runtime_slice_paths) keeps passing
# without modification — these tests additionally pin the re-export identity
# and exercise the new pure (env-string-argument) function directly, since
# the root verifier and the agent-side overlay loader both import it that way.

def test_bridge_policy_mirrors_stay_synced_with_gate():
    assert bridge._BLOCKED_FILE_PATTERNS == gate._BLOCKED_FILE_PATTERNS
    assert bridge._BLOCKED_WORD_PATTERNS == gate._BLOCKED_WORD_PATTERNS
    assert bridge._SENSITIVE_WORDS == gate._SENSITIVE_WORDS
    assert bridge._ALLOWED_SENSITIVE_BASENAMES == gate._ALLOWED_SENSITIVE_BASENAMES
    assert bridge._BLOCKED_EXACT_PATHS == gate._BLOCKED_EXACT_PATHS
    assert bridge._ALLOWED_PATH_PREFIXES == gate._ALLOWED_PATH_PREFIXES
    assert bridge._ALLOWED_EXACT_PATHS == gate._ALLOWED_EXACT_PATHS
    assert bridge._GATE_EXT_ALLOWLIST == gate._GATE_EXT_ALLOWLIST
    assert bridge._GATE_BASENAME_ALLOWLIST == gate._GATE_BASENAME_ALLOWLIST


def test_bridge_reexports_are_the_same_object_as_runtime_deny():
    assert bridge._is_runtime_deny is runtime_deny._is_runtime_deny
    assert bridge._RUNTIME_DENY_ALWAYS_FILES is runtime_deny._RUNTIME_DENY_ALWAYS_FILES
    assert bridge._RUNTIME_DENY_TOKENS is runtime_deny._RUNTIME_DENY_TOKENS


def test_bridge_wrapper_matches_effective_runtime_slice(monkeypatch):
    # #876: bridge._runtime_slice_paths() now delegates to
    # promoted_overlay.effective_runtime_slice (env slice UNION earned
    # ladder rungs), not the bare pure parser — pin that wiring directly.
    from nanobot.runtime.promoted_overlay import effective_runtime_slice

    monkeypatch.setenv(_SLICE_ENV, "nanobot/runtime/probes.py,nanobot/runtime/bridge.py")
    assert bridge._runtime_slice_paths() == effective_runtime_slice(
        "nanobot/runtime/probes.py,nanobot/runtime/bridge.py"
    )


def test_runtime_deny_pure_function_takes_arg_not_environ(monkeypatch):
    # the pure function must NOT read os.environ itself — only its argument
    monkeypatch.setenv(_SLICE_ENV, "nanobot/runtime/bridge.py")  # deny-only, would be dropped anyway
    assert runtime_deny.runtime_slice_paths("nanobot/runtime/probes.py") == {"nanobot/runtime/probes.py"}


def test_runtime_deny_pure_function_none_and_empty():
    assert runtime_deny.runtime_slice_paths(None) == set()
    assert runtime_deny.runtime_slice_paths("") == set()


# ─── _is_runtime_deny (immutable safety shell) ───────────────────────────────

def test_deny_explicit_files():
    for f in (
        "nanobot/runtime/bridge.py",
        "nanobot/runtime/promotion.py",
        "nanobot/runtime/coordinator.py",
    ):
        assert bridge._is_runtime_deny(f), f

def test_deny_covers_the_rest_of_the_verification_kernel():
    # #875 YELLOW-2 fix (opus-review round 2): the proposal claims the
    # verification kernel is structurally never promotable — this asserts
    # that for every module beyond bridge/promotion/coordinator whose
    # compromise (or deletion, or self-weakening) would also break the
    # #875 trust boundary. An operator accidentally listing one of these
    # in SELFEVO_RUNTIME_SLICE must never make it eligible.
    for f in (
        "nanobot/runtime/scorecard.py",
        "nanobot/runtime/benchmark_evidence.py",
        "nanobot/runtime/usage_evidence.py",
        "nanobot/runtime/promoted_overlay.py",
        "nanobot/runtime/runtime_deny.py",
        "nanobot/runtime/validator_harness.py",
        "nanobot/runtime/heldout/microbench.py",
        "nanobot/runtime/heldout/__init__.py",
        "nanobot/runtime/heldout/checkers.py",
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


def test_slice_earned_ladder_rung_added_when_rung0_promotion_active(tmp_path, monkeypatch):
    # #876: with rung 0 (existence_index.py) genuinely ACTIVE in
    # PROMOTED_TREE's manifest, rung 1 (demand.py) is earned and appears in
    # bridge._runtime_slice_paths() even though the operator never listed it.
    import hashlib

    flat = "nanobot__runtime__existence_index.py"
    data = b"X = 1\n"
    (tmp_path / flat).write_bytes(data)
    (tmp_path / "manifest.json").write_text(
        json.dumps({
            "nanobot/runtime/existence_index.py": {
                "sha256": hashlib.sha256(data).hexdigest(),
                "status": "active",
            },
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr("nanobot.runtime.promoted_overlay._boundary_ok", lambda *_: True)
    monkeypatch.setenv("PROMOTED_TREE", str(tmp_path))
    monkeypatch.setenv(_SLICE_ENV, "nanobot/runtime/existence_index.py")
    assert bridge._runtime_slice_paths() == {
        "nanobot/runtime/existence_index.py",
        "nanobot/runtime/demand.py",
    }


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


def test_validator_harness_is_denied_by_all_three_trust_call_sites(tmp_path, monkeypatch):
    """#1274: the validator grader must not be runtime-slice promotable."""
    path = "nanobot/runtime/validator_harness.py"
    assert runtime_deny._is_runtime_deny(path) is True

    monkeypatch.setenv(_SLICE_ENV, path)
    blocked, violations, tier = bridge._classify_mutation_surface([path])
    assert blocked == []
    assert len(violations) == 1
    assert "deny-set" in violations[0]
    assert tier == "script"

    from nanobot.runtime import promoted_overlay
    assert promoted_overlay.effective_runtime_slice(path, "T:/nonexistent-promoted-tree") == set()
    module_bytes = b"VALUE = 1\n"
    (tmp_path / "nanobot__runtime__validator_harness.py").write_bytes(module_bytes)
    (tmp_path / "manifest.json").write_text(
        json.dumps({path: {"sha256": hashlib.sha256(module_bytes).hexdigest(), "status": "active"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(promoted_overlay, "_boundary_ok", lambda *_: True)
    assert promoted_overlay.install_promoted_overlay(tmp_path) == []

    import importlib.util
    import os
    import sys
    import uuid
    verifier_path = Path(__file__).parents[1] / "host" / "eeepc" / "libexec" / "eeepc_promotion_verifier.py"
    name = f"eeepc_promotion_verifier_1274_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(name, verifier_path)
    verifier = importlib.util.module_from_spec(spec)
    sys.modules[name] = verifier
    old_skip = os.environ.get("EEEPC_VERIFIER_SKIP_OWNERSHIP_CHECK")
    old_release = os.environ.get("SELFEVO_RELEASE_DIR")
    os.environ["EEEPC_VERIFIER_SKIP_OWNERSHIP_CHECK"] = "1"
    os.environ["SELFEVO_RELEASE_DIR"] = str(verifier_path.parents[3])
    try:
        spec.loader.exec_module(verifier)
        eligible, reason, module_path = verifier._classify_candidate(
            {"changed_files": [path]}, {path}
        )
    finally:
        sys.modules.pop(name, None)
        if old_skip is None:
            os.environ.pop("EEEPC_VERIFIER_SKIP_OWNERSHIP_CHECK", None)
        else:
            os.environ["EEEPC_VERIFIER_SKIP_OWNERSHIP_CHECK"] = old_skip
        if old_release is None:
            os.environ.pop("SELFEVO_RELEASE_DIR", None)
        else:
            os.environ["SELFEVO_RELEASE_DIR"] = old_release
    assert eligible is False
    assert "deny-set" in reason
    assert module_path is None


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
