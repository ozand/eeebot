"""Harness-owned task-shape classifier (#1825).

The gap this fills, per the issue's own codebase audit (issue #1825
comment): ``demand.py``'s existing classifiers
(``classify_change_tier``, ``classify_integration_class``) both read a
COMMIT's ``files_changed`` -- they classify what a cycle already did.
Nothing classifies what a cycle was ASKED to do, before it runs. This
module is that pre-execution sibling: it reads a ``'proposed'`` ledger
row's own ``task_title``/``target_path`` (written by
``nanobot.runtime.llm_proposer`` before any subagent spawns) and
assigns one of a fixed shape vocabulary.

Harness-owned, not loop-owned: this module lives under
``nanobot/runtime/``, outside every prefix
``nanobot.runtime.mutation_policy`` lists as loop-writable -- the loop
has no reachable path to edit this file or its vocabulary, the same
namespace argument ``skill_fitness.py``/``diary_fitness.py`` rely on
for their own harness-owned status.

Report only (ADR-011 rule 3): see ``nanobot.runtime.trajectory``'s
module docstring for the structural test that keeps this
classification out of task selection.
"""
from __future__ import annotations

from pathlib import Path

#: Fixed vocabulary, defined in exactly this one place. The classes match
#: the shapes #1825's own hand-measurement found in the ledger on
#: 2026-09-19/20 -- adding a class is a harness change to this tuple, never
#: something a loop-authored commit can do (see the module docstring).
TASK_SHAPES = (
    "extend_existing_script",
    "skill_authoring",
    "agents_md_rule",
    "test_repair",
    "give_invoker",
    "other",
)

#: Phrases naming the "give it an invoker" shape -- OPERATING.md's own
#: "Connect" execution kind ("an artifact exists, is tested, and nothing
#: runs it. Give it an invoker.") in the vocabulary a proposal's rationale
#: is likely to use.
_INVOKER_PHRASES = (
    "give it an invoker", "give it a caller", "no consumer", "nothing runs it",
    "nothing invokes it", "wire it up", "wire up", "connect it", "add an invoker",
    "invoke it",
)
#: Phrases naming the "repair a test" shape, checked against the task text
#: as a fallback when the target path itself is not under ``tests/`` (e.g.
#: a proposal that repairs a test by fixing the code it tests).
_TEST_REPAIR_PHRASES = ("repair test", "repair the test", "fix test", "fix the test", "failing test")


def classify_task_shape(
    *, task_title: str = "", target_path: str = "", task_text: str = "", repo: "Path | None" = None,
) -> str:
    """One of :data:`TASK_SHAPES`, checked in this fixed precedence order.

    *repo*, when given (the instance repository checkout), is used for
    exactly one check: whether *target_path* names a ``scripts/*.py``
    file that ALREADY EXISTS -- the signal that distinguishes "extend an
    existing script" (#1825's own monoculture finding) from creating a
    new one, which this vocabulary does not name and which falls through
    to ``"other"``. Without *repo* this degrades to trusting the path
    shape alone (documented here, not silently over-confident): a
    ``scripts/`` target is assumed existing when there is no checkout to
    check against.
    """
    target = str(target_path or "").replace("\\", "/").strip().lstrip("/")
    text = f"{task_title or ''}\n{task_text or ''}".lower()

    target_basename = target.rsplit("/", 1)[-1] if target else ""
    if target_basename == "AGENTS.md":
        return "agents_md_rule"
    if target.startswith("skills/"):
        return "skill_authoring"
    if target.startswith("tests/") or any(phrase in text for phrase in _TEST_REPAIR_PHRASES):
        return "test_repair"
    if any(phrase in text for phrase in _INVOKER_PHRASES):
        return "give_invoker"
    if target.startswith("scripts/") and target.endswith(".py"):
        exists = (Path(repo) / target).is_file() if repo is not None else True
        if exists:
            return "extend_existing_script"
    return "other"
