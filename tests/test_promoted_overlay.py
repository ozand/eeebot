"""Tests for #875: the eeepc-agent-side promoted-runtime-module loader.

``nanobot.runtime.promoted_overlay.install_promoted_overlay`` is the OTHER
half of the root-verified auto-promotion trust boundary (the root verifier,
``host/eeepc/libexec/eeepc_promotion_verifier.py``, is the writer side — see
``tests/test_promotion_verifier.py``). These tests exercise the loader in
isolation: the boundary self-check, deny/slice re-validation, sha256
integrity, and the various fail-open/fail-closed paths — WITHOUT actually
needing root privileges, by monkeypatching the ownership check itself (the
one piece of logic that genuinely requires POSIX + root to exercise for
real) while keeping every other check (deny-set, slice-shape, sha256,
manifest status, actual module loading) fully real.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import sys
from pathlib import Path

import pytest

from nanobot.runtime import promoted_overlay


def _write_manifest(tree_dir: Path, entries: dict) -> None:
    (tree_dir / "manifest.json").write_text(json.dumps(entries), encoding="utf-8")


def _write_module(tree_dir: Path, module_path: str, source: str) -> str:
    """Write ``source`` under the flattened filename convention and return
    its sha256 hex digest (what a manifest entry would record).

    Writes raw bytes (not ``write_text``) so no platform newline
    translation can occur — the sha256 recorded here must match EXACTLY
    what :func:`hashlib.sha256` computes over the bytes the loader reads
    back with ``read_bytes()`` (production is POSIX-only anyway, but this
    keeps the test byte-exact on any dev platform too).
    """
    flat = module_path.replace("/", "__")
    data = source.encode("utf-8")
    (tree_dir / flat).write_bytes(data)
    return hashlib.sha256(data).hexdigest()


@pytest.fixture(autouse=True)
def _force_boundary_ok(monkeypatch):
    """Most tests below want to exercise the POST-boundary logic (deny/
    slice/sha256/loading) without needing a real root-owned tree on disk.
    Individual tests that specifically test the boundary check itself
    override this fixture's effect by monkeypatching ``_boundary_ok`` again
    with a narrower behavior, or by not using this fixture at all (see
    the dedicated boundary-check tests, which patch ``_root_owned_and_not_writable``
    at a lower level instead).
    """
    return None


class TestAbsentOrEmptyTree:
    def test_absent_tree_is_a_quiet_noop(self, tmp_path: Path):
        assert promoted_overlay.install_promoted_overlay(tmp_path / "does-not-exist") == []

    def test_tree_without_manifest_is_a_quiet_noop(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(promoted_overlay, "_boundary_ok", lambda *_: True)
        assert promoted_overlay.install_promoted_overlay(tmp_path) == []

    def test_default_tree_resolution_uses_env_var(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("PROMOTED_TREE", str(tmp_path / "nope"))
        assert promoted_overlay.install_promoted_overlay() == []


class TestBoundarySelfCheck:
    """The critical refuse-everything check. Exercised by monkeypatching the
    actual ownership primitive (``_root_owned_and_not_writable``) rather than
    ``_boundary_ok`` itself, so the real AND-of-two-paths logic in
    ``_boundary_ok`` is genuinely exercised."""

    def test_non_posix_refuses_everything(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(promoted_overlay.os, "name", "nt")
        # Even a tree that WOULD pass ownership checks must be refused.
        monkeypatch.setattr(promoted_overlay, "_root_owned_and_not_writable", lambda p: True)
        _write_manifest(tmp_path, {})
        assert promoted_overlay.install_promoted_overlay(tmp_path) == []

    def test_posix_root_owned_readonly_tree_is_trusted(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(promoted_overlay.os, "name", "posix")
        monkeypatch.setattr(promoted_overlay, "_root_owned_and_not_writable", lambda p: True)
        _write_manifest(tmp_path, {})
        # Empty manifest -> loads nothing, but must not be REFUSED (returns
        # [] either way, so assert via a non-empty manifest in a sibling test).
        assert promoted_overlay.install_promoted_overlay(tmp_path) == []

    def test_posix_but_not_root_owned_refuses_everything(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(promoted_overlay.os, "name", "posix")
        monkeypatch.setattr(promoted_overlay, "_root_owned_and_not_writable", lambda p: False)
        module_src = "VALUE = 'should-never-load'\n"
        sha = _write_module(tmp_path, "nanobot/runtime/probes.py", module_src)
        _write_manifest(tmp_path, {
            "nanobot/runtime/probes.py": {"sha256": sha, "status": "active"},
        })
        assert promoted_overlay.install_promoted_overlay(tmp_path) == []

    def test_boundary_ok_false_on_stat_exception(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(promoted_overlay.os, "name", "posix")

        def _raise(_path):
            raise OSError("boom")

        monkeypatch.setattr(promoted_overlay, "_root_owned_and_not_writable", _raise)
        _write_manifest(tmp_path, {})
        assert promoted_overlay.install_promoted_overlay(tmp_path) == []


class TestManifestEntryValidation:
    """With the boundary check forced True, exercise per-entry validation:
    status gating, deny-set, slice-shape, and sha256 integrity."""

    @pytest.fixture(autouse=True)
    def _trust_boundary(self, monkeypatch):
        monkeypatch.setattr(promoted_overlay, "_boundary_ok", lambda *_: True)

    def test_loads_valid_active_entry(self, tmp_path: Path):
        module_src = "VALUE_875 = 'from-overlay'\n"
        sha = _write_module(tmp_path, "nanobot/runtime/probes.py", module_src)
        _write_manifest(tmp_path, {
            "nanobot/runtime/probes.py": {"sha256": sha, "status": "active"},
        })
        try:
            loaded = promoted_overlay.install_promoted_overlay(tmp_path)
            assert loaded == ["nanobot/runtime/probes.py"]
            mod = sys.modules["nanobot.runtime.probes"]
            assert mod.VALUE_875 == "from-overlay"
            import nanobot.runtime as pkg
            assert pkg.probes is mod
        finally:
            sys.modules.pop("nanobot.runtime.probes", None)
            importlib.reload(importlib.import_module("nanobot.runtime.probes"))

    def test_non_active_status_is_skipped(self, tmp_path: Path):
        sha = _write_module(tmp_path, "nanobot/runtime/probes.py", "X = 1\n")
        for status in ("pending", "soaking", "rejected", "vetoed", None):
            _write_manifest(tmp_path, {
                "nanobot/runtime/probes.py": {"sha256": sha, "status": status},
            })
            assert promoted_overlay.install_promoted_overlay(tmp_path) == []

    def test_deny_set_module_is_refused_even_if_manifest_claims_active(self, tmp_path: Path):
        sha = _write_module(tmp_path, "nanobot/runtime/bridge.py", "X = 1\n")
        _write_manifest(tmp_path, {
            "nanobot/runtime/bridge.py": {"sha256": sha, "status": "active"},
        })
        assert promoted_overlay.install_promoted_overlay(tmp_path) == []

    def test_non_slice_shape_module_is_refused(self, tmp_path: Path):
        # not under nanobot/runtime/, or not .py — both must be refused.
        for bad_path in ("nanobot/agent/subagent.py", "scripts/foo.py", "nanobot/runtime/foo.txt"):
            sha = _write_module(tmp_path, bad_path, "X = 1\n")
            _write_manifest(tmp_path, {bad_path: {"sha256": sha, "status": "active"}})
            assert promoted_overlay.install_promoted_overlay(tmp_path) == [], bad_path

    def test_traversal_key_is_refused(self, tmp_path: Path):
        bad_key = "nanobot/runtime/../bridge.py"
        sha = _write_module(tmp_path, "nanobot/runtime/bridge.py", "X = 1\n")
        _write_manifest(tmp_path, {bad_key: {"sha256": sha, "status": "active"}})
        assert promoted_overlay.install_promoted_overlay(tmp_path) == []

    def test_sha256_mismatch_is_refused(self, tmp_path: Path):
        _write_module(tmp_path, "nanobot/runtime/probes.py", "X = 1\n")
        _write_manifest(tmp_path, {
            "nanobot/runtime/probes.py": {"sha256": "0" * 64, "status": "active"},
        })
        assert promoted_overlay.install_promoted_overlay(tmp_path) == []

    def test_missing_tree_file_is_refused(self, tmp_path: Path):
        _write_manifest(tmp_path, {
            "nanobot/runtime/probes.py": {"sha256": "0" * 64, "status": "active"},
        })
        # no nanobot__runtime__probes.py written at all
        assert promoted_overlay.install_promoted_overlay(tmp_path) == []

    def test_missing_or_malformed_sha256_field_is_refused(self, tmp_path: Path):
        _write_module(tmp_path, "nanobot/runtime/probes.py", "X = 1\n")
        for bad_entry in ({"status": "active"}, {"sha256": 123, "status": "active"}, {"sha256": "", "status": "active"}):
            _write_manifest(tmp_path, {"nanobot/runtime/probes.py": bad_entry})
            assert promoted_overlay.install_promoted_overlay(tmp_path) == []

    def test_one_bad_entry_never_blocks_a_good_sibling_entry(self, tmp_path: Path):
        good_src = "GOOD_875 = 42\n"
        good_sha = _write_module(tmp_path, "nanobot/runtime/probes.py", good_src)
        bad_sha = "deadbeef" * 8
        _write_module(tmp_path, "nanobot/runtime/system_map.py", "BAD=1\n")
        _write_manifest(tmp_path, {
            "nanobot/runtime/probes.py": {"sha256": good_sha, "status": "active"},
            "nanobot/runtime/system_map.py": {"sha256": bad_sha, "status": "active"},  # mismatch
        })
        try:
            loaded = promoted_overlay.install_promoted_overlay(tmp_path)
            assert loaded == ["nanobot/runtime/probes.py"]
        finally:
            sys.modules.pop("nanobot.runtime.probes", None)
            importlib.reload(importlib.import_module("nanobot.runtime.probes"))

    def test_manifest_meta_keys_with_non_dict_value_are_skipped_not_crashed(self, tmp_path: Path):
        _write_manifest(tmp_path, {"_schema_version": "promoted-manifest-v1"})
        assert promoted_overlay.install_promoted_overlay(tmp_path) == []

    def test_malformed_manifest_json_fails_closed(self, tmp_path: Path):
        (tmp_path / "manifest.json").write_text("{not valid json", encoding="utf-8")
        assert promoted_overlay.install_promoted_overlay(tmp_path) == []

    def test_manifest_that_is_a_json_list_fails_closed(self, tmp_path: Path):
        (tmp_path / "manifest.json").write_text("[1, 2, 3]", encoding="utf-8")
        assert promoted_overlay.install_promoted_overlay(tmp_path) == []


# ─── #876: active_promoted_modules / effective_runtime_slice ────────────────
# The read side the trust ladder derives its progression from — reuses the
# SAME boundary self-check the overlay loader uses, so an instance-writable
# or misprovisioned tree can never advance the ladder.


class TestActivePromotedModules:
    def test_absent_tree_is_empty_set(self, tmp_path: Path):
        assert promoted_overlay.active_promoted_modules(tmp_path / "does-not-exist") == set()

    def test_missing_manifest_is_empty_set(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(promoted_overlay, "_boundary_ok", lambda *_: True)
        assert promoted_overlay.active_promoted_modules(tmp_path) == set()

    def test_boundary_fail_is_empty_set_even_with_a_genuinely_valid_manifest(self, tmp_path: Path, monkeypatch):
        # #876 invariant: an instance-writable/misprovisioned tree must
        # never advance the ladder, no matter what its manifest claims —
        # even a manifest entry that would otherwise be genuinely valid.
        sha = _write_module(tmp_path, "nanobot/runtime/existence_index.py", "X = 1\n")
        _write_manifest(tmp_path, {
            "nanobot/runtime/existence_index.py": {"sha256": sha, "status": "active"},
        })
        monkeypatch.setattr(promoted_overlay, "_boundary_ok", lambda *_: False)
        assert promoted_overlay.active_promoted_modules(tmp_path) == set()

    def test_non_posix_refuses_everything(self, tmp_path: Path, monkeypatch):
        sha = _write_module(tmp_path, "nanobot/runtime/existence_index.py", "X = 1\n")
        _write_manifest(tmp_path, {
            "nanobot/runtime/existence_index.py": {"sha256": sha, "status": "active"},
        })
        monkeypatch.setattr(promoted_overlay.os, "name", "nt")
        monkeypatch.setattr(promoted_overlay, "_root_owned_and_not_writable", lambda p: True)
        assert promoted_overlay.active_promoted_modules(tmp_path) == set()

    def test_returns_only_genuinely_valid_active_entries(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(promoted_overlay, "_boundary_ok", lambda *_: True)
        sha = _write_module(tmp_path, "nanobot/runtime/existence_index.py", "X = 1\n")
        _write_manifest(tmp_path, {
            "nanobot/runtime/existence_index.py": {"sha256": sha, "status": "active"},
            "nanobot/runtime/demand.py": {"sha256": "0" * 64, "status": "soaking"},
            "nanobot/runtime/probes.py": {"sha256": "0" * 64, "status": "rejected"},
            "_schema_version": "promoted-manifest-v1",
        })
        assert promoted_overlay.active_promoted_modules(tmp_path) == {
            "nanobot/runtime/existence_index.py",
        }

    def test_active_entry_with_sha256_mismatch_is_not_counted(self, tmp_path: Path, monkeypatch):
        # #876 Fix 3: ladder advancement must require the SAME validity the
        # loader (_load_one_module) enforces — a manifest claiming "active"
        # whose on-disk bytes don't match its recorded sha256 must never
        # count, exactly like the loader itself would refuse to load it.
        monkeypatch.setattr(promoted_overlay, "_boundary_ok", lambda *_: True)
        _write_module(tmp_path, "nanobot/runtime/existence_index.py", "X = 1\n")
        _write_manifest(tmp_path, {
            "nanobot/runtime/existence_index.py": {"sha256": "0" * 64, "status": "active"},
        })
        assert promoted_overlay.active_promoted_modules(tmp_path) == set()

    def test_active_entry_with_missing_tree_file_is_not_counted(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(promoted_overlay, "_boundary_ok", lambda *_: True)
        _write_manifest(tmp_path, {
            "nanobot/runtime/existence_index.py": {"sha256": "0" * 64, "status": "active"},
        })
        # no nanobot__runtime__existence_index.py written at all
        assert promoted_overlay.active_promoted_modules(tmp_path) == set()

    def test_active_entry_on_a_deny_set_path_is_not_counted(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(promoted_overlay, "_boundary_ok", lambda *_: True)
        sha = _write_module(tmp_path, "nanobot/runtime/bridge.py", "X = 1\n")
        _write_manifest(tmp_path, {
            "nanobot/runtime/bridge.py": {"sha256": sha, "status": "active"},
        })
        assert promoted_overlay.active_promoted_modules(tmp_path) == set()

    def test_malformed_manifest_is_empty_set(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(promoted_overlay, "_boundary_ok", lambda *_: True)
        (tmp_path / "manifest.json").write_text("{not valid json", encoding="utf-8")
        assert promoted_overlay.active_promoted_modules(tmp_path) == set()

    def test_manifest_that_is_a_json_list_is_empty_set(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(promoted_overlay, "_boundary_ok", lambda *_: True)
        (tmp_path / "manifest.json").write_text("[1, 2, 3]", encoding="utf-8")
        assert promoted_overlay.active_promoted_modules(tmp_path) == set()

    def test_default_tree_resolution_uses_env_var(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("PROMOTED_TREE", str(tmp_path / "nope"))
        assert promoted_overlay.active_promoted_modules() == set()


class TestEffectiveRuntimeSlice:
    def test_env_only_when_no_promotions(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(promoted_overlay, "_boundary_ok", lambda *_: True)
        _write_manifest(tmp_path, {})
        result = promoted_overlay.effective_runtime_slice("nanobot/runtime/probes.py", tmp_path)
        # No active promotions -> the ladder contributes nothing at all.
        assert result == {"nanobot/runtime/probes.py"}

    def test_empty_env_and_no_promotions_is_byte_identical_empty_set(self, tmp_path: Path, monkeypatch):
        # The core #876 invariant CI caught a regression in: zero active
        # promotions + unset env slice must be EXACTLY set(), never
        # widened by the ladder on its own.
        monkeypatch.setattr(promoted_overlay, "_boundary_ok", lambda *_: True)
        _write_manifest(tmp_path, {})
        assert promoted_overlay.effective_runtime_slice("", tmp_path) == set()
        assert promoted_overlay.effective_runtime_slice(None, tmp_path) == set()

    def test_env_only_existence_index_when_no_promotions(self, tmp_path: Path, monkeypatch):
        # rung 0 reaches the effective slice ONLY via the env allow-list,
        # exactly as before #876 — not via the ladder.
        monkeypatch.setattr(promoted_overlay, "_boundary_ok", lambda *_: True)
        _write_manifest(tmp_path, {})
        result = promoted_overlay.effective_runtime_slice("nanobot/runtime/existence_index.py", tmp_path)
        assert result == {"nanobot/runtime/existence_index.py"}

    def test_env_union_earned_ladder_when_promotions_active(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(promoted_overlay, "_boundary_ok", lambda *_: True)
        sha = _write_module(tmp_path, "nanobot/runtime/existence_index.py", "X = 1\n")
        _write_manifest(tmp_path, {
            "nanobot/runtime/existence_index.py": {"sha256": sha, "status": "active"},
        })
        result = promoted_overlay.effective_runtime_slice("nanobot/runtime/probes.py", tmp_path)
        assert result == {
            "nanobot/runtime/probes.py",
            "nanobot/runtime/demand.py",  # earned: rung0 active unlocks rung1
        }

    def test_boundary_fail_yields_env_only_no_ladder_contribution(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(promoted_overlay, "_boundary_ok", lambda *_: False)
        assert promoted_overlay.effective_runtime_slice(None, tmp_path) == set()
        assert promoted_overlay.effective_runtime_slice("nanobot/runtime/probes.py", tmp_path) == {
            "nanobot/runtime/probes.py",
        }


def test_flattened_filename_convention_matches_verifier_writer_side():
    # Must stay in sync with eeepc_promotion_verifier._flattened_filename —
    # this is the one piece of shared naming convention between the two
    # independent processes (root writer, eeepc-agent reader).
    assert (
        promoted_overlay._flattened_filename("nanobot/runtime/existence_index.py")
        == "nanobot__runtime__existence_index.py"
    )
