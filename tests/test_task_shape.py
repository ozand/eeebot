"""The pre-execution task-shape classifier (#1825)."""
from __future__ import annotations

from pathlib import Path

from nanobot.runtime.task_shape import TASK_SHAPES, classify_task_shape


def test_shape_vocabulary_is_fixed_and_ordered():
    assert TASK_SHAPES == (
        "extend_existing_script",
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
    """A brand-new script is not the shape #1825 is measuring -- it falls
    to 'other', since the vocabulary names only EXTENDING an existing one."""
    (tmp_path / "scripts").mkdir()
    assert classify_task_shape(target_path="scripts/new_thing.py", repo=tmp_path) == "other"


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
