"""Tests for #875: the root-run promotion verifier (the automated operator).

``host/eeepc/libexec/eeepc_promotion_verifier.py`` is a standalone script
(not a ``nanobot`` package module — it is deployed to ``/usr/local/libexec``
and run by systemd as root), so it is loaded here via
``importlib.util.spec_from_file_location`` exactly the way it would be
invoked in production, with a REAL tiny git repository standing in for the
instance repo (so ``git show <head_sha>:<module_path>`` materialization is
genuinely exercised, not mocked).

Rework after #880 (opus-review RED1/RED2): the verifier no longer executes
the microbench measurement / held-out pack / git-show-against-instance-repo
directly — that work now happens inside a demoted CHILD subprocess
(``--child-verify``, see ``_run_child_verify``/``_child_verify_main`` in the
verifier module), and a fail-closed release-ownership check runs before the
verifier ever imports ``nanobot``. Most lifecycle tests below monkeypatch
the ``_run_child_verify`` seam (the same role ``run_measurement``/
``_run_heldout`` monkeypatching played before this rework) with a
deterministic "clean and improved" stand-in; a dedicated class exercises
the REAL child subprocess, the ownership fail-closed check, and the
demotion helpers directly.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_VERIFIER_PATH = _REPO_ROOT / "host" / "eeepc" / "libexec" / "eeepc_promotion_verifier.py"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _init_instance_repo(repo: Path, files: dict) -> str:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "a@example.invalid")
    _git(repo, "config", "user.name", "test")
    for rel, content in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode("utf-8"))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "commit")
    out = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True,
    )
    return out.stdout.strip()


def _load_verifier_module(env: dict) -> "tuple[object, str]":
    """Load a FRESH module instance of the verifier script with ``env``
    applied on top of the current process env (restored by the caller)."""
    mod_name = f"eeepc_promotion_verifier_test_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(mod_name, _VERIFIER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module, mod_name


def _make_child_verify_stub(
    module, *, baseline_ms=100.0, candidate_ms=80.0, heldout_clean=True, heldout_reason="",
):
    """A deterministic stand-in for ``module._run_child_verify`` — the same
    role ``run_measurement``/``_run_heldout`` monkeypatching played before
    the demoted-child rework. Still fetches REAL candidate bytes via the
    (unmodified) ``_git_show_bytes`` helper for ``mode="full"`` so sha256/
    promoted-file-content assertions downstream still exercise real git
    materialization, not a fake.
    """

    def _stub(mode, *, module_path=None, head_sha=None, timeout=None):
        if mode == "full":
            candidate_bytes = module._git_show_bytes(module.INSTANCE_REPO, head_sha, module_path)
            if candidate_bytes is None:
                return {"ok": False, "reason": f"stub: git show failed for {module_path}@{head_sha}"}
            return {
                "ok": True,
                "sha256": hashlib.sha256(candidate_bytes).hexdigest(),
                "baseline_ms": baseline_ms,
                "candidate_ms": candidate_ms,
                "heldout_clean": heldout_clean,
                "heldout_reason": heldout_reason,
            }
        if mode == "heldout":
            return {"ok": True, "heldout_clean": heldout_clean, "heldout_reason": heldout_reason}
        return {"ok": False, "reason": f"stub: unknown mode {mode!r}"}

    return _stub


@pytest.fixture
def verifier(tmp_path, monkeypatch):
    """Load a FRESH module instance of the verifier script per test, with
    STATE_DIR/INSTANCE_REPO/PROMOTED_TREE/SELFEVO_RUNTIME_SLICE env vars set
    BEFORE import (the script binds these to module-level constants at
    import time). ``EEEPC_VERIFIER_SKIP_OWNERSHIP_CHECK=1`` is set too —
    tests run as a non-root user against temp dirs/a real checkout that can
    never satisfy the real root-ownership check (RED1); that env var is
    also propagated into any child-verify subprocess this module spawns
    (see ``_minimal_child_env``), so the child's own copy of the same check
    doesn't fail either.

    ``_run_child_verify`` (the demoted-child orchestrator, RED2) is
    defaulted to a deterministic "20% improvement, clean heldout" stand-in
    — individual tests override ``verifier._run_child_verify`` as needed.
    The REAL function is kept accessible as ``verifier._real_run_child_verify``
    for the dedicated real-subprocess round-trip tests.
    """
    state_dir = tmp_path / "state"
    instance_repo = tmp_path / "instance_repo"
    promoted_tree = tmp_path / "promoted"
    (state_dir / "promotions").mkdir(parents=True)

    monkeypatch.setenv("STATE_DIR", str(state_dir))
    monkeypatch.setenv("INSTANCE_REPO", str(instance_repo))
    monkeypatch.setenv("PROMOTED_TREE", str(promoted_tree))
    monkeypatch.setenv("SELFEVO_RUNTIME_SLICE", "nanobot/runtime/existence_index.py")
    monkeypatch.setenv("SELFEVO_RELEASE_DIR", str(_REPO_ROOT))
    monkeypatch.setenv("EEEPC_VERIFIER_SKIP_OWNERSHIP_CHECK", "1")

    module, mod_name = _load_verifier_module({})

    module._real_run_child_verify = module._run_child_verify
    module._run_child_verify = _make_child_verify_stub(module)

    module.state_dir = state_dir
    module.instance_repo = instance_repo
    module.promoted_tree = promoted_tree
    yield module
    sys.modules.pop(mod_name, None)


def _write_candidate(state_dir: Path, candidate_id: str, changed_files, head_sha, **extra) -> Path:
    record = {
        "schema_version": "runtime-slice-promotion-candidate-v1",
        "promotion_candidate_id": candidate_id,
        "changed_files": changed_files,
        "rollback_record": {"head_sha": head_sha},
        **extra,
    }
    path = state_dir / "promotions" / f"{candidate_id}.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


class TestEligibleCandidateSoaksAndPromotes:
    def test_full_lifecycle_soak_then_promote(self, verifier):
        head_sha = _init_instance_repo(
            verifier.instance_repo, {"nanobot/runtime/existence_index.py": "X = 1\n"}
        )
        _write_candidate(
            verifier.state_dir, "promotion-runtime-cyc1",
            ["nanobot/runtime/existence_index.py"], head_sha,
        )

        s1 = verifier.verify_pass()
        assert s1["soaking"] == 1 and s1["rejected"] == 0

        vs = json.loads((verifier.promoted_tree / "verifier_state.json").read_text())
        entry = vs["candidates"]["promotion-runtime-cyc1"]
        assert entry["status"] == "soaking"
        assert entry["soak_passes_done"] == 0
        assert entry["improvement_pct"] == pytest.approx(20.0)

        # 3 subsequent clean passes required to promote.
        for _ in range(2):
            s = verifier.verify_pass()
            assert s["soaking"] == 1
        s_final = verifier.verify_pass()
        assert s_final["promoted"] == 1

        manifest = json.loads((verifier.promoted_tree / "manifest.json").read_text())
        entry = manifest["nanobot/runtime/existence_index.py"]
        assert entry["status"] == "active"
        assert entry["head_sha"] == head_sha
        assert entry["candidate_id"] == "promotion-runtime-cyc1"

        promoted_file = verifier.promoted_tree / "nanobot__runtime__existence_index.py"
        assert promoted_file.read_text(encoding="utf-8") == "X = 1\n"
        assert entry["sha256"] == hashlib.sha256(b"X = 1\n").hexdigest()

        # Terminal: further passes never reprocess this candidate_id again.
        vs = json.loads((verifier.promoted_tree / "verifier_state.json").read_text())
        assert vs["candidates"]["promotion-runtime-cyc1"]["status"] == "promoted"

    def test_forged_instance_microbench_number_is_ignored(self, verifier):
        """The instance's OWN claimed improvement_pct in the candidate record
        must never influence the verdict — only the root's own re-measure
        (via the demoted child) does. The child's numbers here are set
        BELOW threshold, so a forged 99% instance claim must still be
        rejected."""
        head_sha = _init_instance_repo(
            verifier.instance_repo, {"nanobot/runtime/existence_index.py": "X = 1\n"}
        )
        _write_candidate(
            verifier.state_dir, "promotion-runtime-forged",
            ["nanobot/runtime/existence_index.py"], head_sha,
            microbench={"improvement_pct": 99.0, "forged": True},
        )
        # real improvement: 2%, below threshold
        verifier._run_child_verify = _make_child_verify_stub(verifier, candidate_ms=98.0)
        s = verifier.verify_pass()
        assert s["rejected"] == 1
        vs = json.loads((verifier.promoted_tree / "verifier_state.json").read_text())
        entry = vs["candidates"]["promotion-runtime-forged"]
        assert entry["status"] == "rejected"
        assert "below required" in entry["reason"]


class TestRejectionPaths:
    def test_heldout_regression_at_measurement_time_rejects(self, verifier):
        head_sha = _init_instance_repo(
            verifier.instance_repo, {"nanobot/runtime/existence_index.py": "X = 1\n"}
        )
        _write_candidate(
            verifier.state_dir, "promotion-runtime-r1",
            ["nanobot/runtime/existence_index.py"], head_sha,
        )
        verifier._run_child_verify = _make_child_verify_stub(
            verifier, heldout_clean=False, heldout_reason="heldout regressions: ['scripts/foo.py']",
        )
        s = verifier.verify_pass()
        assert s["rejected"] == 1
        vs = json.loads((verifier.promoted_tree / "verifier_state.json").read_text())
        assert "heldout regressions" in vs["candidates"]["promotion-runtime-r1"]["reason"]

    def test_heldout_failure_at_measurement_time_rejects(self, verifier):
        head_sha = _init_instance_repo(
            verifier.instance_repo, {"nanobot/runtime/existence_index.py": "X = 1\n"}
        )
        _write_candidate(
            verifier.state_dir, "promotion-runtime-r2",
            ["nanobot/runtime/existence_index.py"], head_sha,
        )
        verifier._run_child_verify = _make_child_verify_stub(
            verifier, heldout_clean=False, heldout_reason="heldout failures: ['scripts/bar.py']",
        )
        s = verifier.verify_pass()
        assert s["rejected"] == 1

    def test_multi_file_candidate_is_rejected_not_measured(self, verifier):
        head_sha = _init_instance_repo(
            verifier.instance_repo, {
                "nanobot/runtime/existence_index.py": "X = 1\n",
                "nanobot/runtime/probes.py": "Y = 1\n",
            }
        )
        _write_candidate(
            verifier.state_dir, "promotion-runtime-multi",
            ["nanobot/runtime/existence_index.py", "nanobot/runtime/probes.py"], head_sha,
        )
        measured = []
        verifier._run_child_verify = lambda *a, **kw: measured.append(1) or {
            "ok": True, "heldout_clean": True, "heldout_reason": "",
        }
        s = verifier.verify_pass()
        assert s["rejected"] == 1
        assert measured == []  # never even attempted a child verify
        vs = json.loads((verifier.promoted_tree / "verifier_state.json").read_text())
        assert "exactly one" in vs["candidates"]["promotion-runtime-multi"]["reason"]

    def test_deny_set_module_is_rejected(self, verifier):
        head_sha = _init_instance_repo(
            verifier.instance_repo, {"nanobot/runtime/bridge.py": "X = 1\n"}
        )
        _write_candidate(
            verifier.state_dir, "promotion-runtime-deny",
            ["nanobot/runtime/bridge.py"], head_sha,
        )
        s = verifier.verify_pass()
        assert s["rejected"] == 1
        vs = json.loads((verifier.promoted_tree / "verifier_state.json").read_text())
        assert "deny-set" in vs["candidates"]["promotion-runtime-deny"]["reason"]
        assert not (verifier.promoted_tree / "manifest.json").exists() or json.loads(
            (verifier.promoted_tree / "manifest.json").read_text()
        ) == {"_schema_version": "promoted-manifest-v1"}

    def test_module_not_in_operator_slice_is_rejected(self, verifier, monkeypatch):
        monkeypatch.setenv("SELFEVO_RUNTIME_SLICE", "nanobot/runtime/existence_index.py")
        head_sha = _init_instance_repo(
            verifier.instance_repo, {"nanobot/runtime/probes.py": "X = 1\n"}
        )
        _write_candidate(
            verifier.state_dir, "promotion-runtime-notinslice",
            ["nanobot/runtime/probes.py"], head_sha,
        )
        s = verifier.verify_pass()
        assert s["rejected"] == 1
        vs = json.loads((verifier.promoted_tree / "verifier_state.json").read_text())
        assert "not in the operator-approved" in vs["candidates"]["promotion-runtime-notinslice"]["reason"]

    def test_missing_head_sha_is_rejected(self, verifier):
        _init_instance_repo(verifier.instance_repo, {"nanobot/runtime/existence_index.py": "X = 1\n"})
        path = verifier.state_dir / "promotions" / "promotion-runtime-nohash.json"
        path.write_text(json.dumps({
            "changed_files": ["nanobot/runtime/existence_index.py"],
            "rollback_record": {},
        }), encoding="utf-8")
        s = verifier.verify_pass()
        assert s["rejected"] == 1

    def test_unresolvable_head_sha_is_rejected(self, verifier):
        _init_instance_repo(verifier.instance_repo, {"nanobot/runtime/existence_index.py": "X = 1\n"})
        _write_candidate(
            verifier.state_dir, "promotion-runtime-badsha",
            ["nanobot/runtime/existence_index.py"], "0" * 40,
        )
        s = verifier.verify_pass()
        assert s["rejected"] == 1

    def test_malformed_candidate_json_does_not_abort_the_pass(self, verifier):
        head_sha = _init_instance_repo(
            verifier.instance_repo, {"nanobot/runtime/existence_index.py": "X = 1\n"}
        )
        (verifier.state_dir / "promotions" / "promotion-runtime-bad.json").write_text(
            "{not valid json", encoding="utf-8"
        )
        _write_candidate(
            verifier.state_dir, "promotion-runtime-good",
            ["nanobot/runtime/existence_index.py"], head_sha,
        )
        s = verifier.verify_pass()
        assert s["errors"] == 1
        assert s["soaking"] == 1  # the good sibling candidate still processed


class TestTerminalStatesNeverRetried:
    def test_rejected_candidate_is_not_reevaluated(self, verifier):
        head_sha = _init_instance_repo(
            verifier.instance_repo, {"nanobot/runtime/bridge.py": "X = 1\n"}
        )
        _write_candidate(
            verifier.state_dir, "promotion-runtime-deny",
            ["nanobot/runtime/bridge.py"], head_sha,
        )
        verifier.verify_pass()
        calls = []
        verifier._run_child_verify = lambda *a, **kw: calls.append(1) or {
            "ok": True, "heldout_clean": True, "heldout_reason": "",
        }
        verifier.verify_pass()
        assert calls == []  # never re-attempted


class TestSoakRegressionAndIntegrityWatch:
    def test_regression_during_soak_rejects(self, verifier):
        head_sha = _init_instance_repo(
            verifier.instance_repo, {"nanobot/runtime/existence_index.py": "X = 1\n"}
        )
        _write_candidate(
            verifier.state_dir, "promotion-runtime-soakfail",
            ["nanobot/runtime/existence_index.py"], head_sha,
        )
        verifier.verify_pass()  # enters soaking
        verifier._run_child_verify = _make_child_verify_stub(
            verifier, heldout_clean=False,
            heldout_reason="heldout regressions: ['scripts/newly_broken.py']",
        )
        s = verifier.verify_pass()
        assert s["rejected"] == 1
        vs = json.loads((verifier.promoted_tree / "verifier_state.json").read_text())
        assert vs["candidates"]["promotion-runtime-soakfail"]["status"] == "rejected"

    def test_integrity_event_during_soak_rejects(self, verifier):
        head_sha = _init_instance_repo(
            verifier.instance_repo, {"nanobot/runtime/existence_index.py": "X = 1\n"}
        )
        _write_candidate(
            verifier.state_dir, "promotion-runtime-integrity",
            ["nanobot/runtime/existence_index.py"], head_sha,
        )
        verifier.verify_pass()  # enters soaking, sets ledger watermark

        from nanobot.runtime import cycle_ledger
        cycle_ledger.append_event(verifier.state_dir, {"phase": "integrity", "reason": "test-tamper"})

        s = verifier.verify_pass()
        assert s["rejected"] == 1
        vs = json.loads((verifier.promoted_tree / "verifier_state.json").read_text())
        assert "integrity events" in vs["candidates"]["promotion-runtime-integrity"]["reason"]

    def test_regression_after_promotion_rolls_back(self, verifier):
        head_sha = _init_instance_repo(
            verifier.instance_repo, {"nanobot/runtime/existence_index.py": "X = 1\n"}
        )
        _write_candidate(
            verifier.state_dir, "promotion-runtime-pr1",
            ["nanobot/runtime/existence_index.py"], head_sha,
        )
        for _ in range(4):
            verifier.verify_pass()
        manifest = json.loads((verifier.promoted_tree / "manifest.json").read_text())
        assert manifest["nanobot/runtime/existence_index.py"]["status"] == "active"

        verifier._run_child_verify = _make_child_verify_stub(
            verifier, heldout_clean=False,
            heldout_reason="heldout regressions: ['scripts/broke_after_promotion.py']",
        )
        s = verifier.verify_pass()
        assert s["rolled_back"] == 1
        manifest = json.loads((verifier.promoted_tree / "manifest.json").read_text())
        assert "nanobot/runtime/existence_index.py" not in manifest
        promoted_file = verifier.promoted_tree / "nanobot__runtime__existence_index.py"
        assert not promoted_file.exists()

    def test_integrity_event_after_promotion_rolls_back(self, verifier):
        head_sha = _init_instance_repo(
            verifier.instance_repo, {"nanobot/runtime/existence_index.py": "X = 1\n"}
        )
        _write_candidate(
            verifier.state_dir, "promotion-runtime-pr2",
            ["nanobot/runtime/existence_index.py"], head_sha,
        )
        for _ in range(4):
            verifier.verify_pass()

        from nanobot.runtime import cycle_ledger
        cycle_ledger.append_event(verifier.state_dir, {"phase": "integrity", "reason": "test-tamper-2"})

        s = verifier.verify_pass()
        assert s["rolled_back"] == 1


class TestOperatorVeto:
    def test_vetoed_manifest_entry_rolls_back(self, verifier):
        head_sha = _init_instance_repo(
            verifier.instance_repo, {"nanobot/runtime/existence_index.py": "X = 1\n"}
        )
        _write_candidate(
            verifier.state_dir, "promotion-runtime-veto",
            ["nanobot/runtime/existence_index.py"], head_sha,
        )
        for _ in range(4):
            verifier.verify_pass()

        manifest_path = verifier.promoted_tree / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["nanobot/runtime/existence_index.py"]["status"] = "vetoed"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        s = verifier.verify_pass()
        assert s["rolled_back"] == 1
        manifest = json.loads(manifest_path.read_text())
        assert "nanobot/runtime/existence_index.py" not in manifest
        assert not (verifier.promoted_tree / "nanobot__runtime__existence_index.py").exists()


class TestPassIsIdempotentAndFailSafe:
    def test_empty_state_is_a_clean_noop(self, verifier):
        s = verifier.verify_pass()
        assert s == {
            "processed": 0, "rejected": 0, "soaking": 0,
            "promoted": 0, "rolled_back": 0, "errors": 0,
        }

    def test_missing_promotions_dir_is_a_clean_noop(self, verifier):
        import shutil
        shutil.rmtree(verifier.state_dir / "promotions")
        s = verifier.verify_pass()
        assert s["processed"] == 0 and s["errors"] == 0

    def test_promoted_tree_created_and_chmodded(self, verifier):
        verifier.verify_pass()
        assert verifier.promoted_tree.is_dir()


class TestMainEntrypoint:
    def test_main_returns_zero_on_clean_pass(self, verifier, capsys):
        rc = verifier.main([])
        assert rc == 0

    def test_main_json_flag_prints_json_summary(self, verifier, capsys):
        rc = verifier.main(["--json"])
        assert rc == 0
        out = capsys.readouterr().out.strip()
        parsed = json.loads(out)
        assert set(parsed) == {"processed", "rejected", "soaking", "promoted", "rolled_back", "errors"}


# ─── RED1: fail-closed release-ownership check ──────────────────────────────


class TestOwnershipFailClosed:
    def _minimal_env(self, tmp_path, release_dir):
        return {
            "STATE_DIR": str(tmp_path / "state"),
            "INSTANCE_REPO": str(tmp_path / "instance_repo"),
            "PROMOTED_TREE": str(tmp_path / "promoted"),
            "SELFEVO_RELEASE_DIR": str(release_dir),
        }

    def test_ownership_check_exits_nonzero_without_skip_flag(self, tmp_path, monkeypatch):
        """No EEEPC_VERIFIER_SKIP_OWNERSHIP_CHECK set: importing the module
        against a real (temp) release dir must fail closed — SystemExit
        with a nonzero code, and it must happen BEFORE `from nanobot...`
        ever runs (a plain temp dir has no `nanobot/` subdir at all, so if
        the ownership check were skipped the subsequent import would raise
        ModuleNotFoundError instead — this test would then fail with the
        WRONG exception, which is itself a useful signal the check didn't
        fire)."""
        monkeypatch.delenv("EEEPC_VERIFIER_SKIP_OWNERSHIP_CHECK", raising=False)
        for key, value in self._minimal_env(tmp_path, _REPO_ROOT).items():
            monkeypatch.setenv(key, value)

        mod_name = f"eeepc_promotion_verifier_ownercheck_{uuid.uuid4().hex}"
        spec = importlib.util.spec_from_file_location(mod_name, _VERIFIER_PATH)
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        try:
            with pytest.raises(SystemExit) as exc_info:
                spec.loader.exec_module(module)
            assert exc_info.value.code != 0
        finally:
            sys.modules.pop(mod_name, None)

    def test_ownership_check_skipped_when_flag_set(self, tmp_path, monkeypatch):
        """The escape hatch actually works — with the flag set, the module
        loads all the way through (this is what the ``verifier`` fixture
        relies on for every other test in this file)."""
        monkeypatch.setenv("EEEPC_VERIFIER_SKIP_OWNERSHIP_CHECK", "1")
        for key, value in self._minimal_env(tmp_path, _REPO_ROOT).items():
            monkeypatch.setenv(key, value)

        mod_name = f"eeepc_promotion_verifier_ownercheck_{uuid.uuid4().hex}"
        spec = importlib.util.spec_from_file_location(mod_name, _VERIFIER_PATH)
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        try:
            spec.loader.exec_module(module)  # must not raise
            assert module.verify_pass is not None
        finally:
            sys.modules.pop(mod_name, None)

    @pytest.mark.skipif(os.name != "posix", reason="root-ownership stat semantics are POSIX-only")
    def test_ownership_check_rejects_non_root_owned_release(self, tmp_path, monkeypatch):
        """On POSIX, a release dir that genuinely exists (with a nanobot/
        runtime/ subtree) but is owned by the current (non-root) test user
        must be refused with a uid-mismatch reason, not just because the
        check is platform-gated."""
        release = tmp_path / "release"
        (release / "nanobot" / "runtime").mkdir(parents=True)
        monkeypatch.delenv("EEEPC_VERIFIER_SKIP_OWNERSHIP_CHECK", raising=False)
        for key, value in self._minimal_env(tmp_path, release).items():
            monkeypatch.setenv(key, value)

        mod_name = f"eeepc_promotion_verifier_ownercheck_{uuid.uuid4().hex}"
        spec = importlib.util.spec_from_file_location(mod_name, _VERIFIER_PATH)
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        try:
            with pytest.raises(SystemExit) as exc_info:
                spec.loader.exec_module(module)
            assert exc_info.value.code != 0
        finally:
            sys.modules.pop(mod_name, None)


# ─── RED2: demoted child — real subprocess, failure modes, demotion helpers ─


class TestDemotedChildRealSubprocess:
    def test_child_verify_heldout_real_subprocess_json_round_trip(self, verifier):
        """Exercises the REAL ``_run_child_verify`` (not the fixture's
        stub) end to end: it spawns an actual ``sys.executable`` subprocess
        re-running this script with ``--child-verify --mode heldout``,
        which re-executes the ownership check + nanobot imports + a real
        (unmocked) ``run_heldout`` call, and must hand back one well-formed
        JSON object."""
        verifier.instance_repo.mkdir(parents=True, exist_ok=True)
        result = verifier._real_run_child_verify("heldout")
        assert result.get("ok") is True
        assert isinstance(result.get("heldout_clean"), bool)

    def test_run_child_verify_nonzero_exit_is_not_ok(self, verifier, monkeypatch):
        class _FakeProc:
            returncode = 3
            stdout = b""
            stderr = b"boom"

        monkeypatch.setattr(verifier.subprocess, "run", lambda *a, **kw: _FakeProc())
        result = verifier._real_run_child_verify("heldout")
        assert result["ok"] is False
        assert "exited 3" in result["reason"]

    def test_run_child_verify_unparseable_stdout_is_not_ok(self, verifier, monkeypatch):
        class _FakeProc:
            returncode = 0
            stdout = b"not json"
            stderr = b""

        monkeypatch.setattr(verifier.subprocess, "run", lambda *a, **kw: _FakeProc())
        result = verifier._real_run_child_verify("heldout")
        assert result["ok"] is False
        assert "unparseable" in result["reason"]

    def test_child_verify_failure_rejects_candidate_not_promoted(self, verifier):
        """A malformed child outcome (nonzero exit / unparseable stdout —
        simulated here via the ``_run_child_verify`` seam) must reject the
        candidate, never promote it — fail closed."""
        head_sha = _init_instance_repo(
            verifier.instance_repo, {"nanobot/runtime/existence_index.py": "X = 1\n"}
        )
        _write_candidate(
            verifier.state_dir, "promotion-runtime-childfail",
            ["nanobot/runtime/existence_index.py"], head_sha,
        )
        verifier._run_child_verify = lambda mode, **kw: {
            "ok": False, "reason": "child verify process exited 1: boom",
        }
        s = verifier.verify_pass()
        assert s["rejected"] == 1
        assert s["promoted"] == 0
        vs = json.loads((verifier.promoted_tree / "verifier_state.json").read_text())
        assert "child verify" in vs["candidates"]["promotion-runtime-childfail"]["reason"]
        assert not (verifier.promoted_tree / "manifest.json").exists() or json.loads(
            (verifier.promoted_tree / "manifest.json").read_text()
        ) == {"_schema_version": "promoted-manifest-v1"}


class TestDemotionHelpers:
    def test_is_root_returns_a_bool(self, verifier):
        assert isinstance(verifier._is_root(), bool)

    def test_is_root_false_on_non_posix(self, verifier):
        if os.name != "posix":
            assert verifier._is_root() is False

    @pytest.mark.skipif(os.name != "posix", reason="pwd is POSIX-only")
    def test_resolve_demote_ids_for_current_user(self, verifier):
        import pwd

        current_user = pwd.getpwuid(os.getuid()).pw_name
        ids = verifier._resolve_demote_ids(current_user)
        assert ids == (os.getuid(), os.getgid())

    def test_resolve_demote_ids_unknown_user_returns_none(self, verifier):
        if os.name != "posix":
            assert verifier._resolve_demote_ids("eeepc-agent") is None
        else:
            assert verifier._resolve_demote_ids("no-such-user-xyz-123-does-not-exist") is None

    @pytest.mark.skipif(os.name != "posix", reason="setuid/setgid are POSIX-only")
    def test_demote_preexec_fn_is_callable_and_built_lazily(self, verifier):
        # Never actually invoked (that would require real root) — this only
        # verifies the factory produces a plain no-arg callable, matching
        # what subprocess.run's preexec_fn contract requires.
        fn = verifier._demote_preexec_fn(os.getuid(), os.getgid())
        assert callable(fn)

    def test_demote_kwargs_skips_demotion_when_not_root(self, verifier):
        if verifier._is_root():
            pytest.skip("test runner is root — cannot exercise the non-root branch")
        kwargs, demoted = verifier._demote_kwargs()
        assert demoted is False
        assert kwargs == {}
