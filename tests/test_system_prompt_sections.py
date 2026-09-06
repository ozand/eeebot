"""#1379: every system-prompt fit now names all five sections (identity,
bootstrap, active_skills, skills_catalogue, memory), in build order, on
``ContextBuilder.last_fit["sections"]`` and on
``SystemPromptOverflowError.sections`` -- including a section that ended up
legitimately empty (0) or was removed by the fit (also 0). "Never existed"
is the only thing 0 does NOT mean, because the key is always present.

This pins the reconciliation invariant the issue is built around:

    sum(sizes.values()) + len(SECTION_SEPARATOR) * max(0, n_nonempty - 1)
        == chars == len(prompt)

(``n_nonempty`` = how many of the five recorded sizes are > 0 -- one
separator per *gap* between assembled non-empty sections, not one per
section). ``test_healthy_build_sections_reconcile`` (case a) is a
regression pin: it fails on pre-#1379 main, where ``last_fit`` for a
fitting prompt has no ``"sections"`` key at all -- the key was only ever
populated on the overflow exception.

Reuses ``tests/test_context_prompt_fit.py``'s ``_builder``/``_section``
fixture-builders (droppable-section bootstrap fixed by ``_section(...,
droppable=True)`` + ``ContextBuilder.DROPPABLE_MARKER``) and
``tests/test_cycle_ledger.py``'s bridge-integration fixtures (same fakes
``tests/test_bridge_system_prompt_overflow.py`` drives).
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path

import pytest

from nanobot import crash_record
from nanobot.agent.context import ContextBuilder, SystemPromptOverflowError
from nanobot.runtime import bridge
from tests.test_context_prompt_fit import MARK, _builder, _section
from tests.test_cycle_ledger import (
    _FakeSubagentManager,
    _init_selfevo_repo,
    _read_ledger,
    _seed_bridge_request,
)

SECTION_NAMES = ["identity", "bootstrap", "active_skills", "skills_catalogue", "memory"]

DASHBOARD_PATH = Path(__file__).resolve().parents[1] / "scripts" / "eeebot_dashboard.py"
_SPEC = importlib.util.spec_from_file_location("eeebot_dashboard", DASHBOARD_PATH)
assert _SPEC and _SPEC.loader
DASHBOARD = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(DASHBOARD)


def _assert_reconciles(fit: dict, prompt: str | None = None) -> None:
    """The arithmetic pin: section sizes plus one separator per *gap* between
    non-empty sections must equal the recorded ``chars`` (and, when a prompt
    string is given, its actual length too). Raises ``AssertionError`` like
    any other assert when it does not -- used both as an ordinary helper and,
    in ``test_reconciliation_helper_catches_a_future_hidden_section``, as the
    thing under test.
    """
    sizes = fit["sections"]
    n_nonempty = sum(1 for v in sizes.values() if v > 0)
    gaps = max(0, n_nonempty - 1)
    expected = sum(sizes.values()) + len(ContextBuilder.SECTION_SEPARATOR) * gaps
    assert expected == fit["chars"], (
        f"sum(sections)={sum(sizes.values())} + {gaps} separators = {expected}, "
        f"but chars={fit['chars']} ({sizes})"
    )
    if prompt is not None:
        assert fit["chars"] == len(prompt)


def _expected_section_texts(builder: ContextBuilder, *, loop_profile: bool) -> dict[str, str]:
    """Mirrors ``ContextBuilder.build_system_prompt``'s section assembly
    exactly, so a test can compare the builder's own ``last_fit["sections"]``
    sizes against the actual text it must have produced -- independent of the
    prompt string itself."""
    identity = builder._get_identity(loop_profile=loop_profile)
    bootstrap = builder._load_bootstrap_files() or ""
    always_skills = builder.skills.get_always_skills()
    if loop_profile:
        always_skills = [n for n in always_skills if n != "memory"]
    always_content = builder.skills.load_skills_for_context(always_skills) if always_skills else ""
    active_skills = f"# Active Skills\n\n{always_content}" if always_content else ""
    skills_summary = builder.skills.build_skills_summary(excluded_names=None)
    skills_catalogue = (f"""# Skills

The following skills extend your capabilities. To use a skill, read the skill's SKILL.md file using the read_file tool.
Skills with available="false" need dependencies installed first - you can try installing them with apt/brew.

