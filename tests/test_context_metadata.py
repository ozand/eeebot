from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from nanobot.agent.context import ContextBuilder
from nanobot.runtime.context_metadata import (
    SCHEMA_VERSION,
    build_context_metadata,
    load_context_metadata,
    validate_context_metadata,
)
from nanobot.runtime.context_rules import (
    LOOP_GENERATED_SECTION_ORDER,
    MAX_SYSTEM_PROMPT_CHARS,
    RELEASE_BLOCK_NAMES,
    RELEASE_POOL_CHARS,
    bootstrap_files,
    loop_context_section_order,
)
from nanobot.runtime.operator_documents import PRIORITIES_BLOCK_CAP

SHA = "c7e180d7122605082afc561225051e9a2c26f7f2"


def test_builder_consumes_shared_definitions_without_changing_policy() -> None:
    assert ContextBuilder._RELEASE_BLOCK_NAMES is RELEASE_BLOCK_NAMES
    assert ContextBuilder._RELEASE_POOL_CHARS == RELEASE_POOL_CHARS == 15_500
    assert ContextBuilder.MAX_SYSTEM_PROMPT_CHARS == MAX_SYSTEM_PROMPT_CHARS == 35_000
    assert ContextBuilder.BOOTSTRAP_FILES == bootstrap_files(("AGENTS.md",))


def test_builder_order_and_metadata_follow_shared_loop_sequence() -> None:
    order = loop_context_section_order(("AGENTS.md",))
    assert order == (
        "identity", "soul", "goals", "user", "operating", "agents",
        *LOOP_GENERATED_SECTION_ORDER,
    )
    assert ContextBuilder.LOOP_CONTEXT_SECTION_ORDER == order
    expected_rule_suffixes = [
        "identity", "soul", "charter", "user", "operating", "agents",
        "priorities", "memory", "runtime", "scorecard", "position",
    ]
    assert [rule["id"].split(".", 1)[1] for rule in build_context_metadata(SHA)["rules"]] == expected_rule_suffixes


def test_metadata_is_deterministic_bound_and_rule_sourced() -> None:
    first = build_context_metadata(SHA)
    assert first == build_context_metadata(SHA)
    assert first["schema_version"] == SCHEMA_VERSION
    assert first["source_commit"] == SHA
    assert first["prompt_cap"] == {
        "default": MAX_SYSTEM_PROMPT_CHARS,
        "override_env_name": "NANOBOT_SYSTEM_PROMPT_MAX_CHARS",
        "effective": None,
    }
    assert first["release_pool"]["cap"] == RELEASE_POOL_CHARS
    assert any(rule["values"].get("cap") == PRIORITIES_BLOCK_CAP for rule in first["rules"])
    assert [rule["id"] for rule in first["rules"][:5]] == [
        "release.identity", "release.soul", "release.charter", "release.user", "release.operating",
    ]


def test_metadata_rejects_invalid_commit_schema_and_unknown_fields() -> None:
    with pytest.raises(ValueError, match="full lowercase"):
        build_context_metadata("HEAD")
    metadata = build_context_metadata(SHA)
    with pytest.raises(ValueError, match="does not match"):
        validate_context_metadata(metadata, "0" * 40)
    malformed = dict(metadata, rogue="field")
    with pytest.raises(ValueError, match="shape"):
        validate_context_metadata(malformed, SHA)
    unsupported = dict(metadata, schema_version=True)
    with pytest.raises(ValueError, match="schema"):
        validate_context_metadata(unsupported, SHA)


def test_metadata_rejects_arbitrary_paths_and_bad_numeric_values() -> None:
    metadata = build_context_metadata(SHA)
    changed = json.loads(json.dumps(metadata))
    changed["rules"][0]["values"]["path"] = "/private/OPERATING.md"
    with pytest.raises(ValueError, match="rule values"):
        validate_context_metadata(changed, SHA)
    changed = json.loads(json.dumps(metadata))
    changed["rules"][0]["values"]["floor"] = True
    with pytest.raises(ValueError, match="rule value type"):
        validate_context_metadata(changed, SHA)


def test_artifact_loader_is_bounded_and_validates_release_binding(tmp_path: Path) -> None:
    artifact = tmp_path / "context-metadata.json"
    artifact.write_text(json.dumps(build_context_metadata(SHA)), encoding="utf-8")
    assert load_context_metadata(str(artifact), SHA)["source_commit"] == SHA
    with pytest.raises(ValueError, match="does not match"):
        load_context_metadata(str(artifact), "0" * 40)
    artifact.write_bytes(b" " * 8193)
    with pytest.raises(ValueError, match="size limit"):
        load_context_metadata(str(artifact), SHA)


def test_exporter_import_closure_is_side_effect_bounded() -> None:
    code = """
import sys
from nanobot.runtime.context_metadata import build_context_metadata
metadata = build_context_metadata('c7e180d7122605082afc561225051e9a2c26f7f2')
assert metadata['schema_version'] == 1
for name in ('loguru', 'nanobot.agent.memory', 'nanobot.agent.skills'):
    assert name not in sys.modules, name
"""
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    subprocess.run([sys.executable, "-c", code], check=True, env=env)


def test_exporter_cli_import_closure_uses_only_staged_metadata_modules(tmp_path: Path) -> None:
    import shutil

    repo = Path(__file__).resolve().parents[1]
    staged = tmp_path / "candidate"
    for relative in (
        "scripts/export_context_metadata.py",
        "nanobot/runtime/context_metadata.py",
        "nanobot/runtime/context_rules.py",
        "nanobot/runtime/mutation_policy.py",
        "nanobot/runtime/operator_documents.py",
    ):
        target = staged / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(repo / relative, target)
    # A conflicting ambient package must not satisfy imports for the staged tree.
    ambient = tmp_path / "ambient"
    sentinel = ambient / "nanobot/runtime/context_metadata.py"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_text("raise AssertionError('ambient metadata imported')\n", encoding="utf-8")
    output = tmp_path / "context-metadata.json"
    source_sha = "a" * 40
    wrapper = f"""
import runpy, sys
sys.argv = [{str(staged / 'scripts/export_context_metadata.py')!r}, '--source-commit', {source_sha!r}, '--output', {str(output)!r}]
try:
    runpy.run_path(sys.argv[0], run_name='__main__')
except SystemExit as exc:
    assert exc.code == 0, exc.code
expected = {{'nanobot.runtime.context_rules', 'nanobot.runtime.mutation_policy', 'nanobot.runtime.operator_documents', 'nanobot.runtime.context_metadata'}}
loaded = {{name for name in sys.modules if name.startswith('nanobot.runtime.')}}
assert loaded == expected, loaded
for name in ('nanobot.runtime.local_ci', 'nanobot.runtime.state', 'nanobot.runtime.state_access', 'nanobot.runtime.day_key', 'loguru'):
    assert name not in sys.modules, name
for name in expected:
    assert sys.modules[name].__file__.startswith({str(staged)!r}), (name, sys.modules[name].__file__)
assert all(sys.modules[name].__file__.startswith({str(staged)!r}) for name in expected)
"""
    env = dict(os.environ, PYTHONPATH=str(ambient), PYTHONDONTWRITEBYTECODE="1")
    subprocess.run([sys.executable, "-c", wrapper], cwd=tmp_path, env=env, check=True)
    assert json.loads(output.read_text(encoding="utf-8"))["source_commit"] == source_sha
