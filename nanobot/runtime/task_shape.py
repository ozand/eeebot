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

import subprocess
from pathlib import Path

#: Fixed vocabulary, defined in exactly this one place. The classes match
#: the shapes #1825's own hand-measurement found in the ledger on
#: 2026-09-19/20 -- adding a class is a harness change to this tuple, never
#: something a loop-authored commit can do (see the module docstring).
#:
#: ``new_leaf_script`` (PR #1834 review): a script the cycle under
#: examination created itself, distinct from ``extend_existing_script``
#: -- see :func:`classify_task_shape`'s own docstring for the defect this
#: corrects (present-tense existence cannot tell the two apart, since a
#: freshly created file exists too).
TASK_SHAPES = (
    "extend_existing_script",
    "new_leaf_script",
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


def _script_existed_before_base(repo: Path, base_sha: str, target: str) -> "bool | None":
    """True iff *target* has at least one commit at-or-before *base_sha*.

    This is the check PR #1834's review found missing: a present-tense
    ``.is_file()`` test asks "does this exist NOW", and a script the
    cycle under examination just created exists now too -- measured
    live, 3 of 13 ``scripts/*.py`` targets in a 40-row sample were
    created by the very cycle that targeted them, and the present-tense
    check called all three ``extend_existing_script``. ``base_sha`` (the
    ledger's ``main_sha_before`` -- the commit the cycle branched from)
    is the repository's state BEFORE that cycle ran; a target with no
    history there did not exist yet.

    ``git log --oneline <base_sha> -- <target>`` — the path is passed as
    a subprocess argument, never through a shell pipe, so it cannot pick
    up the CRLF-corruption failure mode the review's own first pass hit.

    Returns ``None`` on any git error (unreadable repo, an unresolvable
    *base_sha*, git not installed) -- fail to "unknown", never guess new
    vs. existing from a failed measurement; the caller degrades to the
    coarser present-tense check on ``None``.
    """
    try:
        result = subprocess.run(
            ["git", "-c", f"safe.directory={repo}", "-C", str(repo),
             "log", "--oneline", str(base_sha), "--", target],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return None
        return bool(result.stdout.strip())
    except Exception:
        return None


def classify_task_shape(
    *, task_title: str = "", target_path: str = "", task_text: str = "",
    repo: "Path | None" = None, base_sha: "str | None" = None,
) -> str:
    """One of :data:`TASK_SHAPES`, checked in this fixed precedence order.

    For a ``scripts/*.py`` target, distinguishing ``extend_existing_script``
    from ``new_leaf_script`` needs to know the repository's state BEFORE
    the cycle under examination ran, not its state now -- a script the
    cycle just created exists now too. When both *repo* (the instance
    repository checkout) and *base_sha* (the ledger's ``main_sha_before``
    for this cycle) are given, :func:`_script_existed_before_base`
    answers this precisely via git history. Without *base_sha* (most
    ``'proposed'`` rows do not carry one -- it is written only on the
    later ``'outcome'`` row) this degrades to the coarser present-tense
    ``.is_file()`` check, which cannot make the distinction and reports
    ``extend_existing_script`` for anything already on disk (documented
    here, not silently over-confident). Without *repo* at all, a
    ``scripts/`` target is assumed existing.
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
        if repo is not None and base_sha:
            existed_before = _script_existed_before_base(Path(repo), base_sha, target)
            if existed_before is True:
                return "extend_existing_script"
            if existed_before is False:
                return "new_leaf_script"
            # existed_before is None: git measurement failed -- fall
            # through to the coarser present-tense check below rather
            # than guess.
        exists_now = (Path(repo) / target).is_file() if repo is not None else True
        if exists_now:
            return "extend_existing_script"
    return "other"
