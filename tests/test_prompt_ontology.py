"""ADR-022 rules 4 and 5 (#1726): the loop-profile prompt ontology harness.

Four assertions over the assembled loop-profile system prompt (built from
the release-owned files at the repo root -- ``IDENTITY.md``, ``SOUL.md``,
``goals.md``, ``USER.md``, ``OPERATING.md`` -- plus a fixture instance
workspace) and, where noted, over ``nanobot.runtime.bridge.build_task``'s
volatile user message:

1. **Fingerprint once** -- each named rule (skip, mutation surface, branch
   discipline, test runner, identity, iteration budget, final-response JSON)
   matches in exactly one system block.
2. **AGENTS.md scope** -- the instance ``AGENTS.md`` stays within
   ``MUTATION_POLICY.agents_md_max_lines`` lines and carries none of
   ``MUTATION_POLICY.agents_md_runtime_headings``.
3. **Surface parity** -- ``OPERATING.md``'s ``## Mutation surface`` section
   equals ``MUTATION_POLICY.render_bridge_surface_block()`` byte for byte,
   and ``Allowed targets:`` appears nowhere else.
4. **No guidance without capability** -- every tool name mentioned in the
   assembled prompt's prose is one of ``EXECUTOR_TOOL_NAMES``; the
   ``search_memory`` status contract belongs to the tool's own description,
   not to surrounding prose.

Regexes are proven against the REAL release files at the repo root, not
copied from the issue's draft table verbatim -- the issue's own draft
``never (checkout|push)`` for "branch" does not match ``OPERATING.md``'s
real sentence ("Never run `git checkout`... and never run `git push`"), so
it is widened here to the real text, per the issue's own instruction to
grep before fixing a regex.

Assertion 1's system-block half, and assertion 3, pass today against the
checked-in release files (``OPERATING.md`` already carries decision 6 from
PR #1731). Assertion 1's user-message half is `xfail(strict=True)` pending
#1723 part (b) (``build_task`` still carries its own skip/surface/branch/
runner/final-JSON literals). Assertion 2's check against the REAL instance
``AGENTS.md`` fixture (192 lines, 8 runtime headings today) is
`xfail(strict=True)` pending #1730 part 1 -- the ontology-fixture-based
half of assertion 1 (and the compliant-fixture half of assertion 2) instead
use a small hand-written target-shape ``AGENTS.md`` fixture, per the ADR-022
orchestrator revision (2026-09-18): "until then the harness test must run
against the fixture release root + a fixture instance AGENTS.md, with test
2 xfail(strict) as agreed." This module additionally found, while proving
assertion 4, that the ``search_memory`` status contract is ALSO restated in
``nanobot/agent/memory.py``'s ``MemoryStore.MEMORY_SEARCH_POINTER`` (folded
into the loop's memory block whenever ``memory/index.md`` is non-trivial),
duplicating the same contract already stated in ``OPERATING.md``'s ``##
Tools`` section -- a real ADR-022 rule 5 violation this module cannot fix
without editing the protected ``OPERATING.md`` or the shared
``nanobot/agent/memory.py``, so it is also `xfail(strict=True)`, flagged as
a new finding in the PR body pending a follow-up issue.
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

from nanobot.agent.context import ContextBuilder
from nanobot.agent.tools.toolsets import EXECUTOR_TOOL_NAMES
from nanobot.runtime.bridge import build_task
from nanobot.runtime.mutation_policy import MUTATION_POLICY

from tests.test_operating_md import (
    RENDERED_FROM_MARKER,
    _read as _read_operating_md,
    _section as _operating_section,
)

RELEASE_ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "instance_agents_md"
#: The REAL instance AGENTS.md, refreshed from the instance repository with:
#:   gh api repos/ozand/eeebot-self-evolving/contents/AGENTS.md --jq .content \
#:     | base64 -d > tests/fixtures/instance_agents_md/AGENTS.md
#: 192 lines / 8 runtime headings as of 2026-09-18 -- used only by the
#: xfail(strict) case of assertion 2, which tracks the real file's shape.
REAL_INSTANCE_AGENTS_MD = FIXTURES_DIR / "AGENTS.md"
MEMORY_INDEX_FIXTURE = FIXTURES_DIR / "memory_index.md"

#: A target-shape instance AGENTS.md: repository layout only, none of the
#: runtime-rule headings OPERATING.md now owns. #1730 part 1 will bring the
#: real file down to roughly this shape; the fingerprint test (assertion 1)
#: and the compliant half of assertion 2 are built against this fixture
#: rather than the real 192-line file, per the ADR-022 orchestrator revision.
_CLEAN_AGENTS_MD = """# Instance Agent Instructions

