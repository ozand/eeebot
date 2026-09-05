"""Tests for #1348: the action index records enough to name an action.

The miner's three live candidates were ``exec:python3`` x5, ``exec:grep`` x5
and ``edit:scripts/*.py`` x4 + ``exec:python3`` — no procedure can be written
from them because the index normalized the identifying part away. Branch A:
``actions_detail`` records the argv head beyond the interpreter (script path
or ``-m module``) and one concrete target path, bounded and value-free;
``actions`` keeps its shape for every existing reader; rows without the new
field fall back to today's behaviour.

A target is recorded only when it is a real file under a workspace root AND
the head's positional contract says that slot is a file — a suffix never
proves a role. The ``ws`` fixture is that workspace.
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

FIXTURE = Path(__file__).parent / "fixtures" / "skill_candidates_live_2026-09-04.json"

WS_FILES = (
    "scripts/check_style.py", "scripts/a.py", "scripts/x.py", "scripts/deploy.py", "scripts/foo.py",
    "scripts/t0.py", "scripts/f0.py", "tests/test_check_style.py", "tests/test_x.py",
    "tests/test_action_index.py", "tests/test_agents_structure.py", "docs/x.md", "docs/notes/x.md",
    "lessons/errors.yaml", "config.json",
)


@pytest.fixture(scope="module")
def ws(tmp_path_factory) -> str:
    """A workspace root holding the files the commands below refer to (POSIX string)."""
    root = tmp_path_factory.mktemp("eeebot-self-evolving")
    for rel in WS_FILES:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x\n", encoding="utf-8")
    return root.as_posix()


def _exec(command: str, ws: str) -> str | None:
    return normalize_action_detail("exec", {"command": command}, (ws,))


# ─── resolution: the argv head beyond the interpreter, one confirmed target ───


@pytest.mark.parametrize("command,template,detail", [
    ("cd {WS} && python3 scripts/check_style.py --fast 2>&1 | head -n 40",
     "exec:python3", "exec:python3 scripts/check_style.py"),
    ("cd /x && python3 -m pytest tests/test_check_style.py -q",
     "exec:python3", "exec:python3 -m pytest tests/test_check_style.py"),
    # a dotted positional is never recorded (hostnames, key names and short
    # JWTs look the same); only the -m position may carry a module
    ("cd /x && python3 -m unittest tests.test_agents_structure",
     "exec:python3", "exec:python3 -m unittest"),
    # the coarse template keeps its pre-#1348 shape (wrapper head); the detail sees through it
    ("cd /x && time timeout 60 python3 scripts/check_style.py --fast",
     "exec:time", "exec:python3 scripts/check_style.py"),
    ("pytest tests/test_action_index.py -q", "exec:pytest", "exec:pytest tests/test_action_index.py"),
    ("cd /x && git add tests/test_agents_structure.py && git commit -m x",
     "exec:git-add", "exec:git-add tests/test_agents_structure.py"),
    # flags are skipped, the token after a flag is its value (never inspected),
    # `--` ends options; the positional file is the target
    ("cd /x && git log --oneline -5 -- scripts/check_style.py", "exec:git-log", "exec:git-log scripts/check_style.py"),
    # grep's first positional slot is the pattern; -n takes no value, but the
    # rule treats the next token as one — it still occupies the pattern slot
    ('grep -n "DEFAULT_INSPECTION_DIRS" scripts/check_style.py', "exec:grep", "exec:grep scripts/check_style.py"),
    ("grep foo scripts/check_style.py", "exec:grep", "exec:grep scripts/check_style.py"),
    ("grep -e secretpattern scripts/x.py", "exec:grep", "exec:grep scripts/x.py"),
    ('grep -rn "pat" scripts/', "exec:grep", "exec:grep"),  # directories are not targets
    ("grep foo scripts/", "exec:grep", "exec:grep"),
    # -m 5: the value takes the pattern slot, PAT is slot 2 — recorded only if it is a real file
    ("grep -m 5 secret.py scripts/a.py", "exec:grep", "exec:grep scripts/a.py"),
    # a script's own positionals have no known contract: never recorded
    ("python3 {WS}/scripts/a.py {WS}/docs/x.md", "exec:python3", "exec:python3 scripts/a.py"),
    ("cat scripts/a.py", "exec:cat", "exec:cat scripts/a.py"),
    ("sed -n 1,20p scripts/x.py > /tmp/out.txt", "exec:sed", "exec:sed scripts/x.py"),
    ("cmd -o out.txt scripts/x.py", "exec:cmd", "exec:cmd"),  # unknown head: no contract, no target
    ("ls tests/ | grep -i style", "exec:ls", "exec:ls"),
])
def test_exec_detail_names_script_module_and_one_target(ws, command, template, detail):
    command = command.replace("{WS}", ws)
    assert normalize_action("exec", {"command": command}, (ws,)) == template
    assert _exec(command, ws) == detail


def test_interpreter_without_a_script_stays_at_the_head(ws):
    assert _exec("python3 -c \"print(open('/etc/passwd').read())\"", ws) == "exec:python3"
    assert _exec("python3 - <<'EOF'\nimport os; print(os.environ['TOKEN'])\nEOF", ws) == "exec:python3"
    assert _exec("python3", ws) == "exec:python3"
    assert _exec("bash -lc 'echo hi'", ws) == "exec:bash"


def test_target_must_be_a_real_file_under_a_workspace_root(ws):
    """A suffix proves nothing; the file has to exist under a root."""
    assert _exec("cat scripts/a.py", ws) == "exec:cat scripts/a.py"
    assert _exec("cat scripts/does_not_exist.py", ws) == "exec:cat"
    assert _exec("python3 scripts/nope.py", ws) == "exec:python3"
    # an out-of-workspace absolute script is not recorded at all (no basename fallback)
    assert _exec("python3 /srv/tool.py", ws) == "exec:python3"
    assert _exec(f"python3 {ws}/scripts/a.py", ws) == "exec:python3 scripts/a.py"
    assert normalize_action_detail("exec", {"command": "python3 scripts/a.py"}, ()) == "exec:python3"  # no roots: nothing confirmed


def test_path_detail_is_the_concrete_workspace_relative_path(ws):
    assert normalize_action("edit_file", {"path": f"{ws}/scripts/foo.py"}, (ws,)) == "edit:scripts/*.py"
    assert normalize_action_detail("edit_file", {"path": f"{ws}/scripts/foo.py"}, (ws,)) == "edit:scripts/foo.py"
    assert normalize_action_detail("read_file", {"path": "docs/notes/x.md"}, (ws,)) == "read:docs/notes/x.md"
    # outside every workspace root: absolute paths are never recorded; the template stands in
    assert normalize_action_detail("edit_file", {"path": "/etc/passwd"}, (ws,)) == "edit:etc/*"
    # parent-relative escapes keep the pre-#1348 template; the concrete name is not recorded
    assert normalize_action_detail("read_file", {"path": "../../.ssh/id_rsa"}, (ws,)) == "read:.ssh/*"
    # a file that does not exist under any root falls back to the template
    assert normalize_action_detail("read_file", {"path": "docs/missing.md"}, (ws,)) == "read:docs/*.md"


def test_tool_names_with_a_colon_keep_their_prefix(ws):
    assert normalize_action("browser:navigate", {"path": "docs/x.md"}, (ws,)) == "browser:navigate:docs/*.md"
    assert normalize_action_detail("browser:navigate", {"path": "docs/x.md"}, (ws,)) == "browser:navigate:docs/x.md"


# ─── security: values are never recorded ─────────────────────────────────────


@pytest.mark.parametrize("command,secret", [
    ("env OPENAI_API_KEY=sk-live-abc123 python3 scripts/x.py", "sk-live-abc123"),
    ('TOKEN="super secret value" python3 scripts/x.py', "secret"),
    ('TOKEN="super secret value python3 scripts/x.py', "secret"),  # unbalanced quote
    ("sudo -u eeepc-agent python3 scripts/x.py", "eeepc-agent"),
    ("sk-ant-" + "Q" * 300, "Q" * 64),
    ("curl api.internal.example.com", "api.internal"),
    ("bash scripts/deploy.sh prod.secret.key", "prod.secret"),
    ("python3 scripts/x.py eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.dBjftJeZ4CVPmB92K27u", "eyJ"),
    ("mytool AbC123/xYz789QqW", "AbC123"),
    ("python3 scripts/deploy.py --token sk-live-abc123 scripts/x.py", "sk-live-abc123"),
    ("python3 scripts/deploy.py sk-live-abc123 scripts/x.py", "sk-live-abc123"),
    ("AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI python3 scripts/x.py", "wJalrXUtnFEMI"),
    ("curl -H 'Authorization: Bearer ghp_abc' https://user:pw@host/p", "ghp_abc"),
    ("python3 scripts/x.py https://user:pw@host/p", "pw@host"),
    ("python3 scripts/x.py --password=hunter2", "hunter2"),
    ("python3 scripts/x.py 'a b c'", "a b c"),
    ("cat /etc/eeepc-agent/litellm.env", "eeepc-agent/litellm.env"),
    ("python3 scripts/x.py " + "A" * 200 + ".py", "A" * 130),
    # a secret wearing an allowed suffix: a suffix does not prove a role
    ("grep -n secret.py scripts/a.py", "secret.py"),
    ("grep hunter2.json scripts/a.py", "hunter2.json"),
    ("grep -m 5 secret.py scripts/a.py", "secret.py"),
    ("python3 scripts/a.py secret.py", "secret.py"),
    ("python3 scripts/a.py config.json", "config.json"),  # a real file, but an unknown positional
    ("python3 scripts/a.py --out config.json token.json", "token.json"),
    ("mytool secret.json", "secret.json"),
    ("cat secret.json", "secret.json"),  # cat's slot is a file, but no such file exists
])
def test_secret_shaped_arguments_are_never_recorded(ws, command, secret):
    detail = _exec(command, ws)
    template = normalize_action("exec", {"command": command}, (ws,))
    assert detail is not None and template is not None
    assert secret not in detail and secret not in template
    assert detail.startswith(("exec:python3", "exec:curl", "exec:cat", "exec:sudo", "exec:bash", "exec:mytool", "exec:grep", "exec:*"))


def test_secret_shaped_argument_records_only_script_and_target(ws):
    assert _exec("python3 scripts/deploy.py --token sk-live-abc123 scripts/x.py", ws) == "exec:python3 scripts/deploy.py"
    assert _exec("python3 scripts/deploy.py sk-live-abc123 scripts/x.py", ws) == "exec:python3 scripts/deploy.py"
    assert _exec("python3 scripts/x.py --password=hunter2", ws) == "exec:python3 scripts/x.py"
    assert _exec("cat /etc/eeepc-agent/litellm.env", ws) == "exec:cat"
    # env-wrapped and quoted assignments are dropped before the head is chosen
    assert _exec("env OPENAI_API_KEY=sk-live-abc123 python3 scripts/x.py", ws) == "exec:python3 scripts/x.py"
    assert _exec('TOKEN="super secret value" python3 scripts/x.py', ws) == "exec:python3 scripts/x.py"
    assert normalize_action("exec", {"command": 'TOKEN="super secret value" python3 scripts/x.py'}) == "exec:python3"
    # wrapper flags end the detail (sudo -u <user> would otherwise become the head)
    assert _exec("sudo -u eeepc-agent python3 scripts/x.py", ws) == "exec:sudo"
    # a head outside the charset/length bound yields no detail and an ``exec:*`` template
    assert _exec("sk-ant-" + "Q" * 300, ws) == "exec:*"
    assert normalize_action("exec", {"command": 'TOKEN="unbalanced python3 scripts/x.py'}) == "exec:*"
    # suffix is not role: grep's first slot is the pattern; a script's positionals are unknown
    assert _exec("grep -n secret.py scripts/a.py", ws) == "exec:grep scripts/a.py"
    assert _exec("python3 scripts/a.py secret.py", ws) == "exec:python3 scripts/a.py"
    assert _exec("mytool secret.json", ws) == "exec:mytool"


def test_bounds_are_explicit_and_enforced(ws):
    long_name = "b" * (action_index._DETAIL_TOKEN_CAP + 1) + ".py"
    assert _exec(f"python3 scripts/x.py {long_name}", ws) == "exec:python3 scripts/x.py"
    many = " ".join(f"scripts/t{i}.py" for i in range(20))
    assert _command_detail(f"pytest {many}", (ws,)) == "pytest scripts/t0.py"  # _DETAIL_MAX_TARGETS = 1
    # the token after a flag is its value: nothing recorded from it
    assert _command_detail("pytest -x scripts/t0.py", (ws,)) == "pytest"
    assert action_index._DETAIL_MAX_TARGETS == 1 and action_index._DETAIL_SCAN_TOKENS == 8
    # the head slot is bounded as well (64 chars, fixed charset)
    assert _command_detail("a" * 65 + " scripts/x.py", (ws,)) is None
    # an over-cap script path is dropped whole, never truncated — there is no basename fallback
    root = Path(ws)
    long_script = root / "scripts" / ("s" * 200 + ".py")
    long_script.write_text("x\n", encoding="utf-8")
    assert _exec(f"python3 {long_script.as_posix()}", ws) == "exec:python3"
    assert _exec(f"python3 scripts/{'s' * 200}.py", ws) == "exec:python3"


def test_tokenization_is_lazy_and_bounded(ws):
    """The body of -c / heredoc is never tokenized; a giant argv stops at the budget."""
    # an unbalanced quote INSIDE the body would raise if the body were tokenized
    assert _exec("python3 -c 'print(1)' \"unbalanced " + "x" * 1_000_000, ws) == "exec:python3"
    assert _exec("python3 - <<'EOF'\n\"unbalanced\nimport os\nEOF", ws) == "exec:python3"
    assert normalize_action("exec", {"command": "python3 -c 'x' \"unbalanced"}) == "exec:python3"
    giant = "cat " + " ".join(f"scripts/f{i}.py" for i in range(5000))
    assert len(action_index._lazy_tokens(giant)) == action_index._TOKEN_BUDGET
    assert _exec(giant, ws) == "exec:cat scripts/f0.py"
    # grep -c is a count flag, not a code body: tokenization continues
    assert _exec("grep -c pat scripts/a.py", ws) == "exec:grep scripts/a.py"


def test_detail_errors_never_reach_the_coarse_index(ws, monkeypatch):
    record = {"cycle_id": "c", "messages": [{"role": "assistant", "tool_calls": [
        {"function": {"name": "exec", "arguments": {"command": "pytest tests/test_x.py"}}},
    ]}]}

    def _boom(*a, **k):
        raise RuntimeError("detail bug")

    monkeypatch.setattr(action_index, "normalize_action_detail", _boom)
    assert action_index._tool_call_pairs(record, (ws,)) == [("exec:pytest", "exec:pytest")]


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
    repo = tmp_path / "eeebot-self-evolving"
    for rel in ("scripts/check_style.py", "scripts/foo.py"):
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text("x\n", encoding="utf-8")
    ws_ = repo.as_posix()
    _jsonl(state / "llm_calls" / "prompts" / f"{_PROMPT_DAY}.jsonl", [
        _record("cycle-1", [f"cd {ws_} && python3 scripts/check_style.py --fast"], [f"{ws_}/scripts/foo.py"]),
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
    report = skill_candidate_mining.mine_report(state)
    assert [tuple(c["sequence"]) for c in report["candidates"]] == [
        ("exec:python3 scripts/check_style.py", "exec:python3 -m pytest tests/test_check_style.py", "edit:scripts/check_style.py"),
    ]
    # the coarse gram from the old rows still qualifies — reported, not presented
    assert ("exec:python3", "exec:python3", "edit:scripts/*.py") in [tuple(c["sequence"]) for c in report["unnameable"]]


# ─── the live sidecar, as a fixture ──────────────────────────────────────────


def test_live_sidecar_fixture_names_no_action():
    """The defect, pinned: every token is a bare binary or a glob — nothing a skill could be written from."""
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert data["schema"] == "skill-candidates-v1" and len(data["candidates"]) == 3
    for candidate in data["candidates"]:
        for token in candidate["sequence"]:
            assert " " not in token  # no argv head beyond the binary
            assert token.split(":", 1)[1] in {"python3", "grep", "scripts/*.py"}


def test_legacy_rows_shaped_like_the_live_index_land_in_unnameable(tmp_path):
    """Rows without actions_detail still parse and still qualify — but a gram that
    carries no concrete file is reported under ``unnameable``, not presented to demand."""
    state = tmp_path / "state"
    rows = [
        {"cycle_id": f"c-{i}", "ts": f"2026-08-{i + 1:02d}T12:00:00Z",
         "actions": (["exec:python3"] if i % 2 else ["exec:grep"]) * 5}
        for i in range(1, 19)
    ]
    _jsonl(state / "action_index" / "2026-08-01.jsonl", rows)
    report = skill_candidate_mining.mine_report(state)
    assert report["candidates"] == []  # nothing a skill could be written from
    assert sorted(tuple(c["sequence"]) for c in report["unnameable"]) == [("exec:grep",) * 5, ("exec:python3",) * 5]
    skill_candidate_mining.write_sidecar(state, None)
    sidecar = json.loads((state / "demand" / "skill_candidates.json").read_text(encoding="utf-8"))
    assert sidecar["candidates"] == [] and len(sidecar["unnameable"]) == 2
    assert skill_candidate_mining.read_sidecar(state) == []  # demand sees no work


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
    # a skill that only mentions the file, not the command, does not cover it
    (repo / "skills" / "style-check" / "SKILL.md").write_text(
        "---\nname: style-check\n---\nSee scripts/check_style.py for the rules.\n", encoding="utf-8"
    )
    assert mine(state, repo) != []


def test_carries_concrete_file_rule():
    rule = skill_candidate_mining._carries_concrete_file
    assert not rule(("exec:python3", "exec:grep", "edit:scripts/*.py"))
    assert not rule(("exec:python3 -m pytest", "exec:git-commit"))
    assert rule(("exec:python3 scripts/x.py",))
    assert rule(("exec:python3 -m pytest tests/test_x.py",))
    assert rule(("read:scripts/x.py", "exec:grep"))
    assert rule(("exec:git-add lessons/errors.yaml",))
    assert not rule(("read:var/x.py",))


def test_trivial_set_is_not_extended():
    assert skill_candidate_mining._TRIVIAL == frozenset({
        ("exec:pytest", "exec:git-commit"),
        ("exec:pytest", "exec:git-commit", "exec:git-push"),
    })
