"""Tests for #1342: the gate closes the skill-tree write path.

AGENTS.md declares ``skills/<name>/SKILL.md`` with lowercase-hyphen names and
YAML frontmatter as a critical rule; the live tree drifted because nothing
checked it. These tests drive :func:`gate._skill_hygiene_violations` against
real git repos (a base commit, then a cycle commit) — the same shape the
bridge feeds it — and the zero-read census in :mod:`skill_fitness`.

The duplicate fixture is the four live test-selection skill descriptions
copied verbatim from the instance repo on 2026-09-05.
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from nanobot.runtime import gate, skill_fitness


# ─── fixtures ────────────────────────────────────────────────────────────────

# Verbatim frontmatter descriptions of the four live test-selection skills
# (ozand/eeebot-self-evolving @ 99c02a1a). The gate must see at least one pair
# as duplicates — that is the drift this issue exists to stop.
LIVE_TEST_SELECTION_SKILLS = {
    "run-tests": "Run the affected test gate locally and report results in one exec call",
    "targeted-test-discovery": (
        "Discover and run only the tests that cover the files you changed, in one bounded pass"
    ),
    "run-targeted-tests-to-avoid-timeouts": (
        "Scope pytest invocations to the test modules that cover the files you changed, "
        "so verification finishes on the slow eeepc host instead of timing out on a full-suite sweep"
    ),
    "focus-inspection-on-paired-test-suite": (
        "Restrict inspection to the target script and its paired test file, skipping repo-wide grep sweeps"
    ),
}


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-c", f"safe.directory={repo}", "-C", str(repo), *args],
        capture_output=True, text=True, check=True,
    )


def _skill_md(name: str, description: str, *, fm_name: str | None = None) -> str:
    return (
        "---\n"
        f"name: {name if fm_name is None else fm_name}\n"
        f"description: {description}\n"
        'version: "1.0.0"\n'
        "---\n\n# Skill\n\nBody.\n"
    )


def _repo_with_skills(tmp_path: Path, skills: dict[str, str]) -> tuple[Path, str]:
    """Init a repo whose base commit holds *skills* (name -> description)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "--initial-branch=main")
    _git(repo, "config", "user.email", "gate@test.local")
    _git(repo, "config", "user.name", "gate-test")
    (repo / "README.md").write_text("base\n", encoding="utf-8", newline="\n")
    for name, desc in skills.items():
        d = repo / "skills" / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(_skill_md(name, desc), encoding="utf-8", newline="\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    base_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    return repo, base_sha


def _cycle_commit(repo: Path, writes: dict[str, str | None]) -> list[str]:
    """Apply *writes* (path -> text, None = delete) as one cycle commit; return changed files."""
    for rel, text in writes.items():
        path = repo / rel
        if text is None:
            path.unlink()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "cycle")
    return sorted(writes)


def _violations(repo: Path, base_sha: str, changed: list[str]) -> list[str]:
    return gate._skill_hygiene_violations(repo, base_sha, changed)


# ─── layout: one test per violation, distinct reason strings ─────────────────


def test_loose_file_at_top_of_skills_is_rejected(tmp_path):
    repo, base = _repo_with_skills(tmp_path, {"run-tests": LIVE_TEST_SELECTION_SKILLS["run-tests"]})
    changed = _cycle_commit(repo, {"skills/batch_grep.py": "print('loose')\n"})
    out = _violations(repo, base, changed)
    assert len(out) == 1
    assert out[0].startswith("skill layout: loose file at the top of skills/")
    assert "skills/batch_grep.py" in out[0]


def test_directory_name_not_lowercase_hyphen_is_rejected(tmp_path):
    repo, base = _repo_with_skills(tmp_path, {})
    changed = _cycle_commit(repo, {
        "skills/fast_path_redundancy_skip/SKILL.md": _skill_md(
            "fast_path_redundancy_skip", "Evaluate the early-exit hierarchy and emit the outcome"
        ),
    })
    out = _violations(repo, base, changed)
    names = [v for v in out if v.startswith("skill layout: directory name must match")]
    assert names == [
        "skill layout: directory name must match ^[a-z0-9]+(-[a-z0-9]+)*$: skills/fast_path_redundancy_skip/"
    ]


def test_missing_frontmatter_is_rejected(tmp_path):
    repo, base = _repo_with_skills(tmp_path, {})
    changed = _cycle_commit(repo, {"skills/memory-lookup/SKILL.md": "# Memory lookup\n\nNo frontmatter.\n"})
    out = _violations(repo, base, changed)
    assert out == [
        "skill frontmatter: missing YAML frontmatter (--- name/description ---): skills/memory-lookup/SKILL.md"
    ]


def test_empty_name_or_description_is_rejected(tmp_path):
    repo, base = _repo_with_skills(tmp_path, {})
    changed = _cycle_commit(repo, {
        "skills/memory-lookup/SKILL.md": "---\nname: memory-lookup\ndescription:\n---\n\nbody\n",
    })
    assert _violations(repo, base, changed) == [
        "skill frontmatter: empty name or description: skills/memory-lookup/SKILL.md"
    ]


