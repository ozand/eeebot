"""Tests for Issue #519: structured lesson recording after subagent commit.
Tests _derive_insight() rule branches and _write_structured_lesson() output."""
from __future__ import annotations
import ast
import datetime
import json
import re
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ── Bridge function extraction ────────────────────────────────────────────────

def _load_bridge_ns(*names: str) -> dict:
    """Extract named functions from bridge without triggering imports."""
    bridge_path = Path(__file__).parent.parent / "nanobot" / "runtime" / "bridge.py"
    source = bridge_path.read_text()
    ns: dict = {"re": re, "Path": Path, "json": json, "datetime": datetime}
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in names:
            func_src = ast.get_source_segment(source, node)
            exec(func_src, ns)  # noqa: S102
    return ns


# ── Test 1: _derive_insight rule branches ─────────────────────────────────────

def test_derive_insight_short_script():
    ns = _load_bridge_ns("_derive_insight")
    result = ns["_derive_insight"](["scripts/foo.py"], tool_calls=15, elapsed_seconds=90)
    assert "single bridge session" in result
    assert "15" in result


def test_derive_insight_fast_task():
    ns = _load_bridge_ns("_derive_insight")
    result = ns["_derive_insight"](["nanobot/runtime/coordinator.py"], tool_calls=25, elapsed_seconds=100)
    assert "2 minutes" in result or "100s" in result


def test_derive_insight_memory_update():
    ns = _load_bridge_ns("_derive_insight")
    result = ns["_derive_insight"](["memory/MEMORY.md"], tool_calls=5, elapsed_seconds=200)
    assert "memory" in result.lower() or "Memory" in result


def test_derive_insight_complex_task():
    ns = _load_bridge_ns("_derive_insight")
    result = ns["_derive_insight"](["nanobot/runtime/coordinator.py"], tool_calls=35, elapsed_seconds=500)
    assert "complex" in result.lower() or "35" in result


def test_derive_insight_default():
    ns = _load_bridge_ns("_derive_insight")
    result = ns["_derive_insight"](["some/other/file.txt"], tool_calls=10, elapsed_seconds=300)
    assert "10" in result or "300" in result
    assert len(result) > 10


# ── Test 2: _write_structured_lesson ─────────────────────────────────────────

def _make_lessons_repo(tmp: Path) -> Path:
    repo = tmp / "eeebot-self-evolving"
    (repo / "lessons").mkdir(parents=True)
    return repo


try:
    import yaml as _yaml_check  # noqa: F401
    HAS_YAML = True
except ImportError:
    HAS_YAML = False


def _load_lessons(path: Path) -> dict:
    """Load lessons file (yaml or json)."""
    text = path.read_text()
    try:
        import yaml
        return yaml.safe_load(text)
    except ImportError:
        return json.loads(text)


@pytest.mark.skipif(not HAS_YAML, reason="PyYAML not installed in test env")
def test_write_structured_lesson_creates_entry():
    """_write_structured_lesson writes a lesson with correct fields."""
    ns = _load_bridge_ns("_derive_insight", "_write_structured_lesson")

    with tempfile.TemporaryDirectory() as td:
        repo = _make_lessons_repo(Path(td))
        result = ns["_write_structured_lesson"](
            repo_root=repo,
            cycle_id="cycle-abc12345678",
            backlog_title="Write scripts/report_summary.py",
            files_changed=["scripts/report_summary.py"],
            commits_pushed=1,
            artifact_data={"hypothesis": "Cycle stats improve operator visibility"},
            budget_used={"tool_calls": 11, "elapsed_seconds": 62},
        )
        assert result is True
        lessons_path = repo / "lessons" / "lessons.yaml"
        assert lessons_path.exists()
        data = _load_lessons(lessons_path)
        lessons = data["lessons"]
        assert len(lessons) == 1
        lesson = lessons[0]
        assert lesson["task_id"] == "Write scripts/report_summary.py"
        assert "report_summary.py" in lesson["result"]
        assert lesson["tool_calls"] == 11
        assert "single bridge session" in lesson["generalized_insight"]
        assert lesson["hypothesis"] == "Cycle stats improve operator visibility"


@pytest.mark.skipif(not HAS_YAML, reason="PyYAML not installed in test env")
def test_write_structured_lesson_no_duplicate():
    """_write_structured_lesson skips duplicate for same cycle_id."""
    ns = _load_bridge_ns("_derive_insight", "_write_structured_lesson")

    with tempfile.TemporaryDirectory() as td:
        repo = _make_lessons_repo(Path(td))
        kwargs = dict(
            repo_root=repo,
            cycle_id="cycle-dup00000001",
            backlog_title="Task X",
            files_changed=["scripts/x.py"],
            commits_pushed=1,
            artifact_data={},
            budget_used={},
        )
        first = ns["_write_structured_lesson"](**kwargs)
        second = ns["_write_structured_lesson"](**kwargs)
        assert first is True
        assert second is False
        data = _load_lessons(repo / "lessons" / "lessons.yaml")
        assert len(data["lessons"]) == 1


@pytest.mark.skipif(not HAS_YAML, reason="PyYAML not installed in test env")
def test_write_structured_lesson_newest_first():
    """_write_structured_lesson inserts newest entry first."""
    ns = _load_bridge_ns("_derive_insight", "_write_structured_lesson")

    with tempfile.TemporaryDirectory() as td:
        repo = _make_lessons_repo(Path(td))
        ns["_write_structured_lesson"](
            repo_root=repo, cycle_id="cycle-old0001", backlog_title="Old task",
            files_changed=["scripts/old.py"], commits_pushed=1, artifact_data={}, budget_used={},
        )
        ns["_write_structured_lesson"](
            repo_root=repo, cycle_id="cycle-new0002", backlog_title="New task",
            files_changed=["scripts/new.py"], commits_pushed=1, artifact_data={}, budget_used={},
        )
        data = _load_lessons(repo / "lessons" / "lessons.yaml")
        lessons = data["lessons"]
        assert lessons[0]["cycle_id"] == "cycle-new0002", "Newest must be first"
        assert lessons[1]["cycle_id"] == "cycle-old0001"


# ── Test 3: coordinator hypotheses.json write ─────────────────────────────────

def test_coordinator_writes_hypotheses_json():
    """_write_research_feed creates state/research/hypotheses.json."""
    from nanobot.runtime.coordinator import _write_research_feed

    with tempfile.TemporaryDirectory() as td:
        state_root = Path(td)
        candidates = [
            {"task_id": "exploit-dash", "title": "Exploit dashboard", "acceptance": "Add metrics"},
            {"task_id": "inspect-streak", "title": "Inspect PASS streak", "acceptance": "Analyze"},
        ]
        _write_research_feed(
            state_root=state_root,
            generated_candidates=candidates,
            cycle_id="cycle-test123",
            goal_id="goal-bootstrap",
        )
        hyp_path = state_root / "research" / "hypotheses.json"
        assert hyp_path.exists(), "hypotheses.json must be created"
        hyps = json.loads(hyp_path.read_text())
        assert isinstance(hyps, list)
        assert len(hyps) == 1
        assert hyps[0]["cycle_id"] == "cycle-test123"
        assert len(hyps[0]["candidates"]) == 2
        assert hyps[0]["candidates"][0]["title"] == "Exploit dashboard"
