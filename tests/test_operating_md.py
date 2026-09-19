"""OPERATING.md — the cycle rules, as a release-owned file (#1723 part (a), ADR-022).

`OPERATING.md` answers question 5 of ADR-022's seven ("how does a cycle run,
including which tools exist"). It replaces literals scattered across
`nanobot/runtime/bridge.py:build_task` and runtime sections of the instance
`AGENTS.md` with one file, so each rule states once instead of the 6-8 times
measured on 2026-09-17.

Part (a) only: this file and its parity test. `build_task` itself is
untouched here (part (b), after #1727 merges and this part is deployed) --
so this suite does not import or exercise `build_task`.
"""
from __future__ import annotations

from pathlib import Path

from nanobot.runtime.mutation_policy import MUTATION_POLICY

RELEASE_ROOT = Path(__file__).resolve().parents[1]
OPERATING_MD = RELEASE_ROOT / "OPERATING.md"

MAX_CHARS = 5_000

# ADR-022's assembly order, section 5 (OPERATING.md): the ten headings this
# file must carry, in this exact order.
EXPECTED_HEADINGS = [
    "Cycle contract",
    "Mutation surface",
    "Before editing: skip check",
    "Execution",
    "Verification",
    "Termination",
    "Handoff",
    "Iteration budget",
    "Final response",
    "Tools",
]

RENDERED_FROM_MARKER = "<!-- rendered-from: mutation_policy -->"


def _read() -> str:
    return OPERATING_MD.read_text(encoding="utf-8")


def _headings(text: str) -> list[str]:
    return [ln[3:].strip() for ln in text.splitlines() if ln.startswith("## ")]


def _section(text: str, heading: str) -> str:
    """Body lines under ``## heading`` up to the next ``## `` heading, joined."""
    lines = text.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.strip() == f"## {heading}")
    body: list[str] = []
    for ln in lines[start + 1:]:
        if ln.startswith("## "):
            break
        body.append(ln)
    return "\n".join(body).strip("\n")


def test_operating_md_exists_non_empty_and_within_budget():
    assert OPERATING_MD.is_file(), "OPERATING.md missing from the release root"
    text = _read()
    assert text.strip(), "OPERATING.md is empty"
    assert len(text) <= MAX_CHARS, f"OPERATING.md is {len(text)} chars; budget {MAX_CHARS}"


def test_operating_md_has_the_ten_sections_in_order():
    headings = _headings(_read())
    assert headings == EXPECTED_HEADINGS, headings


def test_mutation_surface_section_matches_policy_render_byte_for_byte():
    """The section is generated, never hand-edited (#1723 constraint): drift
    between OPERATING.md and mutation_policy must fail this test, not ship."""
    text = _read()
    section = _section(text, "Mutation surface")
    assert section.startswith(RENDERED_FROM_MARKER), section[:80]
    body = section[len(RENDERED_FROM_MARKER):].lstrip("\n")

    # The render's own first line is its "## Mutation surfaces" heading
    # (note: plural, and it is OPERATING.md's own "## Mutation surface"
    # heading, singular, that already introduces this section) -- stripped
    # here so the file carries the heading exactly once, not twice.
    rendered = MUTATION_POLICY.render_bridge_surface_block()
    rendered_body = rendered.split("\n", 1)[1]

    assert body == rendered_body, (
        f"OPERATING.md's Mutation surface section has drifted from "
        f"MUTATION_POLICY.render_bridge_surface_block():\n--- file ---\n{body}\n"
        f"--- render ---\n{rendered_body}"
    )


def test_unittest_does_not_appear_as_a_runner_instruction():
    """#1723: the test runner is stated once, as pytest. AGENTS.md's own
    'standard-test-runner-unittest-over-pytest' skill pointer and the
    memory fact contradicting it are retired by a separate loop task, but
    this file must never repeat that contradiction."""
    assert "unittest" not in _read().lower()


def test_verification_section_names_pytest_and_the_exec_command():
    section = _section(_read(), "Verification")
    assert "pytest" in section
    assert 'exec("python3 -m pytest' in section


def test_json_contract_appears_exactly_once():
    text = _read()
    assert text.count('"action_taken"') == 1
    assert text.count('"concrete_next_action"') == 1
    # The contract is quoted once, here, and this file has no other JSON block.
    assert text.count("```") == 2


def test_no_identity_values_or_charter_prose():
    """Nothing about identity, values, or the charter belongs in this file
    (#1723 constraint) -- those are IDENTITY.md, SOUL.md, and goals.md."""
    text = _read()
    for phrase in ("You are", "Vector 1"):
        assert phrase not in text, phrase


def test_file_states_it_is_release_owned_and_not_committed_by_the_loop():
    text = _read()
    assert "release-owned" in text.lower()