def test_frontmatter_name_must_match_directory(tmp_path):
    repo, base = _repo_with_skills(tmp_path, {})
    changed = _cycle_commit(repo, {
        "skills/batch-grep/SKILL.md": _skill_md("batch-grep", "Match many regex patterns in one pass", fm_name="batch_grep"),
    })
    assert _violations(repo, base, changed) == [
        "skill frontmatter: name 'batch_grep' does not match directory 'batch-grep': skills/batch-grep/SKILL.md"
    ]


def test_directory_without_skill_md_is_rejected(tmp_path):
    repo, base = _repo_with_skills(tmp_path, {})
    changed = _cycle_commit(repo, {"skills/helpers/util.py": "x = 1\n"})
    assert _violations(repo, base, changed) == [
        "skill layout: directory without SKILL.md: skills/helpers/"
    ]


def test_well_formed_new_skill_passes(tmp_path):
    repo, base = _repo_with_skills(tmp_path, {"run-tests": LIVE_TEST_SELECTION_SKILLS["run-tests"]})
    changed = _cycle_commit(repo, {
        "skills/posix-background-execution/SKILL.md": _skill_md(
            "posix-background-execution",
            "Run detached background processes from POSIX sh without bashisms like disown",
        ),
    })
    assert _violations(repo, base, changed) == []


def test_non_skill_changes_are_ignored(tmp_path):
    repo, base = _repo_with_skills(tmp_path, {})
    changed = _cycle_commit(repo, {"scripts/foo.py": "x = 1\n"})
    assert _violations(repo, base, changed) == []


def test_cleanup_deleting_loose_file_and_renaming_snake_case_dir_passes(tmp_path):
    """The sibling cleanup line must be able to fix the tree through this gate."""
    repo, base = _repo_with_skills(tmp_path, {"batch_grep": "Match multiple regex patterns in a single pass across files"})
    (repo / "skills" / "jsonl_stream_filter.py").write_text("loose\n", encoding="utf-8", newline="\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "drifted")
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()
    changed = _cycle_commit(repo, {
        "skills/jsonl_stream_filter.py": None,
        "skills/batch_grep/SKILL.md": None,
        "skills/batch-grep/SKILL.md": _skill_md("batch-grep", "Match multiple regex patterns in a single pass across files"),
    })
    assert _violations(repo, base, changed) == []


# ─── duplicates ──────────────────────────────────────────────────────────────


def test_new_skill_duplicating_existing_description_is_rejected_naming_the_original(tmp_path):
    repo, base = _repo_with_skills(tmp_path, {
        "targeted-test-discovery": LIVE_TEST_SELECTION_SKILLS["targeted-test-discovery"],
        "memory-lookup": "Find a specific fact or history entry with one targeted read_file call",
    })
    changed = _cycle_commit(repo, {
        "skills/run-targeted-tests-to-avoid-timeouts/SKILL.md": _skill_md(
            "run-targeted-tests-to-avoid-timeouts",
            LIVE_TEST_SELECTION_SKILLS["run-targeted-tests-to-avoid-timeouts"],
        ),
    })
    out = _violations(repo, base, changed)
    assert len(out) == 1
    assert out[0].startswith("skill duplicate: new skill skills/run-targeted-tests-to-avoid-timeouts/")
    assert "existing skill 'targeted-test-discovery'" in out[0]
    assert "extend skills/targeted-test-discovery/SKILL.md instead" in out[0]


def test_live_test_selection_fixture_trips_at_least_one_pair():
    """The four real descriptions: at least one pair scores above the threshold."""
    names = list(LIVE_TEST_SELECTION_SKILLS)
    tripped = []
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            ratio, shared = gate._skill_description_overlap(
                LIVE_TEST_SELECTION_SKILLS[a], LIVE_TEST_SELECTION_SKILLS[b]
            )
            if shared >= gate._SKILL_DUP_MIN_SHARED and ratio >= gate._SKILL_DUP_THRESHOLD:
                tripped.append((a, b, round(ratio, 2)))
    assert ("targeted-test-discovery", "run-targeted-tests-to-avoid-timeouts", 0.44) in tripped


def test_threshold_is_below_the_duplicates_and_above_the_nearest_non_duplicate():
    """Pins the calibration recorded next to _SKILL_DUP_THRESHOLD."""
    dup_ratio, _ = gate._skill_description_overlap(
        "Pair new AGENTS.md instruction sections with structural assertions in "
        "tests/test_agents_structure.py so standing instructions are permanently enforced "
        "against accidental removal",
        "Sync mandatory AGENTS.md section headings with REQUIRED_SECTIONS in tests/test_agents_structure.py",
    )
    near_ratio, _ = gate._skill_description_overlap(
        "Stage target-path edits early and verify in stages to beat context and turn limits",
        "Run early edits and targeted test validation on a fixed turn cadence to reserve "
        "budget for verification and commit",
    )
    assert dup_ratio >= gate._SKILL_DUP_THRESHOLD > near_ratio
    assert round(dup_ratio, 2) == 0.55 and round(near_ratio, 2) == 0.27


def test_extending_an_existing_skill_in_place_is_never_a_duplicate(tmp_path):
    """Two existing skills with identical descriptions; editing one must pass."""
    desc = LIVE_TEST_SELECTION_SKILLS["targeted-test-discovery"]
    repo, base = _repo_with_skills(tmp_path, {"targeted-test-discovery": desc, "run-tests": desc})
    changed = _cycle_commit(repo, {
        "skills/run-tests/SKILL.md": _skill_md("run-tests", desc) + "\n## More\n\nExtended.\n",
    })
    assert _violations(repo, base, changed) == []


def test_two_incidental_shared_words_do_not_trip(tmp_path):
    repo, base = _repo_with_skills(tmp_path, {
        "targeted-test-discovery": LIVE_TEST_SELECTION_SKILLS["targeted-test-discovery"],
    })
    changed = _cycle_commit(repo, {
        "skills/batch-grep/SKILL.md": _skill_md(
            "batch-grep", "Match multiple regex patterns in a single pass across files"
        ),
    })
    assert _violations(repo, base, changed) == []


def test_git_failure_is_fail_closed(tmp_path):
    repo, base = _repo_with_skills(tmp_path, {})
    out = _violations(repo, "0" * 40, ["skills/x/SKILL.md"])
    assert len(out) == 1 and out[0].startswith("skill hygiene: cannot list skills/")


# ─── bridge wiring ───────────────────────────────────────────────────────────


def test_bridge_changed_files_carries_skill_hygiene_into_mutation_violations(tmp_path):
    from nanobot.runtime import bridge

    repo, base = _repo_with_skills(tmp_path, {})
    _cycle_commit(repo, {"skills/loose.py": "x\n"})
    files, blocked, mutation, tier = bridge._changed_files_and_violations(repo, base)
    assert files == ["skills/loose.py"]
    assert blocked == []
    assert tier == "script"
    assert any(v.startswith("skill layout: loose file") for v in mutation), mutation


# ─── zero-read census (report only) ──────────────────────────────────────────


def _reads(state: Path, rows: list[dict]) -> None:
    path = state / "skill_fitness" / "reads.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_version": "skill-fitness-v1", "reads": rows}), encoding="utf-8")