{skills_summary}""" if skills_summary else "")
    memory = builder.memory.get_memory_context(loop=loop_profile)
    memory_section = f"# Memory\n\n{memory}" if memory else ""
    return {
        "identity": identity, "bootstrap": bootstrap, "active_skills": active_skills,
        "skills_catalogue": skills_catalogue, "memory": memory_section,
    }


# ─── (a) healthy build ──────────────────────────────────────────────────────


def test_healthy_build_sections_reconcile(tmp_path):
    """Regression pin (fails on pre-#1379 main): a fitting prompt's
    ``last_fit`` carries all five section sizes, in build order -- including
    the empty ``active_skills`` -- and they reconcile with chars/len(prompt).
    On main, ``last_fit`` has no ``"sections"`` key at all for a prompt under
    the cap; ``"active_skills"`` is also absent there whenever no
    always-skill content was assembled, rather than present as 0.
    """
    builder = _builder(tmp_path, _section("Working knowledge", 3))
    prompt = builder.build_system_prompt(loop_profile=True)
    fit = builder.last_fit

    assert list(fit["sections"]) == SECTION_NAMES, "present, in build order, even when empty"
    assert fit["sections"]["active_skills"] == 0, "no always-skills content for this builder"

    expected_texts = _expected_section_texts(builder, loop_profile=True)
    for name, text in expected_texts.items():
        assert fit["sections"][name] == len(text), name

    _assert_reconciles(fit, prompt)


# ─── (b) reconciliation across every fit outcome ────────────────────────────


def test_strict_drop_reconciles_post_drop_bootstrap_length(tmp_path, monkeypatch):
    """(i) strict fit that drops a declared-droppable bootstrap section: the
    recorded bootstrap size reflects the text left standing AFTER the drop,
    not the originally-assembled bootstrap."""
    monkeypatch.setattr(ContextBuilder, "MAX_SYSTEM_PROMPT_CHARS", 6_000)
    bootstrap = (
        _section("Working knowledge", 6)
        + _section("Big optional appendix", 60, droppable=True)
        + _section("Small optional note", 5, droppable=True)
        + _section("Standard test runner", 8)
    )
    builder = _builder(tmp_path, bootstrap)
    prompt = builder.build_system_prompt(loop_profile=True)
    fit = builder.last_fit

    assert fit["dropped"], "the big appendix must have gone for this to be a meaningful check"
    original_bootstrap = builder._load_bootstrap_files()
    dropped_chars = sum(d["chars"] for d in fit["dropped"])
    assert fit["sections"]["bootstrap"] == len(original_bootstrap) - dropped_chars

    _assert_reconciles(fit, prompt)


def test_strict_overflow_sections_reconcile(tmp_path, monkeypatch):
    """(ii) strict overflow: ``exc.sections`` has all five keys and
    reconciles to ``exc.cap + exc.over_by`` (there is no returned prompt to
    compare against -- the build failed)."""
    monkeypatch.setattr(ContextBuilder, "MAX_SYSTEM_PROMPT_CHARS", 4_000)
    bootstrap = _section("Working knowledge", 40) + _section("Optional", 4, droppable=True) + _section("Standard test runner", 40)
    builder = _builder(tmp_path, bootstrap)
    with pytest.raises(SystemPromptOverflowError) as info:
        builder.build_system_prompt(loop_profile=True)
    exc = info.value

    assert set(exc.sections) == set(SECTION_NAMES)
    _assert_reconciles({"sections": exc.sections, "chars": exc.cap + exc.over_by})


def test_non_strict_trim_reconciles(tmp_path, monkeypatch):
    """(iii) non-strict (interactive) trim path that line-trims bootstrap:
    sizes reconcile with chars/len(prompt) same as the strict paths."""
    monkeypatch.setattr(ContextBuilder, "MAX_SYSTEM_PROMPT_CHARS", 3_000)
    builder = _builder(tmp_path, _section("Working knowledge", 80))
    prompt = builder.build_system_prompt(loop_profile=False)
    fit = builder.last_fit

    assert fit["strict"] is False
    assert fit["dropped"][0]["section"] == "bootstrap" and fit["dropped"][0]["how"] == "line-trim"
    _assert_reconciles(fit, prompt)


def test_non_strict_removes_an_unsplittable_oversized_section_entirely(tmp_path, monkeypatch):
    """(iv) the 'drop it explicitly' branch in ``_fit_system_prompt``: a
    section with no line breaks, larger than the cap, cannot be partially
    retained by ``_trim_lines`` (a single unsplittable line either fits whole
    or not at all), so it is removed whole rather than left oversized.

    This branch is not reachable through ``build_system_prompt``'s own five
    sections in practice -- they are all already covered, one at a time, by
    the earlier per-name trim loop (``"bootstrap", "memory",
    "skills_catalogue", "active_skills", "identity"``), which reduces any of
    them to "" if needed before this second loop ever runs. It exists as a
    hard backstop for a section name outside that fixed order, so it is
    exercised here by calling ``_fit_system_prompt`` directly with a
    synthetic extra section the trim-order tuple does not know about.
    """
    builder = ContextBuilder(tmp_path)
    monkeypatch.setattr(ContextBuilder, "MAX_SYSTEM_PROMPT_CHARS", 50)
    sections = [("identity", "hello world, a short single line with no newline"), ("mystery", "Z" * 500)]

    prompt = builder._fit_system_prompt(sections, strict=False)
    fit = builder.last_fit

    assert "mystery" in fit["sections"], "removed entirely, but the key survives -- 0, not absent"
    assert fit["sections"]["mystery"] == 0
    assert any(d["section"] == "mystery" for d in fit["dropped"])
    _assert_reconciles(fit, prompt)


# ─── (c) future-section guard: the arithmetic pin is real ──────────────────


def test_reconciliation_helper_catches_a_future_hidden_section(tmp_path, monkeypatch):
    """If someone later appends content to the joined prompt OUTSIDE the
    named sections list (e.g. a sixth section folded straight into
    ``_join_sections`` without being added to the sections list passed to
    ``_fit_system_prompt``), the reconciliation invariant must break, not
    silently pass -- proving ``_assert_reconciles`` is a real arithmetic pin,
    not a tautology that always holds by construction.
    """
    orig_join = ContextBuilder.__dict__["_join_sections"].__func__

    def _leaky_join(cls, sections):
        return orig_join(cls, sections) + "\n\nHIDDEN-EXTRA-NOT-IN-ANY-SECTION"

    monkeypatch.setattr(ContextBuilder, "_join_sections", classmethod(_leaky_join))

    builder = _builder(tmp_path, _section("Working knowledge", 3))
    prompt = builder.build_system_prompt(loop_profile=True)
    fit = builder.last_fit

    assert fit["chars"] == len(prompt), "the fit's own chars still matches its own (leaky) prompt"
    with pytest.raises(AssertionError):
        _assert_reconciles(fit, prompt)


# ─── (d) empty everything ───────────────────────────────────────────────────


def test_all_empty_sections_are_zero_except_identity(tmp_path):
    """No bootstrap, no always-skills, no skills catalogue, no memory: every
    section but identity is 0, chars equals identity's size exactly (no
    separators counted, since there is only ever one non-empty section)."""
    builder = ContextBuilder(tmp_path)
    builder._load_bootstrap_files = lambda: ""
    builder.skills.get_always_skills = lambda: []
    builder.skills.load_skills_for_context = lambda names: ""
    builder.skills.build_skills_summary = lambda excluded_names=None: ""
    builder.memory.get_memory_context = lambda loop=False: ""

    prompt = builder.build_system_prompt(loop_profile=True)
    fit = builder.last_fit

    assert list(fit["sections"]) == SECTION_NAMES
    for name in SECTION_NAMES:
        if name == "identity":
            assert fit["sections"][name] > 0
        else:
            assert fit["sections"][name] == 0
    assert fit["chars"] == fit["sections"]["identity"] == len(prompt)
    _assert_reconciles(fit, prompt)


# ─── (e) bridge ledger row ───────────────────────────────────────────────────

HEALTHY_SECTIONS = {"identity": 1_200, "bootstrap": 15_000, "active_skills": 0, "skills_catalogue": 5_179, "memory": 2_100}
OVERFLOW_SECTIONS = {"identity": 1_446, "bootstrap": 22_986, "active_skills": 0, "skills_catalogue": 6_951, "memory": 4_030}
OVERFLOW = SystemPromptOverflowError(
    over_by=11_434, cap=24_000, sections=OVERFLOW_SECTIONS,
    dropped=[{"section": "## Optional appendix", "chars": 512, "how": "declared-droppable"}],
    droppable_reserve_chars=0,
)


class _HealthySectionsManager(_FakeSubagentManager):
    def _build_subagent_prompt(self) -> str:
        self.last_prompt_fit = {
            "cap": 24_000, "chars": 23_500, "strict": True,
            "dropped": [{"section": "## Optional appendix", "chars": 900, "how": "declared-droppable"}],
            "droppable_reserve_chars": 1_200, "sections": HEALTHY_SECTIONS,
        }
        return "system prompt"


class _OverflowSectionsManager(_FakeSubagentManager):
    spawned = False

    def _build_subagent_prompt(self) -> str:
        self.last_prompt_fit = {"cap": 24_000, "chars": 35_434, "strict": True, "dropped": OVERFLOW.dropped}
        raise OVERFLOW

    async def spawn(self, **kwargs):
        type(self).spawned = True
        return await super().spawn(**kwargs)


@pytest.fixture(autouse=True)
def _core_smoke_set_matches_fixture_repo(monkeypatch):
    monkeypatch.setattr(bridge, "_CORE_SMOKE_TESTS", ("tests/test_smoke.py",))


def _wire(tmp_path, monkeypatch, manager_cls):
    state_dir = tmp_path / "state"
    state_dir.mkdir(exist_ok=True)
    _init_selfevo_repo(tmp_path)
    monkeypatch.setattr(bridge, "STATE_DIR", state_dir)
    monkeypatch.setattr(bridge, "BRIDGE_STATE_DIR", state_dir / "subagent_bridge")
    monkeypatch.setattr(bridge, "TARGET_WORKSPACE", tmp_path / "target_workspace")
    monkeypatch.setattr(bridge, "SubagentManager", manager_cls)
    monkeypatch.setattr(bridge, "_make_provider", lambda _config: object())
    monkeypatch.setenv("SELFEVO_DUMP_PROMPTS", "0")
    return state_dir


def test_bridge_healthy_row_carries_sections_and_chars(tmp_path, monkeypatch):
    """The healthy ``system_prompt`` ledger row carries the fake fit's
    ``sections`` breakdown verbatim, plus its ``chars``."""
    state_dir = _wire(tmp_path, monkeypatch, _HealthySectionsManager)
    _seed_bridge_request(state_dir, "req-fit", "cycle-fit", task_title="Extend a skill")

    rc = asyncio.run(bridge._main_impl())

    assert rc == 0
    rows = _read_ledger(state_dir)
    fit_rows = [r for r in rows if r["phase"] == "system_prompt"]
    assert len(fit_rows) == 1
    assert fit_rows[0]["sections"] == HEALTHY_SECTIONS
    assert fit_rows[0]["chars"] == 23_500
    _assert_reconciles(fit_rows[0])


def test_bridge_overflow_row_carries_sections_with_a_zero_entry_and_reconciles_to_cap_plus_over_by(tmp_path, monkeypatch):
    """Contract (per review): the overflow row does NOT carry ``chars`` --
    a refused build has no assembled prompt length, and a fabricated
    ``cap + over_by`` value there would let the dashboard's prompt-fit tile
    misread a refused build as a built prompt with negative headroom. The
    per-section breakdown (five keys, including the 0 ``active_skills``
    entry) still reconciles arithmetically to ``cap + over_by`` -- the
    would-be length, computed by the test from the row's own cap/over_by,
    never stored on the row itself.
    """
    state_dir = _wire(tmp_path, monkeypatch, _OverflowSectionsManager)
    _OverflowSectionsManager.spawned = False
    _seed_bridge_request(state_dir, "req-over", "cycle-over", task_title="Extend a skill")

    rc = asyncio.run(bridge._main_impl())

    assert _OverflowSectionsManager.spawned is False
    rows = _read_ledger(state_dir)
    fit_rows = [r for r in rows if r["phase"] == "system_prompt"]
    assert len(fit_rows) == 1
    row = fit_rows[0]
    assert row["overflow"] is True
    assert row["over_by"] == 11_434 and row["cap"] == 24_000
    assert set(row["sections"]) == set(SECTION_NAMES)
    assert row["sections"]["active_skills"] == 0

    assert "chars" not in row, "a refused build must not be journaled with a fabricated assembled length"
    _assert_reconciles({"sections": row["sections"], "chars": row["cap"] + row["over_by"]})


# ─── (f) old-row tolerance: the dashboard reader must not choke ─────────────


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def test_dashboard_scan_tolerates_pre_1379_ledger_rows(tmp_path):
    """``scan_prompt_fit_ledger`` (scripts/eeebot_dashboard.py:~1279) must
    parse both live pre-#1379 row shapes without error: a healthy row that
    never had a ``sections`` key at all, and a pre-``chars``-removal
    overflow row that had ``sections`` (four keys, no ``active_skills`` --
    #1379 predates it) but no post-#1379 five-key breakdown. Absence of
    ``sections`` (or a smaller/older shape of it) must not crash the scan;
    the reader does not currently even look at the key.
    """
    healthy_pre_1379 = {
        "phase": "system_prompt", "cycle_id": "c", "chars": 27184, "cap": 24000,
        "dropped": [], "droppable_reserve_chars": 0, "ts": "2026-08-01T00:00:00Z",
    }
    overflow_pre_chars_removal = {
        "phase": "system_prompt", "overflow": True, "over_by": 2922, "cap": 24000,
        "sections": {"identity": 100, "bootstrap": 20000, "skills_catalogue": 3000, "memory": 900},
        "dropped": [], "droppable_reserve_chars": 0,
    }
    _write_jsonl(tmp_path / "ledger" / "cycles.jsonl", [healthy_pre_1379, overflow_pre_chars_removal])

    result = DASHBOARD.scan_prompt_fit_ledger(tmp_path)

    assert result["source_status"] == "valid"
    assert result["rows_considered"] == 2
    latest = result["latest"]
    assert latest["cap"] == 24000
    assert latest["dropped_count"] == 0
    assert "chars" in latest, "the scanner's own shape is unchanged regardless of what the row carried"
