"""Tests for #1342: the gate closes the skill-tree write path.

AGENTS.md declares ``skills/<name>/SKILL.md`` with lowercase-hyphen names and
YAML frontmatter as a critical rule; the live tree drifted because nothing
checked it. These tests drive :func:`gate._skill_hygiene_violations` against
real git repos (a base commit, then a cycle commit) with the changed-file list
taken from ``git diff --name-only`` — exactly what the bridge feeds it — and
the zero-read census in :mod:`skill_fitness`.

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


def _write(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _mv(repo: Path, src: str, dst: str) -> None:
    """Filesystem move; git detects the rename by content on ``add -A``."""
    (repo / dst).parent.mkdir(parents=True, exist_ok=True)
    (repo / src).rename(repo / dst)


def _repo_with_skills(tmp_path: Path, skills: dict[str, str]) -> tuple[Path, str]:
    """Init a repo whose base commit holds *skills* (name -> description)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "--initial-branch=main")
    _git(repo, "config", "user.email", "gate@test.local")
    _git(repo, "config", "user.name", "gate-test")
    _write(repo, "README.md", "base\n")
    for name, desc in skills.items():
        _write(repo, f"skills/{name}/SKILL.md", _skill_md(name, desc))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    return repo, _git(repo, "rev-parse", "HEAD").stdout.strip()


def _commit_all(repo: Path, message: str = "commit") -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _cycle_commit(repo: Path, base_sha: str, writes: dict[str, str | None]) -> list[str]:
    """Apply *writes* (path -> text, None = delete) as one cycle commit.

    Returns what the bridge feeds the gate: ``git diff --name-only base HEAD``
    (a rename shows only its destination — the production shape).
    """
    for rel, text in writes.items():
        if text is None:
            (repo / rel).unlink()
        else:
            _write(repo, rel, text)
    _commit_all(repo, "cycle")
    out = _git(repo, "diff", "--name-only", base_sha, "HEAD").stdout
    return [line for line in out.splitlines() if line.strip()]


def _violations(repo: Path, base_sha: str, changed: list[str]) -> list[str]:
    return gate._skill_hygiene_violations(repo, base_sha, changed)


# ─── layout: one test per violation, distinct reason strings ─────────────────


def test_loose_file_at_top_of_skills_is_rejected(tmp_path):
    repo, base = _repo_with_skills(tmp_path, {"run-tests": LIVE_TEST_SELECTION_SKILLS["run-tests"]})
    changed = _cycle_commit(repo, base, {"skills/batch_grep.py": "print('loose')\n"})
    out = _violations(repo, base, changed)
    assert len(out) == 1
    assert out[0].startswith("skill layout: loose file at the top of skills/")
    assert "skills/batch_grep.py" in out[0]


