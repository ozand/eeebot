"""Trajectory-level verification (#1825, ADR-011 rule 3)."""
from __future__ import annotations

import json
from pathlib import Path

from nanobot.runtime import cycle_ledger, trajectory
from nanobot.runtime.artifact_graph import ArtifactGraph, Edge, Node


def _proposed(state_dir: Path, cycle_id: str, task_title: str, target_path: str) -> None:
    cycle_ledger.append_event(
        state_dir,
        {"phase": "proposed", "cycle_id": cycle_id, "task_title": task_title, "target_path": target_path},
    )


def _outcome(state_dir: Path, cycle_id: str, outcome: str, files_changed: list[str]) -> None:
    cycle_ledger.record_cycle_outcome(state_dir, cycle_id, outcome, None, files_changed, "branch/" + cycle_id)


def _publish_graph(state_dir: Path, graph: ArtifactGraph) -> None:
    path = state_dir / "artifact_graph" / "latest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(graph.to_dict(), ensure_ascii=False), encoding="utf-8")


# ─── shape_concentration ──────────────────────────────────────────────────


def test_shape_concentration_reports_insufficient_data_below_the_minimum(tmp_path: Path):
    state = tmp_path / "state"
    for i in range(3):
        _proposed(state, f"c{i}", "add a function", f"scripts/x{i}.py")
    result = trajectory.shape_concentration(state, repo=tmp_path)
    assert result["ok"] is False
    assert result["reason"] == "insufficient_data"


def test_shape_concentration_reproduces_a_dominant_share(tmp_path: Path):
    state = tmp_path / "state"
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    for i in range(30):
        p = repo / "scripts" / f"existing{i}.py"
        p.write_text("pass\n", encoding="utf-8")
        _proposed(state, f"extend{i}", "add a function", f"scripts/existing{i}.py")
    for i in range(10):
        _proposed(state, f"other{i}", "investigate something", "")

    result = trajectory.shape_concentration(state, repo=repo, window=40)
    assert result["ok"] is True
    assert result["window"] == 40
    assert result["row_count"] == 40
    assert result["dominant_shape"] == "extend_existing_script"
    assert result["share"] == 30 / 40


def test_shape_concentration_reports_trend_direction_and_delta(tmp_path: Path):
    state = tmp_path / "state"
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "scripts" / "x.py").write_text("pass\n", encoding="utf-8")

    # Prior window: all "other".
    for i in range(10):
        _proposed(state, f"prior{i}", "investigate something", "")
    # Current window: all "extend_existing_script".
    for i in range(10):
        _proposed(state, f"curr{i}", "add a function", "scripts/x.py")

    result = trajectory.shape_concentration(state, repo=repo, window=10)
    assert result["ok"] is True
    assert result["dominant_shape"] == "extend_existing_script"
    assert result["share"] == 1.0
    assert result["trend"]["direction"] == "up"
    assert result["trend"]["prior_share"] == 0.0
    assert result["trend"]["delta"] == 1.0


def test_shape_concentration_omits_trend_when_the_prior_window_is_thin(tmp_path: Path):
    state = tmp_path / "state"
    for i in range(6):
        _proposed(state, f"c{i}", "investigate something", "")
    result = trajectory.shape_concentration(state, window=6)
    assert result["ok"] is True
    assert result["trend"] is None


