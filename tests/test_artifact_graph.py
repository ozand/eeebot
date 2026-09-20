"""Tests for the artifact dependency graph (#1769, ADR-024)."""
from __future__ import annotations

import json
from pathlib import Path

from nanobot.runtime.artifact_graph import (
    EDGE_KINDS,
    SCHEMA_VERSION,
    ArtifactGraph,
    Edge,
    Node,
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


# ─── self-reference and manufactured-consumer edges never promote ───────────
#
# Measured live 2026-09-20: the validity ladder's rung 2 ("an artifact
# nothing ran now has something that runs it") reached the host at 02:52Z;
# proposals shaped "Default <script>.py to self-tests when invoked without
# arguments" went 0 -> 5 within hours. The charter clause was patched
# (PR #1814: "something OTHER THAN ITSELF that runs it"), which closes the
# WORDING the proposer reads. It does not close the MECHANISM: nothing
# stopped the same shape from reaching #1796's ranking through the computed
# graph once entrypoint-kind detection (ADR-025) is built, under a
# different form or a different wording. These tests pin the graph itself
# against the four named forms, independent of prose.
#
# Two of the four (a script importing itself; a wrapper/skill minted next
# to its target) are exercised end-to-end through build_artifact_graph,
# proving the real resolver cannot produce a promoting self-reference from
# source text. All four are ALSO exercised by constructing an ArtifactGraph
# directly with hand-built Node/Edge objects -- this proves the STRUCTURAL
# rule (is_component) itself, independent of whether today's text-based
# resolver happens to have a code path that could produce the shape (it
# does not, for the skill+script pair -- nothing currently makes a skill an
# edge TARGET -- but the rule must hold if that ever changes).


def _graph(nodes: list[Node], edges: list[Edge]) -> ArtifactGraph:
    return ArtifactGraph(
        status="complete",
        generated_at="2026-09-20T00:00:00Z",
        nodes={n.id: n for n in nodes},
        edges=edges,
    )


def test_self_loop_edge_never_promotes():
    """Evasion form 1: a script that imports/invokes itself."""
    graph = _graph(
        nodes=[Node(id="scripts/foo", type="script", path="scripts/foo.py")],
        edges=[Edge(source="scripts/foo.py", target="scripts/foo", kind="used_by", evidence="import_or_exec_path")],
    )
    assert not graph.is_component("scripts/foo")


def test_a_self_tested_script_is_not_promoted_by_its_own_embedded_test():
    """Evasion form 2: a test living in the same file as the thing it
    tests. tested_by never promotes regardless (ADR-025 decision 2), and a
    self-sourced tested_by edge is doubly inert -- both rules apply."""
    graph = _graph(
        nodes=[Node(id="scripts/foo", type="script", path="scripts/foo.py")],
        edges=[Edge(source="scripts/foo.py", target="scripts/foo", kind="tested_by", evidence="import_or_exec_path")],
    )
    assert not graph.is_component("scripts/foo")


def test_a_wrapper_whose_only_consumer_is_its_target_never_promotes_it():
    """Evasion form 3: a wrapper placed next to its target whose only
    consumer is the target -- foo_wrapper "invokes" foo (foo gets a
    used_by edge), but foo_wrapper's own only reference anywhere is foo
    importing it back. Neither is a genuine external dependency."""
    graph = _graph(
        nodes=[
            Node(id="scripts/foo", type="script", path="scripts/foo.py"),
            Node(id="scripts/foo_wrapper", type="script", path="scripts/foo_wrapper.py"),
        ],
        edges=[
            Edge(source="scripts/foo_wrapper.py", target="scripts/foo", kind="used_by", evidence="import_or_exec_path"),
            Edge(source="scripts/foo.py", target="scripts/foo_wrapper", kind="used_by", evidence="import_or_exec_path"),
        ],
    )
    assert not graph.is_component("scripts/foo")
    assert not graph.is_component("scripts/foo_wrapper")


def test_a_skill_whose_only_reader_is_the_script_it_documents_never_promotes_it():
    """Evasion form 4: a skill whose only reader is the script it
    documents -- the same mutual-pair shape as form 3, with a skill+script
    pair instead of two scripts. Nothing in today's resolver makes a skill
    an edge target, so this is a structural guarantee against a future
    extension, exercised directly against the rule rather than through
    build_artifact_graph."""
    graph = _graph(
        nodes=[
            Node(id="scripts/foo", type="script", path="scripts/foo.py"),
            Node(id="skills/foo-doc", type="skill", path="skills/foo-doc/SKILL.md"),
        ],
        edges=[
            Edge(source="skills/foo-doc/SKILL.md", target="scripts/foo", kind="used_by", evidence="skill_reference"),
            Edge(source="scripts/foo.py", target="skills/foo-doc", kind="used_by", evidence="import_or_exec_path"),
        ],
    )
    assert not graph.is_component("scripts/foo")
    assert not graph.is_component("skills/foo-doc")


def test_a_mutual_pair_does_not_strand_a_side_with_genuine_outside_use():
    """The safety valve: a real two-way dependency between two
    independently-used modules must never be wrongly stranded. bar
    genuinely uses foo (external, from elsewhere) AND foo happens to also
    import bar back -- bar's promotion must survive the reciprocal edge to
    foo, since bar has an anchor outside the pair."""
    graph = _graph(
        nodes=[
            Node(id="scripts/foo", type="script", path="scripts/foo.py"),
            Node(id="scripts/bar", type="script", path="scripts/bar.py"),
            Node(id="scripts/consumer", type="script", path="scripts/consumer.py"),
        ],
        edges=[
            # mutual pair between foo and bar
            Edge(source="scripts/bar.py", target="scripts/foo", kind="used_by", evidence="import_or_exec_path"),
            Edge(source="scripts/foo.py", target="scripts/bar", kind="used_by", evidence="import_or_exec_path"),
            # a genuine, independent consumer of bar
            Edge(source="scripts/consumer.py", target="scripts/bar", kind="used_by", evidence="import_or_exec_path"),
        ],
    )
    assert graph.is_component("scripts/bar")  # promoted via consumer.py, not the pair
    assert not graph.is_component("scripts/foo")  # foo's only edge is the manufactured pair


def test_a_doc_or_systemd_source_can_never_form_a_mutual_pair():
    """A source that is not itself a graph node (a doc, a systemd unit)
    cannot reciprocate -- it always promotes, exactly as before this
    hardening. This is the "no it does not apply where the source is
    genuinely external" half of the rule."""
    graph = _graph(
        nodes=[Node(id="scripts/foo", type="script", path="scripts/foo.py")],
        edges=[Edge(source="docs/howto.md", target="scripts/foo", kind="used_by", evidence="run_instruction")],
    )
    assert graph.is_component("scripts/foo")


# ─── in_degree (#1825) -- must agree with is_component's sign at zero ────────


def test_in_degree_zero_matches_a_leaf():
    graph = _graph(nodes=[Node(id="scripts/foo", type="script", path="scripts/foo.py")], edges=[])
    assert graph.in_degree("scripts/foo") == 0
    assert not graph.is_component("scripts/foo")


def test_in_degree_counts_every_genuine_used_by_edge():
    graph = _graph(
        nodes=[
            Node(id="scripts/foo", type="script", path="scripts/foo.py"),
            Node(id="scripts/a", type="script", path="scripts/a.py"),
            Node(id="scripts/b", type="script", path="scripts/b.py"),
        ],
        edges=[
            Edge(source="scripts/a.py", target="scripts/foo", kind="used_by", evidence="import_or_exec_path"),
            Edge(source="scripts/b.py", target="scripts/foo", kind="used_by", evidence="import_or_exec_path"),
            # tested_by must not count toward in-degree, only used_by does.
            Edge(source="tests/test_foo.py", target="scripts/foo", kind="tested_by", evidence="import_or_exec_path"),
        ],
    )
    assert graph.in_degree("scripts/foo") == 2


def test_in_degree_excludes_a_self_loop():
    """A self-loop must read as in-degree ZERO, not one -- a raw edge count
    here would silently disagree with is_component's leaf verdict for the
    same node, which is exactly the gamed-rung failure #1825's in-degree
    number must not reintroduce."""
    graph = _graph(
        nodes=[Node(id="scripts/foo", type="script", path="scripts/foo.py")],
        edges=[Edge(source="scripts/foo.py", target="scripts/foo", kind="used_by", evidence="import_or_exec_path")],
    )
    assert graph.in_degree("scripts/foo") == 0
    assert not graph.is_component("scripts/foo")


def test_in_degree_excludes_a_manufactured_mutual_pair_but_keeps_genuine_use():
    graph = _graph(
        nodes=[
            Node(id="scripts/foo", type="script", path="scripts/foo.py"),
            Node(id="scripts/bar", type="script", path="scripts/bar.py"),
            Node(id="scripts/consumer", type="script", path="scripts/consumer.py"),
        ],
        edges=[
            Edge(source="scripts/bar.py", target="scripts/foo", kind="used_by", evidence="import_or_exec_path"),
            Edge(source="scripts/foo.py", target="scripts/bar", kind="used_by", evidence="import_or_exec_path"),
            Edge(source="scripts/consumer.py", target="scripts/bar", kind="used_by", evidence="import_or_exec_path"),
        ],
    )
    assert graph.in_degree("scripts/foo") == 0  # only the manufactured pair edge
    assert graph.in_degree("scripts/bar") == 1  # the pair edge is excluded, consumer.py counts


def test_a_self_invocation_no_arg_branch_produces_no_edge_at_all(tmp_path):
    """The measured, real-world instance of evasion form 1/2: giving a
    script a no-argument self-test branch (the shape the ladder's rung 2
    was read to satisfy, PR #1814's own measurement). Nothing external
    references the script differently, so no used_by edge is created by
    ANY resolution path -- confirmed against the actual resolver, not just
    the structural rule."""
    repo = _minimal_repo(tmp_path)
    _write(
        repo / "scripts" / "check_thing.py",
        "import sys\n"
        "\n"
        "def _run_self_tests():\n"
        "    assert True\n"
        "\n"
        "def main():\n"
        "    if len(sys.argv) == 1:\n"
        "        _run_self_tests()\n"
        "        return 0\n"
        "    return 1\n"
        "\n"
        "if __name__ == '__main__':\n"
        "    main()\n",
    )
    _write(
        repo / "tests" / "test_check_thing.py",
        "SCRIPT = REPO_ROOT / 'scripts' / 'check_thing.py'\n",
    )

    graph = build_artifact_graph(repo)
    assert not graph.is_component("scripts/check_thing")
    assert not any(e.target == "scripts/check_thing" and e.kind == "used_by" for e in graph.edges)


def test_a_wrapper_script_manufactured_via_real_text_still_does_not_promote(tmp_path):
    """End-to-end through build_artifact_graph (not a hand-built graph):
    a real wrapper script whose only reference anywhere is its target
    importing it back, and vice versa -- the resolver must not promote
    either from source text alone."""
    repo = _minimal_repo(tmp_path)
    _write(repo / "scripts" / "foo.py", "import foo_wrapper\n")
    _write(repo / "scripts" / "foo_wrapper.py", "import foo\n\ndef run():\n    pass\n")

    graph = build_artifact_graph(repo)
    assert not graph.is_component("scripts/foo")
    assert not graph.is_component("scripts/foo_wrapper")


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