def test_census_lists_zero_read_skills_with_count_and_last_read(tmp_path):
    repo, _ = _repo_with_skills(tmp_path, {"run-tests": "a b c d", "memory-lookup": "e f g h", "idle-skill": "i j"})
    state = tmp_path / "state"
    now = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)
    recent = (now - timedelta(days=3)).isoformat().replace("+00:00", "Z")
    old = (now - timedelta(days=45)).isoformat().replace("+00:00", "Z")
    _reads(state, [
        {"skill": "run-tests", "ts": recent, "confirmed": True},
        {"skill": "memory-lookup", "ts": old, "confirmed": True},
        {"skill": "idle-skill", "ts": recent, "confirmed": False},  # authoring read: not evidence
    ])
    summary = skill_fitness.write_zero_read_census(state, repo, now=now)
    payload = json.loads((state / "demand" / "skill_census.json").read_text(encoding="utf-8"))
    assert payload["schema"] == "skill-census-v1"
    assert payload["window_days"] == skill_fitness._CENSUS_WINDOW_DAYS
    assert payload["zero_read"] == [
        {"skill": "idle-skill", "reads_in_window": 0, "last_read": None},
        {"skill": "memory-lookup", "reads_in_window": 0, "last_read": old},
    ]
    assert summary == {"written": 2, "path": str(state / "demand" / "skill_census.json")}


@pytest.mark.parametrize("corrupt", [None, "{not json", '{"schema_version": "other", "reads": 1}'])
def test_census_fails_open_on_missing_or_corrupt_reads(tmp_path, corrupt):
    repo, _ = _repo_with_skills(tmp_path, {"run-tests": "a b c d"})
    state = tmp_path / "state"
    if corrupt is not None:
        path = state / "skill_fitness" / "reads.json"
        path.parent.mkdir(parents=True)
        path.write_text(corrupt, encoding="utf-8")
    rows = skill_fitness.zero_read_census(state, repo)
    assert rows == [{"skill": "run-tests", "reads_in_window": 0, "last_read": None}]


def test_census_never_gates(tmp_path):
    """A skill with zero reads is not a hygiene violation."""
    repo, base = _repo_with_skills(tmp_path, {"run-tests": "Run the affected test gate locally"})
    changed = _cycle_commit(repo, {"skills/run-tests/SKILL.md": _skill_md("run-tests", "Run the affected test gate locally") + "more\n"})
    assert _violations(repo, base, changed) == []
    assert skill_fitness.zero_read_census(tmp_path / "state", repo) == [
        {"skill": "run-tests", "reads_in_window": 0, "last_read": None}
    ]


def test_census_writer_is_declared_in_state_paths():
    from nanobot.runtime import state_paths

    assert "nanobot.runtime.skill_fitness:write_zero_read_census" in state_paths.STATE_PATH_WRITERS["demand"]