def test_json_contract_reaches_loop_profile_prompt_but_not_build_task(tmp_path: Path):
    """#1723(b): once OPERATING.md is loaded into the system prompt (#1725),
    build_task must not repeat the JSON contract as a literal -- it appears
    exactly once, sourced from OPERATING.md, in the assembled loop-profile
    system prompt."""
    from nanobot.agent.context import ContextBuilder
    from nanobot.runtime.bridge import build_task

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "AGENTS.md").write_text("# Instance AGENTS.md\n\nRepo layout.", encoding="utf-8")
    prompt = ContextBuilder(workspace, release_root=RELEASE_ROOT).build_system_prompt(loop_profile=True)
    assert prompt.count('"concrete_next_action"') == 1

    req = {"task_title": "x", "request_id": "r", "cycle_id": "c", "goal_id": "g"}
    task_prompt = build_task(req, "goal", "")
    assert '"concrete_next_action"' not in task_prompt
    assert '"action_taken"' not in task_prompt
    assert "Rules: see OPERATING.md in your system prompt." in task_prompt


# ---------------------------------------------------------------------------
# #1767 -- the Tools section names the registry and its triggers
# ---------------------------------------------------------------------------

def test_tools_section_names_every_tool_in_the_executor_registry():
    """#1767: the section is the executor's only statement about tooling, so
    a tool it carries and the section never mentions is a capability the
    loop does not know it has -- which is how `search_memory` went 43 of 48
    cycles unused."""
    from nanobot.agent.tools.toolsets import EXECUTOR_TOOL_NAMES

    section = _section(_read(), "Tools")
    missing = [name for name in EXECUTOR_TOOL_NAMES if f"`{name}`" not in section]
    assert not missing, f"Tools section never names: {missing}"


def test_tools_section_describes_no_tool_the_executor_does_not_carry():
    """ADR-022: state guidance only where the capability exists. Describing
    `web_search` to a subagent whose registry has no such tool is an
    instruction it can only fail to follow."""
    from nanobot.agent.tools import toolsets

    section = _section(_read(), "Tools")
    known = set(toolsets.INTERACTIVE_TOOL_NAMES)
    absent = known - set(toolsets.EXECUTOR_TOOL_NAMES)
    described = sorted(name for name in absent if f"`{name}`" in section)
    assert not described, (
        f"Tools section describes tools absent from EXECUTOR_TOOL_NAMES: {described}"
    )


def test_tools_section_is_pinned_to_the_registry():
    """Adding or removing a tool without updating the text must fail here.

    Both directions in one assertion: the set of registry names the section
    mentions is exactly the registry. A new tool nobody documented fails the
    test above; a tool documented after its removal from the registry fails
    the one above that. This pins the pair so neither can drift silently."""
    from nanobot.agent.tools.toolsets import EXECUTOR_TOOL_NAMES

    section = _section(_read(), "Tools")
    mentioned = {name for name in EXECUTOR_TOOL_NAMES if f"`{name}`" in section}
    assert mentioned == set(EXECUTOR_TOOL_NAMES)


def test_search_memory_and_the_skills_catalogue_each_state_a_trigger():
    """#1767's finding: the catalogue lists what exists and nothing states
    when to go and get it. A name without a condition is the failure mode,
    not the fix, so both get an explicit trigger sentence."""
    section = _section(_read(), "Tools")

    search_line = next(ln for ln in section.splitlines() if "`search_memory`" in ln)
    assert "index" in search_line.lower(), search_line
    assert "when" in search_line.lower(), search_line

    skills_line = next(ln for ln in section.splitlines() if "**Skills**" in ln)
    assert "SKILL.md" in skills_line, skills_line
    assert "before starting" in skills_line, skills_line


def test_exec_bounds_survive_the_rewrite():
    """The 60s/10,000-char bounds are the one piece of the old section that
    states a fact the executor cannot get anywhere else."""
    section = _section(_read(), "Tools")
    assert "60-second" in section
    assert "10,000-character" in section


# ---------------------------------------------------------------------------
# #1783 -- headroom for the #1770 migration
# ---------------------------------------------------------------------------

def test_iteration_budget_section_keeps_all_four_rules():
    """#1783 compressed this section by 94 chars rather than raise a cap.
    Compression may not quietly drop a rule, so each is pinned."""
    section = _section(_read(), "Iteration budget")
    # where the number is stated
    assert "Iteration budget this cycle" in section
    assert "task section" in section
    # ADR-022 rule 4: the prompt-ontology harness fingerprints this block on
    # the literal phrase "tool iterations". A compression that paraphrases it
    # away leaves the rule with no owner -- caught by
    # test_fingerprint_once_per_system_block, and pinned here too so the
    # constraint is visible at the place a future edit happens.
    assert "tool iterations" in section
    # pace early
    assert "early" in section
    # verify on a candidate
    assert "candidate fix" in section
    # keep reserve for the commit and the final response
    assert "reserve" in section
    assert "final response" in section
    # do not circle
    assert "circle" in section


def test_operating_md_has_headroom_for_the_soul_migration():
    """#1770 must move two rules out of SOUL.md into this file (+153 chars).
    Measured here so the migration cannot discover mid-PR that it does not
    fit -- which is exactly what #1783 was filed about."""
    slack = MAX_CHARS - len(_read())
    assert slack >= 153, (
        f"only {slack} chars of headroom; #1770's migration needs 153. "
        f"Compress a section rather than raise the cap (#1783)."
    )
