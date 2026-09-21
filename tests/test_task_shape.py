"""The pre-execution task-shape classifier (#1825)."""
from __future__ import annotations

import subprocess
from pathlib import Path

from nanobot.runtime.task_shape import TASK_SHAPES, classify_task_shape


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, text=True, capture_output=True,
    ).stdout.strip()


def test_shape_vocabulary_is_fixed_and_ordered():
    assert TASK_SHAPES == (
        "extend_existing_script",
        "new_leaf_script",
        "skill_authoring",
        "agents_md_rule",
        "test_repair",
        "give_invoker",
        "other",
    )


def test_agents_md_target_classifies_as_agents_md_rule():
    assert classify_task_shape(target_path="AGENTS.md", task_title="add a rule") == "agents_md_rule"


def test_skill_target_classifies_as_skill_authoring():
    assert classify_task_shape(target_path="skills/review/SKILL.md") == "skill_authoring"


def test_tests_target_classifies_as_test_repair():
    assert classify_task_shape(target_path="tests/test_foo.py") == "test_repair"


def test_repair_phrase_classifies_as_test_repair_even_off_tests_path():
    assert classify_task_shape(
        target_path="scripts/foo.py", task_title="repair the failing test for foo",
    ) == "test_repair"


def test_invoker_phrase_classifies_as_give_invoker():
    assert classify_task_shape(
        target_path="scripts/foo.py", task_title="give it an invoker, nothing runs it",
    ) == "give_invoker"


def test_scripts_target_without_repo_defaults_to_extending_existing():
    """No repo checkout to verify existence against -- documented coarse
    fallback: a scripts/ target is assumed existing."""
    assert classify_task_shape(target_path="scripts/analyze_repeat_failures.py") == "extend_existing_script"


def test_scripts_target_with_repo_and_file_present_is_extend_existing(tmp_path: Path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "foo.py").write_text("pass\n", encoding="utf-8")
    assert classify_task_shape(target_path="scripts/foo.py", repo=tmp_path) == "extend_existing_script"


def test_scripts_target_with_repo_and_file_absent_is_other(tmp_path: Path):
    """Without a base_sha there is no way to tell 'new leaf' from
    'not there yet, not part of this cycle either' -- coarse fallback,
    documented on classify_task_shape itself."""
    (tmp_path / "scripts").mkdir()
    assert classify_task_shape(target_path="scripts/new_thing.py", repo=tmp_path) == "other"


# ---------------------------------------------------------------------------
# new_leaf_script (PR #1834 review): present-tense existence cannot tell a
# script the cycle just created from one that has always been there --
# base_sha (the ledger's own main_sha_before) is what actually answers it.
# ---------------------------------------------------------------------------


def _repo_with_base_and_new_script(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    (repo / "scripts" / "existing.py").write_text("pass\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base: existing script")
    base_sha = _git(repo, "rev-parse", "HEAD")

    (repo / "scripts" / "new_leaf.py").write_text("pass\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "cycle: add new_leaf.py")
    return repo, base_sha


def test_a_script_with_history_before_base_sha_is_extend_existing(tmp_path: Path):
    repo, base_sha = _repo_with_base_and_new_script(tmp_path)
    assert classify_task_shape(target_path="scripts/existing.py", repo=repo, base_sha=base_sha) == "extend_existing_script"


def test_a_script_created_after_base_sha_is_new_leaf_script(tmp_path: Path):
    """The exact defect PR #1834's review measured live: the target exists
    NOW (this cycle just created it), so a present-tense check alone would
    call it extend_existing_script. base_sha makes the difference visible."""
    repo, base_sha = _repo_with_base_and_new_script(tmp_path)
    assert classify_task_shape(target_path="scripts/new_leaf.py", repo=repo, base_sha=base_sha) == "new_leaf_script"


def test_present_tense_check_alone_would_have_missed_it(tmp_path: Path):
    """Without base_sha, the same target degrades to the coarse
    present-tense check and reports extend_existing_script -- this is the
    documented, known-imprecise fallback, not a second bug."""
    repo, _base_sha = _repo_with_base_and_new_script(tmp_path)
    assert classify_task_shape(target_path="scripts/new_leaf.py", repo=repo) == "extend_existing_script"


def test_base_sha_without_repo_is_ignored_not_an_error():
    assert classify_task_shape(target_path="scripts/x.py", base_sha="deadbeef") == "extend_existing_script"


def test_an_unresolvable_base_sha_degrades_to_the_coarse_check(tmp_path: Path):
    """A git error (bad base_sha, unreadable repo) must never be guessed
    as new-vs-existing -- it falls through to the present-tense check."""
    repo, _base_sha = _repo_with_base_and_new_script(tmp_path)
    assert classify_task_shape(
        target_path="scripts/existing.py", repo=repo, base_sha="0" * 40,
    ) == "extend_existing_script"  # present-tense fallback: the file IS there


def test_live_shape_from_the_1834_review_reproduces_10_extend_3_new_leaf(tmp_path: Path):
    """Not the production instance repo (out of this session's reach, per
    the PR's own admission) -- a fixture built to the SAME SHAPE the
    review measured: 13 scripts/*.py targets from a 40-row window, 10
    with history before the cycle's base_sha, 3 created by the cycle
    itself. The three new-leaf names are the review's own
    (scripts/demand_quarantine.py, scripts/filter_defects.py,
    scripts/verify_doc_sections.py); the ten 'extend' names are real
    artifact names from this repo's own #1796 in-degree measurement,
    standing in for the review's unnamed ten -- the claim under test is
    the SHAPE (10 extend, 3 new-leaf), not that these specific ten are
    the review's own ten."""
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")

    extend_names = [
        "analyze_repeat_failures", "eeebot_dashboard", "validate_lessons",
        "workspace_validation_helpers", "validate_deprecation_status", "run_all_tests",
        "approval_truth", "validate_telegram_live_proof", "check_destructive_syntax",
        "check_litellm_cooldown",
    ]
    for name in extend_names:
        (repo / "scripts" / f"{name}.py").write_text("pass\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base: ten pre-existing scripts")
    base_sha = _git(repo, "rev-parse", "HEAD")

    new_leaf_names = ["demand_quarantine", "filter_defects", "verify_doc_sections"]
    for name in new_leaf_names:
        (repo / "scripts" / f"{name}.py").write_text("pass\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "cycle: three new leaves")

    for name in extend_names:
        assert classify_task_shape(
            target_path=f"scripts/{name}.py", repo=repo, base_sha=base_sha,
        ) == "extend_existing_script", name
    for name in new_leaf_names:
        assert classify_task_shape(
            target_path=f"scripts/{name}.py", repo=repo, base_sha=base_sha,
        ) == "new_leaf_script", name


def test_no_target_and_no_matching_phrase_is_other():
    assert classify_task_shape(task_title="investigate something vague") == "other"


def test_precedence_agents_md_beats_everything_else():
    """A target that is literally AGENTS.md still classifies as
    agents_md_rule even if the rationale text also uses invoker/test
    phrasing -- precedence is fixed, not a best-match scan."""
    assert classify_task_shape(
        target_path="AGENTS.md", task_title="give it an invoker and repair the failing test",
    ) == "agents_md_rule"


def test_path_separators_and_leading_slash_are_normalised():
    assert classify_task_shape(target_path="\\skills\\review\\SKILL.md") == "skill_authoring"
    assert classify_task_shape(target_path="/AGENTS.md") == "agents_md_rule"
