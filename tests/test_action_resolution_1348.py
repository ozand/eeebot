"""Tests for #1348: the action index records enough to name an action.

The miner's three live candidates were ``exec:python3`` x5, ``exec:grep`` x5
and ``edit:scripts/*.py`` x4 + ``exec:python3`` — no procedure can be written
from them because the index normalized the identifying part away. Branch A:
``actions_detail`` records the argv head beyond the interpreter (script path
or ``-m module``) and one concrete target path, bounded and value-free;
``actions`` is unchanged for every existing reader; rows without the new
field fall back to today's behaviour.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from nanobot.runtime import action_index, skill_candidate_mining
from nanobot.runtime.action_index import (
    _command_detail,
    build_action_index,
    normalize_action,
    normalize_action_detail,
)
from nanobot.runtime.skill_candidate_mining import _row_actions, mine

ROOT = "/var/lib/eeepc-agent/self-evolving-agent/eeebot-self-evolving"
ROOTS = (ROOT,)
FIXTURE = Path(__file__).parent / "fixtures" / "skill_candidates_live_2026-09-04.json"


def _exec(command: str) -> str | None:
    return normalize_action_detail("exec", {"command": command}, ROOTS)


# ─── resolution: the argv head beyond the interpreter, one target ─────────────


@pytest.mark.parametrize("command,template,detail", [
    (f"cd {ROOT} && python3 scripts/check_style.py --fast 2>&1 | head -n 40",
     "exec:python3", "exec:python3 scripts/check_style.py"),
    ("cd /x && python3 -m pytest tests/test_check_style.py -q",
     "exec:python3", "exec:python3 -m pytest tests/test_check_style.py"),
    ("cd /x && python3 -m unittest tests.test_agents_structure",
     "exec:python3", "exec:python3 -m unittest tests.test_agents_structure"),
    # the coarse template keeps its pre-#1348 shape (wrapper head); the detail sees through it
    ("cd /x && time timeout 60 python3 scripts/check_style.py --fast",
     "exec:time", "exec:python3 scripts/check_style.py"),
    ("pytest tests/test_action_index.py -q", "exec:pytest", "exec:pytest tests/test_action_index.py"),
    ("cd /x && git add tests/test_agents_structure.py && git commit -m x",
     "exec:git-add", "exec:git-add tests/test_agents_structure.py"),
    ("cd /x && git log --oneline -5 -- scripts/check_style.py", "exec:git-log", "exec:git-log"),
    ('grep -n "DEFAULT_INSPECTION_DIRS" scripts/check_style.py', "exec:grep", "exec:grep"),
    ("grep foo scripts/", "exec:grep", "exec:grep scripts"),
    (f"python3 {ROOT}/scripts/a.py {ROOT}/docs/x.md", "exec:python3", "exec:python3 scripts/a.py docs/x.md"),
    ("sed -n 1,20p scripts/x.py > /tmp/out.txt", "exec:sed", "exec:sed"),
    ("ls tests/ | grep -i style", "exec:ls", "exec:ls tests"),
])
def test_exec_detail_names_script_module_and_one_target(command, template, detail):
    assert normalize_action("exec", {"command": command}, ROOTS) == template
    assert _exec(command) == detail


def test_interpreter_without_a_script_stays_at_the_head():
    assert _exec("python3 -c \"print(open('/etc/passwd').read())\"") == "exec:python3"
    assert _exec("python3 - <<'EOF'\nimport os; print(os.environ['TOKEN'])\nEOF") == "exec:python3"
    assert _exec("python3") == "exec:python3"
    assert _exec("bash -lc 'echo hi'") == "exec:bash"


def test_path_detail_is_the_concrete_workspace_relative_path():
    assert normalize_action("edit_file", {"path": f"{ROOT}/scripts/foo.py"}, ROOTS) == "edit:scripts/*.py"
    assert normalize_action_detail("edit_file", {"path": f"{ROOT}/scripts/foo.py"}, ROOTS) == "edit:scripts/foo.py"
    assert normalize_action_detail("read_file", {"path": "docs/notes/x.md"}, ROOTS) == "read:docs/notes/x.md"
    # outside every workspace root: absolute paths are never recorded; the template stands in
    assert normalize_action_detail("edit_file", {"path": "/etc/passwd"}, ROOTS) == "edit:etc/*"
    # parent-relative escapes keep the pre-#1348 template; the concrete name is not recorded
    assert normalize_action_detail("read_file", {"path": "../../.ssh/id_rsa"}, ROOTS) == "read:.ssh/*"


# ─── security: values are never recorded ─────────────────────────────────────


@pytest.mark.parametrize("command,secret", [
    ("python3 scripts/deploy.py --token sk-live-abc123 scripts/x.py", "sk-live-abc123"),
    ("python3 scripts/deploy.py sk-live-abc123 scripts/x.py", "sk-live-abc123"),
    ("AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI python3 scripts/x.py", "wJalrXUtnFEMI"),
    ("curl -H 'Authorization: Bearer ghp_abc' https://user:pw@host/p", "ghp_abc"),
    ("python3 scripts/x.py https://user:pw@host/p", "pw@host"),
    ("python3 scripts/x.py --password=hunter2", "hunter2"),
    ("python3 scripts/x.py 'a b c'", "a b c"),
    ("cat /etc/eeepc-agent/litellm.env", "eeepc-agent/litellm.env"),
    ("python3 scripts/x.py " + "A" * 200 + ".py", "A" * 130),
])
def test_secret_shaped_arguments_are_never_recorded(command, secret):
    detail = _exec(command)
    assert detail is not None
    assert secret not in detail
    assert detail.startswith(("exec:python3", "exec:curl", "exec:cat"))


def test_secret_shaped_argument_records_only_script_and_target():
    assert _exec("python3 scripts/deploy.py --token sk-live-abc123 scripts/x.py") == "exec:python3 scripts/deploy.py"
    assert _exec("python3 scripts/deploy.py sk-live-abc123 scripts/x.py") == "exec:python3 scripts/deploy.py scripts/x.py"
    assert _exec("python3 scripts/x.py --password=hunter2") == "exec:python3 scripts/x.py"
    assert _exec("cat /etc/eeepc-agent/litellm.env") == "exec:cat"


def test_bounds_are_explicit_and_enforced():
    long_name = "b" * (action_index._DETAIL_TOKEN_CAP + 1) + ".py"
    assert _exec(f"python3 scripts/x.py {long_name}") == "exec:python3 scripts/x.py"
    many = " ".join(f"scripts/t{i}.py" for i in range(20))
    detail = _command_detail(f"pytest {many}", ROOTS)
    assert detail == "pytest scripts/t0.py"  # _DETAIL_MAX_TARGETS = 1
    # a flag anywhere in the scan window ends it: nothing after it is looked at
    assert _command_detail("pytest -x scripts/t0.py", ROOTS) == "pytest"
    assert action_index._DETAIL_MAX_TARGETS == 1 and action_index._DETAIL_SCAN_TOKENS == 8


# ─── index rows: parallel field, old rows untouched ──────────────────────────

_PROMPT_DAY = "2026-08-24"


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 8, 25, 12, 0, 0, tzinfo=tz or timezone.utc)


@pytest.fixture(autouse=True)
def _frozen(monkeypatch):
    monkeypatch.setattr(action_index, "datetime", _FrozenDatetime)
    monkeypatch.setattr(skill_candidate_mining, "datetime", _FrozenDatetime)


def _jsonl(path: Path, rows: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def _record(cycle: str, commands: list[str], edits: list[str]) -> dict:
    calls = [{"function": {"name": "exec", "arguments": json.dumps({"command": c})}} for c in commands]
    calls += [{"function": {"name": "edit_file", "arguments": json.dumps({"path": p})}} for p in edits]
    return {"cycle_id": cycle, "seq": 1, "messages": [{"role": "assistant", "tool_calls": calls}]}


def test_build_writes_actions_detail_parallel_to_actions(tmp_path):
    state = tmp_path / "state"
    # <state_root>/../eeebot-self-evolving is one of the workspace roots the
    # index strips (the instance repo sits next to state/ on the host).
    ws = (tmp_path / "eeebot-self-evolving").as_posix()
    _jsonl(state / "llm_calls" / "prompts" / f"{_PROMPT_DAY}.jsonl", [
        _record("cycle-1", [f"cd {ws} && python3 scripts/check_style.py --fast"], [f"{ws}/scripts/foo.py"]),
    ])
    _jsonl(state / "ledger" / "cycles.jsonl", [
        {"phase": "proposed", "cycle_id": "cycle-1", "task_title": "t"},
        {"phase": "outcome", "cycle_id": "cycle-1", "outcome": "success", "ts": f"{_PROMPT_DAY}T10:00:00Z"},
    ])
    build_action_index(state)
    rows = [json.loads(l) for l in (state / "action_index" / f"{_PROMPT_DAY}.jsonl").read_text().splitlines()]
    assert rows[0]["actions"] == ["exec:python3", "edit:scripts/*.py"]
    assert rows[0]["actions_detail"] == ["exec:python3 scripts/check_style.py", "edit:scripts/foo.py"]


def test_rows_without_detail_fall_back_to_actions():
    legacy = {"cycle_id": "c", "ts": "2026-08-01T00:00:00Z", "actions": ["exec:python3", "edit:scripts/*.py"]}
    assert _row_actions(legacy) == ["exec:python3", "edit:scripts/*.py"]
    mismatched = dict(legacy, actions_detail=["exec:python3 x.py"])  # length differs: ignored
    assert _row_actions(mismatched) == ["exec:python3", "edit:scripts/*.py"]
    good = dict(legacy, actions_detail=["exec:python3 scripts/x.py", "edit:scripts/x.py"])
    assert _row_actions(good) == ["exec:python3 scripts/x.py", "edit:scripts/x.py"]


def test_mixed_format_window_reads_both_shapes(tmp_path):
    """Old rows keep contributing their coarse grams; new rows contribute named ones."""
    state = tmp_path / "state"
    old = [
        {"cycle_id": f"old-{i}", "ts": f"2026-08-{i + 1:02d}T12:00:00Z",
         "actions": ["exec:python3", "exec:python3", "edit:scripts/*.py"]}
        for i in range(1, 9)
    ]
    new = [
        {"cycle_id": f"new-{i}", "ts": f"2026-08-{i + 10:02d}T12:00:00Z",
         "actions": ["exec:python3", "exec:python3", "edit:scripts/*.py"],
         "actions_detail": ["exec:python3 scripts/check_style.py", "exec:python3 -m pytest tests/test_check_style.py",
                            "edit:scripts/check_style.py"]}
        for i in range(1, 9)
    ]
    _jsonl(state / "action_index" / "2026-08-01.jsonl", old + new)
    sequences = [tuple(c["sequence"]) for c in mine(state)]
    assert ("exec:python3", "exec:python3", "edit:scripts/*.py") in sequences
    assert ("exec:python3 scripts/check_style.py", "exec:python3 -m pytest tests/test_check_style.py",
            "edit:scripts/check_style.py") in sequences


# ─── the live sidecar, as a fixture ──────────────────────────────────────────


def test_live_sidecar_fixture_names_no_action():
    """The defect, pinned: every token is a bare binary or a glob — nothing a skill could be written from."""
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert data["schema"] == "skill-candidates-v1" and len(data["candidates"]) == 3
    for candidate in data["candidates"]:
        for token in candidate["sequence"]:
            assert " " not in token  # no argv head beyond the binary
            assert token.split(":", 1)[1] in {"python3", "grep", "scripts/*.py"}


def test_legacy_rows_shaped_like_the_live_index_reproduce_the_fixture(tmp_path):
    """Same input, same output: rows without actions_detail behave exactly as before #1348."""
    state = tmp_path / "state"
    rows = [
        {"cycle_id": f"c-{i}", "ts": f"2026-08-{i + 1:02d}T12:00:00Z",
         "actions": (["exec:python3"] if i % 2 else ["exec:grep"]) * 5}
        for i in range(1, 19)
    ]
    _jsonl(state / "action_index" / "2026-08-01.jsonl", rows)
    assert sorted(tuple(c["sequence"]) for c in mine(state)) == [("exec:grep",) * 5, ("exec:python3",) * 5]


def test_existing_skill_naming_the_script_suppresses_the_candidate(tmp_path):
    state = tmp_path / "state"
    repo = tmp_path / "repo"
    (repo / "skills" / "style-check").mkdir(parents=True)
    (repo / "skills" / "style-check" / "SKILL.md").write_text(
        "---\nname: style-check\n---\nRun `python3 scripts/check_style.py --fast` before committing.\n", encoding="utf-8"
    )
    rows = [
        {"cycle_id": f"c-{i}", "ts": f"2026-08-{i + 1:02d}T12:00:00Z",
         "actions": ["edit:scripts/*.py", "exec:python3"],
         "actions_detail": ["edit:scripts/check_style.py", "exec:python3 scripts/check_style.py"]}
        for i in range(1, 10)
    ]
    _jsonl(state / "action_index" / "2026-08-01.jsonl", rows)
    assert mine(state, None) != []
    assert mine(state, repo) == []


def test_trivial_set_is_not_extended():
    assert skill_candidate_mining._TRIVIAL == frozenset({
        ("exec:pytest", "exec:git-commit"),
        ("exec:pytest", "exec:git-commit", "exec:git-push"),
    })
