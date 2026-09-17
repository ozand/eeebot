"""#1725 (ADR-022): the file-driven loop-profile loader.

``ContextBuilder.BOOTSTRAP_FILES`` is an ordered ``(root_kind, filename,
cap, required)`` block list — release-owned (IDENTITY.md, SOUL.md,
goals.md, USER.md, OPERATING.md) then workspace-owned (AGENTS.md) — loaded
by ``ContextBuilder.load_block`` and assembled by
``ContextBuilder._build_loop_system_prompt`` in that order, followed by the
skills catalogue, memory, and code-generated runtime facts. These tests
exercise the loader itself (missing/truncated handling, block order,
telemetry) against a real fixture release root + workspace — the generic
fit/strict/droppable ladder has its own tests in
``tests/test_context_prompt_fit.py`` and ``tests/test_system_prompt_sections.py``.
"""
from __future__ import annotations

import re
from pathlib import Path

from nanobot.agent.context import ContextBuilder

_CONTEXT_SRC = Path(__file__).resolve().parents[1] / "nanobot" / "agent" / "context.py"
_SUBAGENT_SRC = Path(__file__).resolve().parents[1] / "nanobot" / "agent" / "subagent.py"

#: #1725 constraint: no prose literal for role, behaviour or procedure
#: survives in context.py/subagent.py. build_task's own literals
#: ("Branch discipline (MANDATORY)") are #1723's to remove, not this
#: issue's — checked here only as a fingerprint that the LOOP profile
#: (context.py/subagent.py) never emits them, not that the string is
#: absent from the whole repo.
_FORBIDDEN_LOOP_PROSE = (
    "clarification",
    "'message' tool",
    "web_fetch",
    "web_search",
    "You are the autonomous improvement agent",
)


def _make_release_and_workspace(tmp_path: Path, *, omit: tuple[str, ...] = ()) -> tuple[Path, Path]:
    release = tmp_path / "release"
    workspace = tmp_path / "workspace"
    release.mkdir()
    workspace.mkdir()
    for name in ("IDENTITY.md", "SOUL.md", "goals.md", "USER.md", "OPERATING.md"):
        if name in omit:
            continue
        (release / name).write_text(f"{name} placeholder content.", encoding="utf-8")
    if "AGENTS.md" not in omit:
        (workspace / "AGENTS.md").write_text("# Instance AGENTS.md\n\nRepo layout.", encoding="utf-8")
    return release, workspace


def test_block_order_identity_to_runtime_each_with_its_own_heading(tmp_path):
    """AC: identity -> soul -> goals -> user -> operating -> AGENTS.md ->
    skills -> memory -> runtime, each introduced by its own heading."""
    release, workspace = _make_release_and_workspace(tmp_path)
    builder = ContextBuilder(workspace, release_root=release)
    prompt = builder.build_system_prompt(loop_profile=True)

    headings = re.findall(r"^(#{1,2} .+)$", prompt, re.MULTILINE)
    # Ordered subsequence check: every expected heading appears, in order,
    # not necessarily contiguous (the skills/memory sections may add their
    # own '# Skills'/'# Memory' headings in between).
    expected = ["## IDENTITY.md", "## SOUL.md", "## goals.md", "## USER.md", "## OPERATING.md", "## AGENTS.md", "## Runtime"]
    positions = [headings.index(h) for h in expected]
    assert positions == sorted(positions), f"headings out of order: {headings}"

    assert list(builder.last_fit["sections"]) == [
        "identity", "soul", "goals", "user", "operating", "agents",
        "skills_catalogue", "memory", "runtime",
    ]


def test_telemetry_blocks_sum_to_message_length(tmp_path):
    """AC: the ledger system_prompt row lists the same blocks with chars
    summing to the message length."""
    release, workspace = _make_release_and_workspace(tmp_path)
    builder = ContextBuilder(workspace, release_root=release)
    prompt = builder.build_system_prompt(loop_profile=True)
    fit = builder.last_fit

    sizes = fit["sections"]
    n_nonempty = sum(1 for v in sizes.values() if v > 0)
    gaps = max(0, n_nonempty - 1)
    expected = sum(sizes.values()) + len(ContextBuilder.SECTION_SEPARATOR) * gaps
    assert expected == fit["chars"] == len(prompt)


def test_missing_required_file_produces_marker_and_telemetry_flag(tmp_path):
    """AC: removing SOUL.md from a test release tree produces
    '[missing: SOUL.md]' in the prompt and missing: ['SOUL.md'] in the
    telemetry row; the cycle still runs (build_system_prompt does not
    raise)."""
    release, workspace = _make_release_and_workspace(tmp_path, omit=("SOUL.md",))
    builder = ContextBuilder(workspace, release_root=release)
    prompt = builder.build_system_prompt(loop_profile=True)

    assert "[missing: SOUL.md]" in prompt
    assert builder.last_fit["missing"] == ["SOUL.md"]
    assert builder.last_fit["truncated"] == []
    # every other required block still rendered — a fail-open loader, not a
    # fail-closed one.
    assert "[missing: IDENTITY.md]" not in prompt
    assert "[missing: AGENTS.md]" not in prompt


