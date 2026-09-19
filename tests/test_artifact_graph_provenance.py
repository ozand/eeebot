"""Provenance test for the published artifact graph (#1769, ADR-023).

ADR-023's test is provenance, not file ownership: there is no uid boundary
on eeepc (``User=eeepc-agent`` owns both ``state/`` and the instance repo
checkout the loop commits into), so "who owns the file" proves nothing. The
only real question is whether the value's derivation touches anything the
loop can put a commit on. This module's own docstring (see
``nanobot/runtime/artifact_graph.py``, "harness-owned publication") already
states the mechanism this test exists to hold to: the published graph lives
at ``<state_dir>/artifact_graph/latest.json``, and ``state_dir`` is never a
path inside the instance repository at all -- so no prefix the loop can
commit under (``mutation_policy._COMMIT_PATH_PREFIXES``) can ever resolve to
it, by construction of the two being disjoint namespaces, not by a
permission bit.
"""
from __future__ import annotations

from pathlib import Path

from nanobot.runtime.artifact_graph import (
    publish_artifact_graph,
    read_latest_artifact_graph,
)
from nanobot.runtime.mutation_policy import _COMMIT_PATH_PREFIXES, _FORBIDDEN_DIRS


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_the_published_graphs_own_path_is_outside_every_committable_prefix():
    """The namespace check: the publish path's own directory segment
    (``artifact_graph/``) is not, and can never become, a member of
    ``_COMMIT_PATH_PREFIXES`` -- unlike ``scripts/``, ``tests/`` etc., which
    the loop can and does write to every cycle."""
    assert "artifact_graph/" not in _COMMIT_PATH_PREFIXES
    assert not any(prefix.rstrip("/") == "artifact_graph" for prefix in _COMMIT_PATH_PREFIXES)


def test_state_dir_is_declared_forbidden_to_the_loop():
    """``state/`` is one of ``mutation_policy._FORBIDDEN_DIRS`` -- named
    explicitly as off-limits, not merely absent from the allow-list. The
    graph's home directory sits under exactly this prefix."""
    assert "state/" in _FORBIDDEN_DIRS


def test_a_file_the_loop_committed_under_an_allowed_prefix_never_becomes_the_published_graph(tmp_path):
    """The functional proof: plant a forged graph -- claiming, falsely, that
    every artifact is a component -- at a path the loop's own commit surface
    can reach (``scripts/``, the most permissive prefix). Publish the REAL
    graph from a genuinely separate ``state_dir`` that never overlaps any
    ``_COMMIT_PATH_PREFIXES`` member. Reading the published graph back must
    reflect only the real repo content, never the forged file, because the
    read path (``state_dir/artifact_graph/latest.json``) and the forged
    file's path (``repo/scripts/artifact_graph/latest.json``) do not
    intersect -- the loop had no reachable path to the read location at all,
    committable prefix or not.
    """
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "tests").mkdir(parents=True)
    _write(repo / "scripts" / "lonely.py", "x = 1\n")  # a genuine leaf

    # Confirm the forged path itself DOES sit under a real, allowed prefix --
    # this is not a strawman path outside the loop's surface.
    forged_rel = "scripts/artifact_graph/latest.json"
    assert any(forged_rel.startswith(prefix) for prefix in _COMMIT_PATH_PREFIXES)
    forged = repo / forged_rel
    _write(
        forged,
        '{"status": "complete", "nodes": {"scripts/lonely": {"type": "script", '
        '"path": "scripts/lonely.py"}}, "edges": [{"source": "forged", '
        '"target": "scripts/lonely", "kind": "used_by", "evidence": "forged"}]}',
    )

    state_dir = tmp_path / "state"  # disjoint from every _COMMIT_PATH_PREFIXES member
    publish_artifact_graph(state_dir, repo)
    graph = read_latest_artifact_graph(state_dir)

    # The real graph: lonely.py has no genuine reference anywhere -> leaf.
    # If the forged file had been consulted, this would read "component".
    assert graph.rung("scripts/lonely") == "leaf"


def test_publish_never_writes_inside_the_repo_it_reads(tmp_path):
    """A second namespace guard on the writer itself: `publish_artifact_graph`
    must not place its output anywhere under the repo path it was given --
    only under the caller-supplied `state_dir`. If it ever did, the output
    would land inside the loop's own checkout and every prefix-disjointness
    argument above would be moot."""
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    _write(repo / "scripts" / "a.py", "x = 1\n")
    state_dir = tmp_path / "state"

    written = publish_artifact_graph(state_dir, repo)

    assert repo not in written.parents
    assert state_dir in written.parents