def test_directory_name_not_lowercase_hyphen_is_rejected(tmp_path):
    repo, base = _repo_with_skills(tmp_path, {})
    changed = _cycle_commit(repo, base, {
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
    changed = _cycle_commit(repo, base, {"skills/memory-lookup/SKILL.md": "# Memory lookup\n\nNo frontmatter.\n"})
    assert _violations(repo, base, changed) == [
        "skill frontmatter: missing or malformed YAML frontmatter (--- name/description ---): skills/memory-lookup/SKILL.md"
    ]


def test_empty_name_or_description_is_rejected(tmp_path):
    repo, base = _repo_with_skills(tmp_path, {})
    changed = _cycle_commit(repo, base, {
        "skills/memory-lookup/SKILL.md": "---\nname: memory-lookup\ndescription:\n---\n\nbody\n",
    })
    assert _violations(repo, base, changed) == [
        "skill frontmatter: empty name or description: skills/memory-lookup/SKILL.md"
    ]


def test_description_over_policy_limit_is_rejected(tmp_path):
    repo, base = _repo_with_skills(tmp_path, {})
    changed = _cycle_commit(repo, base, {
        "skills/memory-lookup/SKILL.md": "---\nname: memory-lookup\ndescription: " + ("x" * 121) + "\n---\n\nbody\n",
    })
    violations = _violations(repo, base, changed)
    assert violations == [
        "skill frontmatter: description exceeds 120 chars: skills/memory-lookup/SKILL.md"
    ]


def test_description_at_policy_limit_is_allowed(tmp_path):
    repo, base = _repo_with_skills(tmp_path, {})
    changed = _cycle_commit(repo, base, {
        "skills/memory-lookup/SKILL.md": "---\nname: memory-lookup\ndescription: " + ("x" * 120) + "\n---\n\nbody\n",
    })
    assert _violations(repo, base, changed) == []


@pytest.mark.parametrize("raw", ["# comment only", "null", "[]", "~", "{}", '"" # empty quoted'])
def test_empty_yaml_values_are_rejected_as_empty(tmp_path, raw):
    repo, base = _repo_with_skills(tmp_path, {})
    changed = _cycle_commit(repo, base, {
        "skills/memory-lookup/SKILL.md": f"---\nname: memory-lookup\ndescription: {raw}\n---\n\nbody\n",
    })
    assert _violations(repo, base, changed) == [
        "skill frontmatter: empty name or description: skills/memory-lookup/SKILL.md"
    ]


@pytest.mark.parametrize("raw", ['"unterminated', "'unterminated", '"closed" trailing junk'])
def test_malformed_scalars_are_rejected_as_malformed_frontmatter(tmp_path, raw):
    repo, base = _repo_with_skills(tmp_path, {})
    changed = _cycle_commit(repo, base, {
        "skills/memory-lookup/SKILL.md": f"---\nname: memory-lookup\ndescription: {raw}\n---\n\nbody\n",
    })
    assert _violations(repo, base, changed) == [
        "skill frontmatter: missing or malformed YAML frontmatter (--- name/description ---): skills/memory-lookup/SKILL.md"
    ]


def test_yaml_scalar_grammar():
    ys = gate._yaml_scalar
    assert ys("Find a fact # trailing comment") == "Find a fact"
    assert ys('"quoted # not a comment" # comment') == "quoted # not a comment"
    assert ys("'single'") == "single"
    assert ys('"esc \\" aped"') == 'esc \\" aped'
    assert ys("Null") == "" and ys("~") == "" and ys("[a, b]") == "" and ys("{a: 1}") == ""
    assert ys('"open') is gate._MALFORMED and ys("'open") is gate._MALFORMED


def test_directory_name_rule_is_anchored_at_both_ends():
    assert gate._SKILL_DIR_RE.fullmatch("batch-grep")
    assert gate._SKILL_DIR_RE.fullmatch("a\n") is None
    assert gate._SKILL_DIR_RE.fullmatch("Batch-grep") is None
    assert gate._SKILL_DIR_RE.fullmatch("batch_grep") is None
    assert gate._SKILL_DIR_RE.fullmatch("-x") is None and gate._SKILL_DIR_RE.fullmatch("x-") is None


def test_frontmatter_name_must_match_directory(tmp_path):
    repo, base = _repo_with_skills(tmp_path, {})
    changed = _cycle_commit(repo, base, {
        "skills/batch-grep/SKILL.md": _skill_md("batch-grep", "Match many regex patterns in one pass", fm_name="batch_grep"),
    })
    assert _violations(repo, base, changed) == [
        "skill frontmatter: name 'batch_grep' does not match directory 'batch-grep': skills/batch-grep/SKILL.md"
    ]


def test_directory_without_skill_md_is_rejected(tmp_path):
    repo, base = _repo_with_skills(tmp_path, {})
    changed = _cycle_commit(repo, base, {"skills/helpers/util.py": "x = 1\n"})
    assert _violations(repo, base, changed) == [
        "skill layout: directory without SKILL.md: skills/helpers/"
    ]


def test_folded_description_scalar_is_accepted(tmp_path):
    repo, base = _repo_with_skills(tmp_path, {})
    changed = _cycle_commit(repo, base, {
        "skills/memory-lookup/SKILL.md": (
            "---\nname: memory-lookup\ndescription: >-\n  Find a specific fact or history entry\n"
            "  with one targeted read_file call\nversion: \"1.0.0\"\n---\n\nbody\n"
        ),
    })
    assert _violations(repo, base, changed) == []
    fm = gate._parse_skill_frontmatter((repo / "skills/memory-lookup/SKILL.md").read_text(encoding="utf-8"))
    assert fm == {
        "name": "memory-lookup",
        "description": "Find a specific fact or history entry with one targeted read_file call",
        "version": "1.0.0",
    }


def test_well_formed_new_skill_passes(tmp_path):
    repo, base = _repo_with_skills(tmp_path, {"run-tests": LIVE_TEST_SELECTION_SKILLS["run-tests"]})
    changed = _cycle_commit(repo, base, {
        "skills/posix-background-execution/SKILL.md": _skill_md(
            "posix-background-execution",
            "Run detached background processes from POSIX sh without bashisms like disown",
        ),
    })
    assert _violations(repo, base, changed) == []


def test_nested_files_are_resources_of_the_skill(tmp_path):
    repo, base = _repo_with_skills(tmp_path, {"run-tests": LIVE_TEST_SELECTION_SKILLS["run-tests"]})
    changed = _cycle_commit(repo, base, {"skills/run-tests/evals/cases.json": "[]\n"})
    assert changed == ["skills/run-tests/evals/cases.json"]
    assert _violations(repo, base, changed) == []


def test_non_skill_changes_are_ignored(tmp_path):
    repo, base = _repo_with_skills(tmp_path, {})
    changed = _cycle_commit(repo, base, {"scripts/foo.py": "x = 1\n"})
    assert _violations(repo, base, changed) == []


def test_cleanup_deleting_loose_file_and_renaming_snake_case_dir_passes(tmp_path):
    """The sibling cleanup line must be able to fix the tree through this gate."""
    repo, _ = _repo_with_skills(tmp_path, {"batch_grep": "Match multiple regex patterns in a single pass across files"})
    _write(repo, "skills/jsonl_stream_filter.py", "loose\n")
    base = _commit_all(repo, "drifted")
    changed = _cycle_commit(repo, base, {
        "skills/jsonl_stream_filter.py": None,
        "skills/batch_grep/SKILL.md": None,
        "skills/batch-grep/SKILL.md": _skill_md("batch-grep", "Match multiple regex patterns in a single pass across files"),
    })
    # production shape: the rename shows only its destination
    assert changed == ["skills/batch-grep/SKILL.md", "skills/jsonl_stream_filter.py"]
    assert _violations(repo, base, changed) == []


def test_renaming_one_member_of_a_duplicate_pair_is_not_a_new_skill(tmp_path):
    """Rename detection: the twin stays, the renamed skill is the same skill, not a duplicate."""
    repo, base = _repo_with_skills(tmp_path, {
        "run_targeted_tests_to_avoid_timeouts": LIVE_TEST_SELECTION_SKILLS["run-targeted-tests-to-avoid-timeouts"],
        "targeted-test-discovery": LIVE_TEST_SELECTION_SKILLS["targeted-test-discovery"],
    })
    _mv(repo, "skills/run_targeted_tests_to_avoid_timeouts", "skills/run-targeted-tests-to-avoid-timeouts")
    _write(
        repo, "skills/run-targeted-tests-to-avoid-timeouts/SKILL.md",
        _skill_md("run-targeted-tests-to-avoid-timeouts", LIVE_TEST_SELECTION_SKILLS["run-targeted-tests-to-avoid-timeouts"]),
    )
    _commit_all(repo, "rename")
    changed = [l for l in _git(repo, "diff", "--name-only", base, "HEAD").stdout.splitlines() if l]
    assert changed == ["skills/run-targeted-tests-to-avoid-timeouts/SKILL.md"]
    assert _violations(repo, base, changed) == []


def test_rename_leaving_resources_behind_flags_the_old_directory(tmp_path):
    repo, _ = _repo_with_skills(tmp_path, {"batch_grep": "Match multiple regex patterns in a single pass across files"})
    _write(repo, "skills/batch_grep/helper.py", "x = 1\n")
    base = _commit_all(repo, "with helper")
    _mv(repo, "skills/batch_grep/SKILL.md", "skills/batch-grep/SKILL.md")
    _write(repo, "skills/batch-grep/SKILL.md", _skill_md("batch-grep", "Match multiple regex patterns in a single pass across files"))
    _commit_all(repo, "half rename")
    changed = [l for l in _git(repo, "diff", "--name-only", base, "HEAD").stdout.splitlines() if l]
    out = _violations(repo, base, changed)
    assert "skill layout: directory without SKILL.md: skills/batch_grep/" in out
    assert "skill layout: directory name must match ^[a-z0-9]+(-[a-z0-9]+)*$: skills/batch_grep/" in out
    assert not any(v.startswith("skill duplicate") for v in out)


def test_quoted_non_ascii_path_is_already_a_surface_violation():
    """git quotes non-ASCII paths; such a path matches no allowed prefix and is blocked upstream."""
    quoted = '"skills/t\\303\\251st/SKILL.md"'
    _blocked, violations, _tier = gate._classify_mutation_surface([quoted])
    assert violations and "file outside allowed paths" in violations[0]
    assert gate._skill_hygiene_violations(Path("."), "HEAD", [quoted]) == []


# ─── duplicates ──────────────────────────────────────────────────────────────


def test_new_skill_duplicating_existing_description_is_rejected_naming_the_original(tmp_path):
    repo, base = _repo_with_skills(tmp_path, {
        "targeted-test-discovery": LIVE_TEST_SELECTION_SKILLS["targeted-test-discovery"],
        "memory-lookup": "Find a specific fact or history entry with one targeted read_file call",
    })
    changed = _cycle_commit(repo, base, {
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
    changed = _cycle_commit(repo, base, {
        "skills/run-tests/SKILL.md": _skill_md("run-tests", desc) + "\n## More\n\nExtended.\n",
    })
    assert _violations(repo, base, changed) == []


def test_two_incidental_shared_words_do_not_trip(tmp_path):
    repo, base = _repo_with_skills(tmp_path, {
        "targeted-test-discovery": LIVE_TEST_SELECTION_SKILLS["targeted-test-discovery"],
    })
    changed = _cycle_commit(repo, base, {
        "skills/batch-grep/SKILL.md": _skill_md(
            "batch-grep", "Match multiple regex patterns in a single pass across files"
        ),
    })
    assert _violations(repo, base, changed) == []


def test_git_failure_is_fail_closed(tmp_path):
    repo, base = _repo_with_skills(tmp_path, {})
    out = _violations(repo, "0" * 40, ["skills/x/SKILL.md"])
    assert len(out) == 1 and out[0].startswith("skill hygiene: cannot read skills/")


def test_unreadable_existing_skill_is_fail_closed(tmp_path, monkeypatch):
    repo, base = _repo_with_skills(tmp_path, {"memory-lookup": "Find a fact with one targeted read"})
    changed = _cycle_commit(repo, base, {
        "skills/batch-grep/SKILL.md": _skill_md("batch-grep", "Match multiple regex patterns in a single pass"),
    })
    real = gate._git_show_many

    def _broken(repo_root, ref, paths):
        out = real(repo_root, ref, paths)
        return {p: (None if ref != "HEAD" else t) for p, t in out.items()}

    monkeypatch.setattr(gate, "_git_show_many", _broken)
    out = _violations(repo, base, changed)
    assert out == [
        f"skill duplicate: cannot read existing skills/memory-lookup/SKILL.md at {base[:12]}; skills/batch-grep/ unverifiable"
    ]


def test_existing_descriptions_are_read_in_one_git_call(tmp_path, monkeypatch):
    repo, base = _repo_with_skills(tmp_path, {f"skill-{i}": f"Description number {i} of the fixture set" for i in range(12)})
    changed = _cycle_commit(repo, base, {"skills/new-one/SKILL.md": _skill_md("new-one", "Something entirely different here")})
    calls: list[int] = []
    real = subprocess.run

    def _counting(argv, *a, **kw):
        if "cat-file" in argv:
            calls.append(1)
        return real(argv, *a, **kw)

    monkeypatch.setattr(gate.subprocess, "run", _counting)
    assert _violations(repo, base, changed) == []
    assert len(calls) == 2  # one for the staged SKILL.md, one batch for all 12 peers


# ─── bridge wiring ───────────────────────────────────────────────────────────


def test_bridge_changed_files_carries_skill_hygiene_into_mutation_violations(tmp_path):
    from nanobot.runtime import bridge

    repo, base = _repo_with_skills(tmp_path, {})
    _cycle_commit(repo, base, {"skills/loose.py": "x\n"})
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
    assert payload["ok"] is True and payload["skills_total"] == 3
    assert payload["zero_read"] == [
        {"skill": "idle-skill", "reads_in_window": 0, "last_read": None},
        {"skill": "memory-lookup", "reads_in_window": 0, "last_read": old},
    ]
    assert summary == {"ok": True, "written": 2, "path": str(state / "demand" / "skill_census.json")}
    assert payload["reason"] is None


@pytest.mark.parametrize("corrupt", [
    None,                                              # missing file
    "{not json",                                       # invalid JSON
    '{"schema_version": "other", "reads": []}',        # wrong schema
    '{"schema_version": "skill-fitness-v1", "reads": 1}',  # reads not a list
])
def test_census_fails_open_to_an_empty_census_when_reads_are_unavailable(tmp_path, corrupt):
    """No data is not a proven zero: unavailable source -> ok False, zero_read []."""
    repo, _ = _repo_with_skills(tmp_path, {"run-tests": "a b c d"})
    state = tmp_path / "state"
    if corrupt is not None:
        path = state / "skill_fitness" / "reads.json"
        path.parent.mkdir(parents=True)
        path.write_text(corrupt, encoding="utf-8")
    result = skill_fitness.census(state, repo)
    assert result == {"ok": False, "reason": "reads_unavailable", "skills_total": 1, "zero_read": []}
    summary = skill_fitness.write_zero_read_census(state, repo)
    assert summary["ok"] is False and summary["written"] == 0
    payload = json.loads((state / "demand" / "skill_census.json").read_text(encoding="utf-8"))
    assert payload["ok"] is False and payload["reason"] == "reads_unavailable" and payload["zero_read"] == []


def test_valid_empty_reads_file_is_evidence_of_zero_reads(tmp_path):
    repo, _ = _repo_with_skills(tmp_path, {"run-tests": "a b c d"})
    state = tmp_path / "state"
    _reads(state, [])
    assert skill_fitness.census(state, repo) == {
        "ok": True,
        "skills_total": 1,
        "zero_read": [{"skill": "run-tests", "reads_in_window": 0, "last_read": None}],
    }


def test_census_window_uses_parsed_timestamps_not_strings(tmp_path):
    """'zzzz' sorts above every ISO date as a string; it must not count as a fresh read."""
    repo, _ = _repo_with_skills(tmp_path, {"run-tests": "a", "memory-lookup": "b", "idle-skill": "c"})
    state = tmp_path / "state"
    now = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)
    _reads(state, [
        {"skill": "run-tests", "ts": "zzzz", "confirmed": True},                       # unparseable
        {"skill": "memory-lookup", "ts": "2027-01-01T00:00:00Z", "confirmed": True},   # future
        {"skill": "idle-skill", "ts": "2026-09-04T12:00:00", "confirmed": True},       # naive: not aware
    ])
    result = skill_fitness.census(state, repo, now=now)
    assert result["ok"] is True
    assert [r["skill"] for r in result["zero_read"]] == ["idle-skill", "memory-lookup", "run-tests"]
    assert all(r["last_read"] is None for r in result["zero_read"])


def test_census_distinguishes_no_skills_from_failure(tmp_path):
    state = tmp_path / "state"
    _reads(state, [])
    assert skill_fitness.census(state, tmp_path / "no-repo") == {"ok": True, "skills_total": 0, "zero_read": []}
    assert skill_fitness.census(state, None)["ok"] is False  # type: ignore[arg-type]


def test_census_never_gates(tmp_path):
    """A skill with zero reads is not a hygiene violation."""
    repo, base = _repo_with_skills(tmp_path, {"run-tests": "Run the affected test gate locally"})
    changed = _cycle_commit(repo, base, {"skills/run-tests/SKILL.md": _skill_md("run-tests", "Run the affected test gate locally") + "more\n"})
    assert _violations(repo, base, changed) == []
    _reads(tmp_path / "state", [])
    assert skill_fitness.zero_read_census(tmp_path / "state", repo) == [
        {"skill": "run-tests", "reads_in_window": 0, "last_read": None}
    ]


def test_census_writer_is_declared_in_state_paths():
    from nanobot.runtime import state_paths

    assert "nanobot.runtime.skill_fitness:write_zero_read_census" in state_paths.STATE_PATH_WRITERS["demand"]