## Repository layout

This is the self-evolving loop's own instance workspace on the `eeepc` host
(i386 Debian 12, 2 GB RAM, Python 3.11), mirroring
`ozand/eeebot-self-evolving`. `surfaces/`, `scripts/`, `memory/`,
`lessons/`, `docs/`, `tests/`, `skills/` are the loop's own commit
surfaces; `ops/`, `state/`, `systemd/` are the live control plane and are
read-only.
"""


def _make_workspace(tmp_path: Path, *, agents_md: str = _CLEAN_AGENTS_MD, with_memory: bool = False) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "AGENTS.md").write_text(agents_md, encoding="utf-8")
    if with_memory:
        memory_dir = workspace / "memory"
        memory_dir.mkdir()
        shutil.copy(MEMORY_INDEX_FIXTURE, memory_dir / "index.md")
    return workspace


#: ADR-022 assembly order (rule 3) with the heading that opens each block in
#: the rendered prompt -- mirrors the ordered-subsequence check in
#: tests/test_ontology_loader.py::test_block_order_identity_to_runtime_each_with_its_own_heading.
_BLOCK_HEADINGS: list[tuple[str, str]] = [
    ("identity", "## IDENTITY.md"),
    ("soul", "## SOUL.md"),
    ("goals", "## goals.md"),
    ("user", "## USER.md"),
    ("operating", "## OPERATING.md"),
    ("agents", "## AGENTS.md"),
    ("skills_catalogue", "# Skills"),
    ("memory", "# Memory"),
    ("runtime", "## Runtime"),
]


def _split_blocks(prompt: str) -> dict[str, str]:
    """Split the assembled loop-profile prompt into its ADR-022 blocks by
    their own headings, in the fixed assembly order. A block whose heading
    never appears (an empty skills/memory section, for instance) maps to
    ``""`` rather than being omitted, so callers can iterate every name."""
    positions: list[tuple[int, str]] = []
    search_from = 0
    for name, heading in _BLOCK_HEADINGS:
        idx = prompt.find(heading, search_from)
        if idx == -1:
            continue
        positions.append((idx, name))
        search_from = idx + len(heading)
    blocks: dict[str, str] = {name: "" for name, _ in _BLOCK_HEADINGS}
    for i, (idx, name) in enumerate(positions):
        end = positions[i + 1][0] if i + 1 < len(positions) else len(prompt)
        blocks[name] = prompt[idx:end]
    return blocks


#: Mirrors nanobot/runtime/bridge.py's own local ``_LOOP_EXCLUDED_SKILLS``
#: (builtins irrelevant to the self-evolving loop, bridge-side, not
#: instance-controlled): without excluding them here too, the fixture
#: skills catalogue would carry names -- "cron" foremost -- the real loop
#: never sees, which is exactly the false positive assertion 4 exists to
#: rule out, not manufacture.
_LOOP_EXCLUDED_SKILLS = ["weather", "tmux", "clawhub", "cron", "summarize", "github"]


def _build_prompt(tmp_path: Path, **workspace_kwargs) -> dict[str, str]:
    workspace = _make_workspace(tmp_path, **workspace_kwargs)
    builder = ContextBuilder(workspace, release_root=RELEASE_ROOT)
    prompt = builder.build_system_prompt(loop_profile=True, excluded_skill_names=_LOOP_EXCLUDED_SKILLS)
    return _split_blocks(prompt)


# ---------------------------------------------------------------------------
# Assertion 1: fingerprint once.
# ---------------------------------------------------------------------------

#: Proven (see module docstring) against the real release files at the repo
#: root: each matches exactly the ``operating`` block of the assembled
#: prompt built from RELEASE_ROOT + a clean fixture AGENTS.md, and exactly
#: the ``identity`` block for "identity".
RULE_FINGERPRINTS: dict[str, re.Pattern[str]] = {
    "skip": re.compile(r"outcome:?\s*[\"']?skipped", re.IGNORECASE),
    "mutation_surface": re.compile(r"Allowed targets:"),
    # Widened from the issue's draft `never (checkout|push)`, which does not
    # match OPERATING.md's real sentence ("Never run `git checkout`, `git
    # switch`, or `git branch`, and never run `git push`").
    "branch": re.compile(r"never run [`\"']?git (checkout|switch|branch|push)", re.IGNORECASE),
    "runner": re.compile(r"python3 -m pytest|\bunittest\b", re.IGNORECASE),
    # Widened from the issue's draft `You are the`, which does not match
    # IDENTITY.md's real sentence ("You are eeebot, the autonomous
    # improvement agent...") -- and would also false-positive on SOUL.md's
    # "You are an honest instrument" if left as the bare word "You are".
    "identity": re.compile(r"You are eeebot"),
    "budget": re.compile(r"tool iterations", re.IGNORECASE),
    "final_json": re.compile(r'"concrete_next_action"'),
}

#: The only fingerprint the volatile user message is allowed to carry
#: (the iteration-count number/phrase); every other rule literal in
#: build_task's message is #1723 part (b)'s to remove.
_ALLOWED_IN_USER_MESSAGE = {"budget"}


def _assert_fingerprint_once(rule: str, rx: re.Pattern[str], blocks: dict[str, str]) -> None:
    matches = [name for name, text in blocks.items() if text and rx.search(text)]
    assert len(matches) == 1, (
        f"rule {rule!r} ({rx.pattern!r}) matched {len(matches)} block(s) {matches}; "
        "ADR-022 rule 4 requires exactly one owner"
    )


def test_fingerprint_once_per_system_block(tmp_path: Path):
    """AC: each named rule regex matches in exactly one system block of the
    assembled prompt built from the real release root files plus a
    target-shape instance AGENTS.md fixture."""
    blocks = _build_prompt(tmp_path)
    for rule, rx in RULE_FINGERPRINTS.items():
        _assert_fingerprint_once(rule, rx, blocks)


def test_fingerprint_duplicate_is_caught_naming_both_blocks(tmp_path: Path):
    """AC: a deliberate duplicate (the skip rule restated in AGENTS.md, on
    top of its real OPERATING.md home) fails, naming both blocks and the
    count -- proves the checker actually looks, rather than degrading into
    ``assert True``."""
    duplicated_agents_md = _CLEAN_AGENTS_MD + (
        "\n## Before you edit\n\n"
        'If the task is already done, report `outcome: "skipped"` and stop.\n'
    )
    blocks = _build_prompt(tmp_path, agents_md=duplicated_agents_md)

    with pytest.raises(AssertionError) as excinfo:
        _assert_fingerprint_once("skip", RULE_FINGERPRINTS["skip"], blocks)
    message = str(excinfo.value)
    assert "operating" in message and "agents" in message
    assert "2 block" in message


def test_fingerprint_user_message_only_budget_and_task():
    """AC (assertion 1, user-message half): build_task's user message may
    match only the budget number/phrase and the task -- every other rule
    literal (skip, mutation surface, branch, runner, final JSON) is
    #1723(b)'s to remove from build_task. GREEN since #1742 (`023a38f7`)
    removed those literals; the xfail(strict) marker this test carried
    while part (b) was open did its job and was dropped on the rebase."""
    req = {"task_title": "some task", "request_id": "r1", "cycle_id": "c1", "goal_id": "g1"}
    task = build_task(req, "mission text", "report_source.json")

    violations = {
        rule: rx.pattern
        for rule, rx in RULE_FINGERPRINTS.items()
        if rule not in _ALLOWED_IN_USER_MESSAGE and rx.search(task)
    }
    assert not violations, f"user message carries rule literals beyond budget/task: {violations}"


# ---------------------------------------------------------------------------
# Assertion 2: AGENTS.md scope.
# ---------------------------------------------------------------------------


def test_agents_md_scope_violations_passes_for_compliant_fixture():
    """AC: a fixture text of <=150 lines with none of the runtime headings
    passes."""
    assert MUTATION_POLICY.agents_md_scope_violations(_CLEAN_AGENTS_MD) == []


def test_agents_md_scope_violations_flags_over_length_fixture():
    over_length = "\n".join(f"line {i}" for i in range(MUTATION_POLICY.agents_md_max_lines + 10))
    violations = MUTATION_POLICY.agents_md_scope_violations(over_length)
    assert any("exceeds" in v for v in violations), violations


def test_agents_md_scope_violations_flags_a_runtime_heading():
    heading = MUTATION_POLICY.agents_md_runtime_headings[0]
    text = f"# Instance Agent Instructions\n\n{heading}\n\nBody.\n"
    violations = MUTATION_POLICY.agents_md_scope_violations(text)
    assert any(heading in v for v in violations), violations


@pytest.mark.xfail(strict=True, reason="#1730 part 1 pending")
def test_agents_md_scope_violations_against_real_instance_fixture():
    """AC: the real instance AGENTS.md fixture (192 lines, 8 runtime
    headings as of 2026-09-18 -- see FIXTURES_DIR docstring for the refresh
    command) passes the same bound the #1731 gate applies at HEAD. RED
    today; flips to a hard failure, not a silent pass, the day #1730 part 1
    shrinks the real file and this marker is forgotten."""
    text = REAL_INSTANCE_AGENTS_MD.read_text(encoding="utf-8")
    assert MUTATION_POLICY.agents_md_scope_violations(text) == []


# ---------------------------------------------------------------------------
# Assertion 3: surface parity.
# ---------------------------------------------------------------------------


def _surface_diff(operating_md_text: str) -> "str | None":
    """Return a diff string if *operating_md_text*'s ``## Mutation surface``
    section has drifted from ``MUTATION_POLICY.render_bridge_surface_block()``,
    else ``None``. Factored out of tests.test_operating_md's own
    byte-for-byte assertion (via its ``_section`` helper, imported rather
    than reimplemented) so a mutated copy can be checked without touching
    the real, protected OPERATING.md file."""
    section = _operating_section(operating_md_text, "Mutation surface")
    if not section.startswith(RENDERED_FROM_MARKER):
        return f"missing {RENDERED_FROM_MARKER!r} marker:\n{section[:200]}"
    body = section[len(RENDERED_FROM_MARKER):].lstrip("\n")
    rendered_body = MUTATION_POLICY.render_bridge_surface_block().split("\n", 1)[1]
    if body != rendered_body:
        return f"--- file ---\n{body}\n--- render ---\n{rendered_body}"
    return None


def test_surface_parity_real_operating_md_matches_render():
    assert _surface_diff(_read_operating_md()) is None


def test_surface_parity_mutated_operating_md_fails_with_diff():
    """AC: a mutated OPERATING.md surface section fails the parity test
    with a diff."""
    mutated = _read_operating_md().replace(
        "Allowed targets: surfaces/, scripts/, memory/",
        "Allowed targets: surfaces/, scripts/, memory/, EXTRA_UNAUTHORIZED_PATH/",
    )
    assert mutated != _read_operating_md()  # the replace actually landed
    diff = _surface_diff(mutated)
    assert diff is not None
    assert "EXTRA_UNAUTHORIZED_PATH" in diff


def test_surface_allowed_targets_appears_only_in_operating_md_at_release_root():
    marker = "Allowed targets:"
    offenders = [
        path.name
        for path in sorted(RELEASE_ROOT.glob("*.md"))
        if path.name != "OPERATING.md" and marker in path.read_text(encoding="utf-8")
    ]
    assert offenders == [], f"{marker!r} also found in: {offenders}"


def test_surface_allowed_targets_appears_only_in_operating_block_of_prompt(tmp_path: Path):
    blocks = _build_prompt(tmp_path)
    _assert_fingerprint_once("mutation_surface", RULE_FINGERPRINTS["mutation_surface"], blocks)
    assert [name for name, text in blocks.items() if "Allowed targets:" in text] == ["operating"]


# ---------------------------------------------------------------------------
# Assertion 4: no guidance without capability.
# ---------------------------------------------------------------------------

#: Tools an executor was never given; naming them in prose is exactly the
#: defect ADR-022 rule 5 closes.
_FORBIDDEN_CAPABILITY_WORDS = ("message", "web_fetch", "web_search", "spawn", "cron")
_ALL_CANDIDATE_TOOL_WORDS = tuple(_FORBIDDEN_CAPABILITY_WORDS) + tuple(EXECUTOR_TOOL_NAMES)

#: The search_memory status contract, worded loosely enough to match both
#: its home in the tool's own description (nanobot/agent/tools/memory_search.py)
#: and its accidental restatements (OPERATING.md's "## Tools" section;
#: memory.py's MEMORY_SEARCH_POINTER).
_SEARCH_MEMORY_CONTRACT = re.compile(r"complete.{0,15}partial.{0,15}(or\s+)?unavailable", re.IGNORECASE)


def _mentioned_tool_words(text: str) -> set[str]:
    return {word for word in _ALL_CANDIDATE_TOOL_WORDS if re.search(rf"\b{re.escape(word)}\b", text)}


def test_prompt_prose_names_only_executor_registry_tools(tmp_path: Path):
    """AC: every tool name mentioned in the assembled prompt's prose is in
    EXECUTOR_TOOL_NAMES; none of message/web_fetch/web_search/spawn/cron
    appears (the executor was never given those tools)."""
    blocks = _build_prompt(tmp_path)
    prompt = "".join(blocks.values())
    mentioned = _mentioned_tool_words(prompt)
    forbidden_mentioned = mentioned & set(_FORBIDDEN_CAPABILITY_WORDS)
    assert not forbidden_mentioned, f"prompt names tools the executor lacks: {forbidden_mentioned}"
    assert mentioned <= set(EXECUTOR_TOOL_NAMES), mentioned - set(EXECUTOR_TOOL_NAMES)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "search_memory contract duplicated: OPERATING.md's '## Tools' section "
        "and nanobot/agent/memory.py's MemoryStore.MEMORY_SEARCH_POINTER both "
        "restate 'complete/partial/unavailable' in the assembled loop memory "
        "block; fixing needs the protected OPERATING.md or the shared memory.py "
        "module (out of #1726 scope) -- new finding, follow-up issue not yet filed"
    ),
)
def test_search_memory_status_contract_appears_only_in_operating_tools_block(tmp_path: Path):
    """AC (assertion 4, second half): the search_memory status contract
    lives in the tool's own description, not restated in surrounding prose
    -- specifically not in the memory block. Uses a workspace with a
    populated memory/index.md so MemoryStore's MEMORY_SEARCH_POINTER is
    actually exercised, not vacuously absent."""
    blocks = _build_prompt(tmp_path, with_memory=True)
    matches = [name for name, text in blocks.items() if text and _SEARCH_MEMORY_CONTRACT.search(text)]
    assert matches == ["operating"], (
        f"search_memory status contract found in {matches}; expected only 'operating' "
        "(OPERATING.md's '## Tools' section) -- see this test's xfail reason"
    )
