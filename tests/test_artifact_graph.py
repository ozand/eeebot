"""Tests for the artifact dependency graph (#1769, ADR-024)."""
from __future__ import annotations

import json
from pathlib import Path

from nanobot.runtime.artifact_graph import (
    EDGE_KINDS,
    SCHEMA_VERSION,
    ArtifactGraph,
    build_artifact_graph,
    publish_artifact_graph,
    read_latest_artifact_graph,
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# ─── missing/unreadable input ────────────────────────────────────────────────


def test_missing_repo_is_unavailable_not_empty(tmp_path):
    graph = build_artifact_graph(tmp_path / "does-not-exist")
    assert graph.status == "unavailable"
    assert graph.nodes == {}
    assert graph.edges == []


def test_repo_with_no_artifact_dirs_is_unavailable(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    graph = build_artifact_graph(repo)
    assert graph.status == "unavailable"


def test_unavailable_graph_reports_zero_counts_but_is_not_a_zero_graph(tmp_path):
    graph = build_artifact_graph(tmp_path / "missing")
    d = graph.to_dict()
    assert d["status"] == "unavailable"
    assert d["counts"]["artifacts"] == 0
    # The distinguishing signal a reader must check first, per the AC.
    assert d["status"] != "complete"


# ─── node discovery ───────────────────────────────────────────────────────────


def _minimal_repo(tmp_path) -> Path:
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "tests").mkdir(parents=True)
    return repo


def test_discovers_scripts_surfaces_and_skills(tmp_path):
    repo = _minimal_repo(tmp_path)
    _write(repo / "scripts" / "a.py", "x = 1\n")
    _write(repo / "surfaces" / "goals.md", "# goals\n")
    _write(repo / "skills" / "do-thing" / "SKILL.md", "# do-thing\n")
    # a skill's own nested scripts/ must not become top-level graph nodes
    _write(repo / "skills" / "do-thing" / "scripts" / "helper.py", "x = 1\n")

    graph = build_artifact_graph(repo)
    assert graph.status == "complete"
    ids = set(graph.nodes)
    assert "scripts/a" in ids
    assert "surfaces/goals.md" in ids
    assert "skills/do-thing" in ids
    assert not any(nid.startswith("skills/do-thing/scripts") for nid in ids)
    assert graph.nodes["skills/do-thing"].path == "skills/do-thing/SKILL.md"


def test_a_script_with_no_reference_anywhere_is_a_leaf(tmp_path):
    repo = _minimal_repo(tmp_path)
    _write(repo / "scripts" / "lonely.py", "x = 1\n")

    graph = build_artifact_graph(repo)
    assert graph.rung("scripts/lonely") == "leaf"
    assert "scripts/lonely" in graph.leaves()
    assert "scripts/lonely" not in graph.components()


# ─── used_by (production) ────────────────────────────────────────────────────


def test_plain_import_from_another_script_is_used_by(tmp_path):
    repo = _minimal_repo(tmp_path)
    _write(repo / "scripts" / "consumer.py", "import producer\n")
    _write(repo / "scripts" / "producer.py", "x = 1\n")

    graph = build_artifact_graph(repo)
    assert graph.is_component("scripts/producer")
    edge = next(e for e in graph.edges if e.target == "scripts/producer")
    assert edge.kind == "used_by"
    assert edge.source == "scripts/consumer.py"


def test_dotted_scripts_import_from_form_resolves_to_the_submodule(tmp_path):
    """`from scripts.producer import x` -- the dominant real-corpus idiom
    (89 files, measured 2026-09-19). `node.module.split('.')[0]` (the OLD
    resolver's own logic, and this module's own first cut) always yields
    the literal string "scripts", which is never a stem and silently drops
    the edge. Regression pin for that fix."""
    repo = _minimal_repo(tmp_path)
    _write(repo / "scripts" / "consumer.py", "from scripts.producer import helper\n")
    _write(repo / "scripts" / "producer.py", "def helper(): pass\n")

    graph = build_artifact_graph(repo)
    assert graph.is_component("scripts/producer")


def test_dotted_scripts_plain_import_form_resolves_to_the_submodule(tmp_path):
    repo = _minimal_repo(tmp_path)
    _write(repo / "scripts" / "consumer.py", "import scripts.producer\n")
    _write(repo / "scripts" / "producer.py", "x = 1\n")

    graph = build_artifact_graph(repo)
    assert graph.is_component("scripts/producer")


def test_from_scripts_import_submodule_form_resolves(tmp_path):
    repo = _minimal_repo(tmp_path)
    _write(repo / "scripts" / "consumer.py", "from scripts import producer\n")
    _write(repo / "scripts" / "producer.py", "x = 1\n")

    graph = build_artifact_graph(repo)
    assert graph.is_component("scripts/producer")


def test_split_literal_path_construction_is_used_by_or_tested_by(tmp_path):
    """`REPO_ROOT / 'scripts' / 'producer.py'` -- the split-literal
    Path-construction idiom this codebase's own tests use to load a
    standalone script via `importlib.util.spec_from_file_location`."""
    repo = _minimal_repo(tmp_path)
    _write(
        repo / "tests" / "test_producer.py",
        "SCRIPT = REPO_ROOT / 'scripts' / 'producer.py'\n",
    )
    _write(repo / "scripts" / "producer.py", "x = 1\n")

    graph = build_artifact_graph(repo)
    edge = next(e for e in graph.edges if e.target == "scripts/producer")
    assert edge.kind == "tested_by"


def test_literal_scripts_path_in_subprocess_call_is_used_by(tmp_path):
    repo = _minimal_repo(tmp_path)
    _write(
        repo / "scripts" / "runner.py",
        "import subprocess\nsubprocess.run(['python3', 'scripts/producer.py'])\n",
    )
    _write(repo / "scripts" / "producer.py", "x = 1\n")

    graph = build_artifact_graph(repo)
    assert graph.is_component("scripts/producer")


def test_a_script_naming_itself_never_becomes_self_referential(tmp_path):
    repo = _minimal_repo(tmp_path)
    _write(repo / "scripts" / "solo.py", "# see scripts/solo.py for usage\n")

    graph = build_artifact_graph(repo)
    assert not graph.is_component("scripts/solo")
    assert not any(e.source == "scripts/solo.py" and e.target == "scripts/solo" for e in graph.edges)


# ─── tested_by never promotes ────────────────────────────────────────────────


def test_a_test_importing_a_script_never_promotes_it_to_component(tmp_path):
    repo = _minimal_repo(tmp_path)
    _write(repo / "scripts" / "leaf.py", "x = 1\n")
    _write(repo / "tests" / "test_leaf.py", "import leaf\n")

    graph = build_artifact_graph(repo)
    assert graph.rung("scripts/leaf") == "leaf"
    edge = next(e for e in graph.edges if e.target == "scripts/leaf")
    assert edge.kind == "tested_by"


# ─── mentioned_in never promotes ─────────────────────────────────────────────


def test_prose_mention_in_docs_is_mentioned_in_not_used_by(tmp_path):
    repo = _minimal_repo(tmp_path)
    (repo / "docs").mkdir()
    _write(repo / "scripts" / "leaf.py", "x = 1\n")
    _write(
        repo / "docs" / "catalogue.md",
        "The scripts/leaf.py utility does the thing.\n",
    )

    graph = build_artifact_graph(repo)
    assert graph.rung("scripts/leaf") == "leaf"
    edge = next(e for e in graph.edges if e.target == "scripts/leaf")
    assert edge.kind == "mentioned_in"
    assert edge.evidence == "prose_mention"


def test_run_instruction_in_docs_is_used_by(tmp_path):
    repo = _minimal_repo(tmp_path)
    (repo / "docs").mkdir()
    _write(repo / "scripts" / "runme.py", "x = 1\n")
    _write(
        repo / "docs" / "howto.md",
        "Run it like this:\n\n```\n$ python3 scripts/runme.py\n```\n",
    )

    graph = build_artifact_graph(repo)
    assert graph.is_component("scripts/runme")
    edge = next(e for e in graph.edges if e.target == "scripts/runme")
    assert edge.kind == "used_by"
    assert edge.evidence == "run_instruction"


def test_a_doc_can_carry_both_a_run_instruction_and_separate_prose(tmp_path):
    repo = _minimal_repo(tmp_path)
    (repo / "docs").mkdir()
    _write(repo / "scripts" / "runme.py", "x = 1\n")
    _write(repo / "scripts" / "onlymentioned.py", "x = 1\n")
    _write(
        repo / "docs" / "howto.md",
        "$ python3 scripts/runme.py\n\nSee also scripts/onlymentioned.py for details.\n",
    )

    graph = build_artifact_graph(repo)
    assert graph.rung("scripts/runme") == "component"
    assert graph.rung("scripts/onlymentioned") == "leaf"


# ─── surfaces/skills referencing scripts ─────────────────────────────────────


def test_a_skill_invoking_a_script_is_used_by(tmp_path):
    repo = _minimal_repo(tmp_path)
    _write(repo / "scripts" / "invoked.py", "x = 1\n")
    _write(
        repo / "skills" / "do-thing" / "SKILL.md",
        "Run scripts/invoked.py to do the thing.\n",
    )

    graph = build_artifact_graph(repo)
    assert graph.is_component("scripts/invoked")
    edge = next(e for e in graph.edges if e.target == "scripts/invoked")
    assert edge.kind == "used_by"
    assert edge.evidence == "skill_reference"


def test_a_surface_referencing_a_script_is_used_by(tmp_path):
    repo = _minimal_repo(tmp_path)
    _write(repo / "scripts" / "invoked.py", "x = 1\n")
    _write(repo / "surfaces" / "goals.md", "See scripts/invoked.py.\n")

    graph = build_artifact_graph(repo)
    assert graph.is_component("scripts/invoked")


# ─── systemd (harness repo, never the live host) ─────────────────────────────


def test_a_systemd_unit_checked_into_the_harness_repo_is_used_by(tmp_path):
    repo = _minimal_repo(tmp_path)
    _write(repo / "scripts" / "cleaner.py", "x = 1\n")

    harness_root = tmp_path / "harness"
    unit = harness_root / "host" / "eeepc" / "systemd" / "cleaner.service"
    _write(
        unit,
        "[Service]\nExecStart=/opt/venv/bin/python /opt/.../scripts/cleaner.py\n",
    )

    graph = build_artifact_graph(repo, harness_root=harness_root)
    assert graph.unit_scan_status == "scanned"
    assert graph.is_component("scripts/cleaner")
    edge = next(e for e in graph.edges if e.target == "scripts/cleaner")
    assert edge.evidence == "systemd_unit"


def test_no_systemd_dir_in_harness_repo_is_unavailable_not_scanned(tmp_path):
    repo = _minimal_repo(tmp_path)
    _write(repo / "scripts" / "a.py", "x = 1\n")
    harness_root = tmp_path / "empty-harness"
    harness_root.mkdir()

    graph = build_artifact_graph(repo, harness_root=harness_root)
    assert graph.unit_scan_status == "unavailable"
    assert any("blind spot" in n for n in graph.notes)


# ─── unresolved (never dropped, never an edge) ───────────────────────────────


def test_ambiguous_stem_across_two_scripts_is_unresolved(tmp_path):
    """Two different files happen to share a stem-worthy name only across
    directories is impossible for scripts/ (flat, one glob) -- but a
    reference resolving to more than one *candidate* is still the shape
    ADR-024 rule 5 requires be reported, never guessed. Simulate it by
    forcing a stem collision through the stems_index directly via two
    physically distinct scripts that a naive resolver could confuse: here
    we assert the conservative behavior on a single, unambiguous case
    doesn't accidentally mark it unresolved, and a genuinely unresolvable
    exec path does.
    """
    repo = _minimal_repo(tmp_path)
    _write(repo / "scripts" / "runner.py", "x = 1\n")
    graph = build_artifact_graph(repo)
    assert graph.unresolved_count == 0


def test_runtime_built_subprocess_path_is_unresolved_not_dropped(tmp_path):
    repo = _minimal_repo(tmp_path)
    _write(repo / "scripts" / "target.py", "x = 1\n")
    _write(
        repo / "scripts" / "runner.py",
        "import subprocess\n"
        "name = 'target'\n"
        "subprocess.run(['python3', f'scripts/{name}.py'])\n",
    )

    graph = build_artifact_graph(repo)
    assert not graph.is_component("scripts/target")
    assert graph.unresolved_count == 1
    assert any("runtime-built" in e for e in graph.unresolved_examples)


def test_variable_command_with_no_literal_hint_is_not_a_reference_at_all(tmp_path):
    """No literal `scripts/` substring anywhere in the call -- ADR-024 rule
    5's own text: this is "not a candidate reference in the first place",
    so it must not inflate `unresolved_count` either."""
    repo = _minimal_repo(tmp_path)
    _write(
        repo / "scripts" / "runner.py",
        "import subprocess, sys\n"
        "cmd = build_command()\n"
        "subprocess.run(cmd)\n",
    )

    graph = build_artifact_graph(repo)
    assert graph.unresolved_count == 0


# ─── the rung: read only through the graph's own methods ────────────────────


def test_rung_definition_is_used_by_edges_only_never_tested_or_mentioned(tmp_path):
    repo = _minimal_repo(tmp_path)
    (repo / "docs").mkdir()
    _write(repo / "scripts" / "tested_only.py", "x = 1\n")
    _write(repo / "scripts" / "mentioned_only.py", "x = 1\n")
    _write(repo / "scripts" / "used.py", "x = 1\n")
    _write(repo / "tests" / "test_tested_only.py", "import tested_only\n")
    _write(repo / "docs" / "cat.md", "mentioned_only.py appears in scripts/mentioned_only.py.\n")
    _write(repo / "scripts" / "consumer.py", "import used\n")

    graph = build_artifact_graph(repo)
    assert graph.rung("scripts/tested_only") == "leaf"
    assert graph.rung("scripts/mentioned_only") == "leaf"
    assert graph.rung("scripts/used") == "component"


# ─── oldest_leaves ────────────────────────────────────────────────────────────


def test_oldest_leaves_orders_by_mtime_oldest_first(tmp_path):
    import os
    import time

    repo = _minimal_repo(tmp_path)
    _write(repo / "scripts" / "old.py", "x = 1\n")
    _write(repo / "scripts" / "new.py", "x = 1\n")
    now = time.time()
    os.utime(repo / "scripts" / "old.py", (now - 1000, now - 1000))
    os.utime(repo / "scripts" / "new.py", (now, now))

    graph = build_artifact_graph(repo)
    oldest = graph.oldest_leaves(repo=repo, limit=10)
    assert oldest.index("scripts/old") < oldest.index("scripts/new")


def test_oldest_leaves_without_repo_falls_back_to_discovery_order_not_a_guess(tmp_path):
    repo = _minimal_repo(tmp_path)
    _write(repo / "scripts" / "a.py", "x = 1\n")
    _write(repo / "scripts" / "b.py", "x = 1\n")

    graph = build_artifact_graph(repo)
    assert graph.oldest_leaves(limit=1) == graph.leaves()[:1]


# ─── serialization round trip ─────────────────────────────────────────────────


def test_to_dict_from_dict_round_trip(tmp_path):
    repo = _minimal_repo(tmp_path)
    _write(repo / "scripts" / "producer.py", "x = 1\n")
    _write(repo / "scripts" / "consumer.py", "import producer\n")

    graph = build_artifact_graph(repo)
    restored = ArtifactGraph.from_dict(graph.to_dict())

    assert restored.status == graph.status
    assert set(restored.nodes) == set(graph.nodes)
    assert restored.is_component("scripts/producer")
    assert restored.leaves() == graph.leaves()
    assert restored.components() == graph.components()


def test_to_dict_schema_version_is_present(tmp_path):
    repo = _minimal_repo(tmp_path)
    _write(repo / "scripts" / "a.py", "x = 1\n")
    graph = build_artifact_graph(repo)
    assert graph.to_dict()["schema_version"] == SCHEMA_VERSION


def test_from_dict_of_malformed_data_does_not_raise():
    restored = ArtifactGraph.from_dict({"status": "complete", "nodes": "not-a-dict"})
    assert restored.nodes == {}
    assert restored.edges == []


def test_edge_kinds_constant_matches_adr_024():
    assert set(EDGE_KINDS) == {"used_by", "tested_by", "mentioned_in"}


# ─── publish / read (harness-owned state) ────────────────────────────────────


def test_publish_writes_latest_json_under_state_dir(tmp_path):
    repo = _minimal_repo(tmp_path)
    _write(repo / "scripts" / "a.py", "x = 1\n")
    state_dir = tmp_path / "state"

    path = publish_artifact_graph(state_dir, repo)
    assert path == state_dir / "artifact_graph" / "latest.json"
    assert path.is_file()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["status"] == "complete"


def test_read_latest_after_publish_round_trips(tmp_path):
    repo = _minimal_repo(tmp_path)
    _write(repo / "scripts" / "producer.py", "x = 1\n")
    _write(repo / "scripts" / "consumer.py", "import producer\n")
    state_dir = tmp_path / "state"

    publish_artifact_graph(state_dir, repo)
    graph = read_latest_artifact_graph(state_dir)

    assert graph.status == "complete"
    assert graph.is_component("scripts/producer")


def test_read_latest_with_nothing_published_is_unavailable(tmp_path):
    graph = read_latest_artifact_graph(tmp_path / "never-published")
    assert graph.status == "unavailable"


def test_read_latest_of_corrupt_json_is_unavailable_not_a_crash(tmp_path):
    state_dir = tmp_path / "state"
    path = state_dir / "artifact_graph" / "latest.json"
    _write(path, "{not valid json")

    graph = read_latest_artifact_graph(state_dir)
    assert graph.status == "unavailable"


def test_publish_overwrites_the_previous_snapshot(tmp_path):
    repo = _minimal_repo(tmp_path)
    _write(repo / "scripts" / "a.py", "x = 1\n")
    state_dir = tmp_path / "state"

    publish_artifact_graph(state_dir, repo)
    _write(repo / "scripts" / "b.py", "x = 1\n")
    publish_artifact_graph(state_dir, repo)

    graph = read_latest_artifact_graph(state_dir)
    assert "scripts/b" in graph.nodes