def test_no_release_root_marks_every_release_block_missing(tmp_path):
    """release_root=None (every caller but the self-evolving bridge): every
    release block renders '[missing: <name>]', the same degrade path a
    genuinely absent file takes, not a special case."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "AGENTS.md").write_text("# Instance AGENTS.md", encoding="utf-8")
    builder = ContextBuilder(workspace, release_root=None)
    prompt = builder.build_system_prompt(loop_profile=True)

    for name in ("IDENTITY.md", "SOUL.md", "goals.md", "USER.md", "OPERATING.md"):
        assert f"[missing: {name}]" in prompt
    assert sorted(builder.last_fit["missing"]) == sorted(
        ["IDENTITY.md", "SOUL.md", "goals.md", "USER.md", "OPERATING.md"]
    )
    assert "[missing: AGENTS.md]" not in prompt


def test_oversized_agents_md_truncated_with_notice_exactly_once(tmp_path):
    """AC: a 30,000-char AGENTS.md fixture is truncated at its cap with the
    built-in notice, and the notice text appears in the prompt exactly
    once."""
    release, workspace = _make_release_and_workspace(tmp_path, omit=("AGENTS.md",))
    (workspace / "AGENTS.md").write_text(
        "# Instance AGENTS.md\n\n" + ("Repo layout line.\n" * 2000), encoding="utf-8",
    )
    assert (workspace / "AGENTS.md").stat().st_size > 30_000

    builder = ContextBuilder(workspace, release_root=release)
    prompt = builder.build_system_prompt(loop_profile=True)

    notice = "AGENTS.md truncated at 4000 chars; read the file for the rest"
    assert prompt.count(notice) == 1
    assert builder.last_fit["truncated"] == ["AGENTS.md"]
    assert builder.last_fit["missing"] == []
    assert builder.last_fit["sections"]["agents"] <= 4000


def test_load_block_never_exceeds_cap_and_marks_truncated():
    """Unit-level: load_block's own contract, independent of the full
    builder — heading + kept body + notice never exceed cap."""
    class _FakePath:
        def __init__(self, text: str):
            self._text = text

        def is_file(self) -> bool:
            return True

        def read_text(self, encoding: str = "utf-8") -> str:
            return self._text

    text, meta = ContextBuilder.load_block("SOUL.md", _FakePath("x" * 10_000), 500, True)
    assert len(text) <= 500
    assert meta["truncated"] is True
    assert meta["missing"] is False
    assert "SOUL.md truncated at 500 chars; read the file for the rest" in text


def test_active_skills_absent_from_loop_profile_prompt_and_telemetry(tmp_path):
    """AC: active_skills no longer appears in loop-profile prompts or
    telemetry."""
    release, workspace = _make_release_and_workspace(tmp_path)
    builder = ContextBuilder(workspace, release_root=release)
    prompt = builder.build_system_prompt(loop_profile=True)

    assert "active_skills" not in builder.last_fit["sections"]
    assert "# Active Skills" not in prompt


def test_no_forbidden_loop_prose_in_context_or_subagent_source():
    """Constraint: a test greps for the current sentences ('Ask for
    clarification', "Only use the 'message' tool", web_fetch/web_search
    guidance, the loop role sentence) and fails if any is found in the
    Python source that assembles the LOOP profile. build_task's own
    literals (Branch discipline etc.) are #1723's, not checked here."""
    context_src = _CONTEXT_SRC.read_text(encoding="utf-8")
    subagent_src = _SUBAGENT_SRC.read_text(encoding="utf-8")

    # The interactive profile legitimately keeps its own template (ADR-022:
    # "the interactive profile may keep its own template, it must not share
    # the loop's text") — so this asserts the phrases are confined to the
    # INTERACTIVE branch of _get_identity, not absent from the file
    # entirely. Locate the loop-profile branch (returns before the
    # interactive template) and check only that slice.
    loop_branch_start = context_src.index("if loop_profile:\n            return f\"\"\"## Runtime")
    loop_branch_end = context_src.index("platform_policy = \"\"", loop_branch_start)
    loop_branch = context_src[loop_branch_start:loop_branch_end]
    for phrase in _FORBIDDEN_LOOP_PROSE:
        assert phrase not in loop_branch, f"{phrase!r} found in the loop-profile runtime block"

    # subagent.py never authors any GUIDANCE prose at all (it only assembles
    # via ContextBuilder / appends an operator-supplied system_context).
    # "web_fetch"/"web_search" are excluded here: they legitimately appear
    # as tool-registry names (registered_tool_names, EXECUTOR_TOOL_NAMES),
    # not as prose telling the executor about a tool it lacks — ADR-022
    # rule 5 is about guidance text, not the registration code that grants
    # the capability in the first place.
    for phrase in ("clarification", "'message' tool", "You are the autonomous improvement agent"):
        assert phrase not in subagent_src, f"{phrase!r} found in subagent.py"
