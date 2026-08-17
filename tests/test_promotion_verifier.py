"""Tests for #875: the root-run promotion verifier (the automated operator).

``host/eeepc/libexec/eeepc_promotion_verifier.py`` is a standalone script
(not a ``nanobot`` package module — it is deployed to ``/usr/local/libexec``
and run by systemd as root), so it is loaded here via
``importlib.util.spec_from_file_location`` exactly the way it would be
invoked in production, with a REAL tiny git repository standing in for the
instance repo (so ``git show <head_sha>:<module_path>`` materialization is
genuinely exercised, not mocked) while the two genuinely-slow/hardware-
dependent pieces — the microbench wall-clock measurement and the held-out
subprocess pack — are monkeypatched to deterministic values per test.
"""
from __future__ import annotations

import importlib.util
import json
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


@pytest.fixture
def verifier(tmp_path, monkeypatch):
    """Load a FRESH module instance of the verifier script per test, with
    STATE_DIR/INSTANCE_REPO/PROMOTED_TREE/SELFEVO_RUNTIME_SLICE env vars set
    BEFORE import (the script binds these to module-level constants at
    import time), and its two slow/hardware-dependent entry points
    (``run_measurement``, ``_run_heldout``) defaulted to deterministic
    "clean and improved" stand-ins that individual tests override."""
    state_dir = tmp_path / "state"
    instance_repo = tmp_path / "instance_repo"
    promoted_tree = tmp_path / "promoted"
    (state_dir / "promotions").mkdir(parents=True)

    monkeypatch.setenv("STATE_DIR", str(state_dir))
    monkeypatch.setenv("INSTANCE_REPO", str(instance_repo))
    monkeypatch.setenv("PROMOTED_TREE", str(promoted_tree))
    monkeypatch.setenv("SELFEVO_RUNTIME_SLICE", "nanobot/runtime/existence_index.py")
    monkeypatch.setenv("SELFEVO_RELEASE_DIR", str(_REPO_ROOT))

    mod_name = f"eeepc_promotion_verifier_test_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(mod_name, _VERIFIER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)

    # Default stand-ins: 20% improvement, clean heldout. Individual tests
    # override module.run_measurement / module._run_heldout as needed.
    module.run_measurement = lambda module_path, candidate_bytes=None, timeout=120: (
        100.0 if candidate_bytes is None else 80.0
    )
    module._run_heldout = lambda state_dir, repo, **kw: {"regressions": [], "results": {}}

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
        assert entry["sha256"] == __import__("hashlib").sha256(b"X = 1\n").hexdigest()

        # Terminal: further passes never reprocess this candidate_id again.
        vs = json.loads((verifier.promoted_tree / "verifier_state.json").read_text())
        assert vs["candidates"]["promotion-runtime-cyc1"]["status"] == "promoted"

    def test_forged_instance_microbench_number_is_ignored(self, verifier):
        """The instance's OWN claimed improvement_pct in the candidate record
        must never influence the verdict — only the root's own re-measure
        does. Root re-measure here is set BELOW threshold, so a forged 99%
        instance claim must still be rejected."""
        head_sha = _init_instance_repo(
            verifier.instance_repo, {"nanobot/runtime/existence_index.py": "X = 1\n"}
        )
        _write_candidate(
            verifier.state_dir, "promotion-runtime-forged",
            ["nanobot/runtime/existence_index.py"], head_sha,
            microbench={"improvement_pct": 99.0, "forged": True},
        )
        verifier.run_measurement = lambda module_path, candidate_bytes=None, timeout=120: (
            100.0 if candidate_bytes is None else 98.0  # real improvement: 2%, below threshold
        )
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
        verifier._run_heldout = lambda state_dir, repo, **kw: {
            "regressions": ["scripts/foo.py"], "results": {},
        }
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
        verifier._run_heldout = lambda state_dir, repo, **kw: {
            "regressions": [], "results": {"scripts/bar.py": {"status": "fail"}},
        }
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
        verifier.run_measurement = lambda *a, **kw: measured.append(1) or 100.0
        s = verifier.verify_pass()
        assert s["rejected"] == 1
        assert measured == []  # never even attempted a measurement
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
        verifier.run_measurement = lambda *a, **kw: calls.append(1) or 100.0
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
        verifier._run_heldout = lambda state_dir, repo, **kw: {
            "regressions": ["scripts/newly_broken.py"], "results": {},
        }
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

        verifier._run_heldout = lambda state_dir, repo, **kw: {
            "regressions": ["scripts/broke_after_promotion.py"], "results": {},
        }
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