def test_shape_concentration_uses_the_outcome_join_to_tell_new_leaf_from_extend(tmp_path: Path):
    """End-to-end proof of the PR #1834 review fix: shape_concentration
    itself (not just classify_task_shape in isolation) must distinguish
    a script the cycle created from one it extended, by joining each
    'proposed' row to its own 'outcome' row's main_sha_before -- a
    'proposed' row carries no base sha of its own."""
    import subprocess

    def _git(repo: Path, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repo), *args], check=True, text=True, capture_output=True,
        ).stdout.strip()

    state = tmp_path / "state"
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    (repo / "scripts" / "existing.py").write_text("pass\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    base_sha = _git(repo, "rev-parse", "HEAD")
    (repo / "scripts" / "new_leaf.py").write_text("pass\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "cycle")

    # _MIN_ROWS_FOR_A_FINDING padding: a few filler proposals with no
    # matching outcome row, so they fall to the "other" bucket without
    # affecting the two rows under test.
    for i in range(3):
        _proposed(state, f"filler-{i}", "investigate something", "")

    _proposed(state, "extend-cycle", "add a function", "scripts/existing.py")
    cycle_ledger.record_cycle_outcome(
        state, "extend-cycle", "success", None, ["scripts/existing.py"], "branch/extend-cycle",
        main_sha_before=base_sha,
    )
    _proposed(state, "new-leaf-cycle", "add a script", "scripts/new_leaf.py")
    cycle_ledger.record_cycle_outcome(
        state, "new-leaf-cycle", "success", None, ["scripts/new_leaf.py"], "branch/new-leaf-cycle",
        main_sha_before=base_sha,
    )

    result = trajectory.shape_concentration(state, repo=repo, window=5)
    assert result["ok"] is True
    assert result["counts"]["extend_existing_script"] == 1
    assert result["counts"]["new_leaf_script"] == 1


def test_shape_concentration_window_and_row_count_are_always_reported():
    """AC: 'the report states its own window and row count, so a thin
    window cannot read as a confident finding' -- true on both the
    ok and the insufficient-data path."""
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        state = Path(td) / "state"
        result = trajectory.shape_concentration(state)
        assert "window" in result and "row_count" in result


# ─── target_concentration ─────────────────────────────────────────────────


def test_target_concentration_reports_unavailable_without_a_published_graph(tmp_path: Path):
    state = tmp_path / "state"
    result = trajectory.target_concentration(state)
    assert result["ok"] is False
    assert result["reason"] == "artifact_graph_unavailable"


def test_target_concentration_reproduces_the_9_4_0_shape(tmp_path: Path):
    """The exact shape of #1825's own hand measurement: 20 successful
    cycles, 13 code-bearing, 9 to leaves, 4 to 1-2-consumer components,
    0 to the most-used artifacts."""
    state = tmp_path / "state"

    nodes = [Node(id=f"scripts/leaf{i}", type="script", path=f"scripts/leaf{i}.py") for i in range(9)]
    nodes += [Node(id=f"scripts/comp{i}", type="script", path=f"scripts/comp{i}.py") for i in range(4)]
    nodes.append(Node(id="scripts/hot", type="script", path="scripts/hot.py"))
    nodes.append(Node(id="scripts/consumer", type="script", path="scripts/consumer.py"))

    edges = [
        Edge(source="scripts/consumer.py", target="scripts/comp0", kind="used_by", evidence="x"),
        Edge(source="scripts/consumer.py", target="scripts/comp1", kind="used_by", evidence="x"),
        Edge(source="scripts/consumer.py", target="scripts/comp2", kind="used_by", evidence="x"),
        Edge(source="scripts/consumer.py", target="scripts/comp3", kind="used_by", evidence="x"),
        Edge(source="scripts/a.py", target="scripts/hot", kind="used_by", evidence="x"),
        Edge(source="scripts/b.py", target="scripts/hot", kind="used_by", evidence="x"),
        Edge(source="scripts/c.py", target="scripts/hot", kind="used_by", evidence="x"),
    ]
    graph = ArtifactGraph(status="complete", generated_at="2026-09-20T13:04:00Z", nodes={n.id: n for n in nodes}, edges=edges)
    _publish_graph(state, graph)

    for i in range(9):
        _outcome(state, f"leaf-cycle-{i}", "success", [f"scripts/leaf{i}.py", f"tests/test_leaf{i}.py"])
    for i in range(4):
        _outcome(state, f"comp-cycle-{i}", "success", [f"scripts/comp{i}.py"])
    for i in range(7):
        _outcome(state, f"doc-cycle-{i}", "success", ["docs/notes.md"])

    result = trajectory.target_concentration(state, window=20)
    assert result["ok"] is True
    assert result["successful_in_window"] == 20
    assert result["code_bearing_in_window"] == 13
    assert result["bands"] == {"in_degree_0": 9, "in_degree_1_2": 4, "in_degree_3_plus": 0}


def test_target_concentration_only_reads_the_last_window_successful_cycles(tmp_path: Path):
    state = tmp_path / "state"
    graph = ArtifactGraph(
        status="complete", generated_at="2026-09-20T13:04:00Z",
        nodes={"scripts/leaf": Node(id="scripts/leaf", type="script", path="scripts/leaf.py")}, edges=[],
    )
    _publish_graph(state, graph)
    for i in range(30):
        _outcome(state, f"c{i}", "success", ["scripts/leaf.py"])

    result = trajectory.target_concentration(state, window=20)
    assert result["successful_in_window"] == 20


def test_target_concentration_excludes_non_successful_outcomes(tmp_path: Path):
    state = tmp_path / "state"
    graph = ArtifactGraph(status="complete", generated_at="x", nodes={}, edges=[])
    _publish_graph(state, graph)
    _outcome(state, "c1", "success", ["scripts/leaf.py"])
    _outcome(state, "c2", "failed", ["scripts/leaf.py"])

    result = trajectory.target_concentration(state, window=20)
    assert result["successful_in_window"] == 1


def test_target_concentration_counts_a_test_only_change_as_unresolved_not_a_leaf(tmp_path: Path):
    """A code-bearing change with no non-test file (classify_change_tier
    still calls this code-bearing) has no artifact to band -- it must not
    silently count as a leaf."""
    state = tmp_path / "state"
    graph = ArtifactGraph(status="complete", generated_at="x", nodes={}, edges=[])
    _publish_graph(state, graph)
    _outcome(state, "c1", "success", ["tests/test_only.py"])

    result = trajectory.target_concentration(state, window=20)
    assert result["code_bearing_in_window"] == 1
    assert result["unresolved_targets"] == 1
    assert result["bands"] == {"in_degree_0": 0, "in_degree_1_2": 0, "in_degree_3_plus": 0}


# ─── write_trajectory_report ──────────────────────────────────────────────


def test_write_trajectory_report_persists_both_sections(tmp_path: Path):
    state = tmp_path / "state"
    result = trajectory.write_trajectory_report(state, tmp_path / "repo")
    assert result["ok"] is True
    payload = json.loads((state / trajectory.REPORT_REL).read_text(encoding="utf-8"))
    assert payload["schema"] == trajectory.SCHEMA_VERSION
    assert "shape_concentration" in payload
    assert "target_concentration" in payload
