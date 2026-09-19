from __future__ import annotations

import json
import subprocess
from pathlib import Path

from nanobot.runtime import runtime_deny, scorecard, skill_fitness


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, text=True,
                          capture_output=True).stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    skill = repo / "skills" / "review" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("# Review\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "add skill")
    return repo, _git(repo, "rev-parse", "HEAD")


def test_later_cycle_read_is_confirmed(tmp_path: Path):
    repo, birth = _repo(tmp_path)
    (repo / "README.md").write_text("next\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "next cycle")
    base = _git(repo, "rev-parse", "HEAD")

    assert skill_fitness.record_skill_reads(
        state_dir=tmp_path / "state", reads=[{"skill": "review", "path": "skills/review/SKILL.md"}], repo=repo,
        cycle_id="later", cycle_base_sha=base,
    ) == 1
    rows = skill_fitness.confirmed_reads_for_cycle(tmp_path / "state", "later")
    assert len(rows) == 1
    assert rows[0]["skill_commit"] == birth


def test_authoring_cycle_and_missing_provenance_earn_zero(tmp_path: Path):
    repo, _birth = _repo(tmp_path)
    parent = _git(repo, "rev-parse", "HEAD^") if _git(repo, "rev-list", "--count", "HEAD") != "1" else "0" * 40
    state = tmp_path / "state"
    skill_fitness.record_skill_reads(state_dir=state, reads=[{"skill": "review", "path": "skills/review/SKILL.md"}], repo=repo,
                                     cycle_id="birth", cycle_base_sha=parent)
    skill_fitness.record_skill_reads(state_dir=state, reads=[{"skill": "unknown", "path": "skills/unknown/SKILL.md"}], repo=repo,
                                     cycle_id="unknown", cycle_base_sha="")
    assert skill_fitness.confirmed_reads_for_cycle(state, "birth") == []
    assert skill_fitness.confirmed_reads_for_cycle(state, "unknown") == []


def test_renamed_skill_keys_join_without_reset_and_unknown_keys_remain_visible(tmp_path: Path, monkeypatch):
    repo, _birth = _repo(tmp_path)
    renamed = repo / "skills" / "review-renamed" / "SKILL.md"
    (repo / "skills" / "review").rename(repo / "skills" / "review-renamed")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "rename review skill")
    state = tmp_path / "state"
    _reads = [
        {"skill": "review", "ts": "2026-09-01T00:00:00Z", "confirmed": True},
        {"skill": "review", "ts": "2026-09-02T00:00:00Z", "confirmed": True},
        {"skill": "deleted-skill", "ts": "2026-09-03T00:00:00Z", "confirmed": True},
    ]
    path = state / skill_fitness.SIDECAR_REL
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"schema_version": skill_fitness.SCHEMA_VERSION, "reads": _reads}), encoding="utf-8")

    inventory = skill_fitness.skill_fitness_inventory(state, repo)
    assert inventory["recorded_keys"] == 2
    assert inventory["resolved"] == 1
    assert inventory["unresolvable"] == 1
    assert inventory["confirmed_recorded_keys"] == 2
    assert inventory["confirmed_resolved"] == 1
    assert inventory["confirmed_unresolvable"] == 1
    assert inventory["unresolvable_keys"] == ["deleted-skill"]
    assert inventory["rename_map"] == {"review": "review-renamed"}
    assert skill_fitness.last_confirmed_skill_reads(state, repo) == {"review-renamed": "2026-09-02T00:00:00Z", "deleted-skill": "2026-09-03T00:00:00Z"}
    before = json.loads(path.read_text(encoding="utf-8"))["reads"]
    assert before == _reads

    migrated = skill_fitness.migrate_skill_keys(state, repo)
    assert migrated == {"ok": True, "migrated": 2, "unresolvable": 1, "changed": True}
    after = json.loads(path.read_text(encoding="utf-8"))["reads"]
    assert [row["skill"] for row in after] == ["review-renamed", "review-renamed", "deleted-skill"]
    assert after[0]["skill_key_original"] == "review"
    assert after[0]["skill_key_status"] == "migrated"
    assert after[2]["skill"] == "deleted-skill"
    assert after[2]["skill_key_status"] == "unresolvable"
    assert skill_fitness.migrate_skill_keys(state, repo)["changed"] is False


def test_operator_mapping_resolves_deleted_key_without_dropping_history(tmp_path: Path):
    repo, _birth = _repo(tmp_path)
    state = tmp_path / "state"
    path = state / skill_fitness.SIDECAR_REL
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"schema_version": skill_fitness.SCHEMA_VERSION, "reads": [
        {"skill": "legacy-review", "ts": "2026-09-01T00:00:00Z", "confirmed": True},
    ]}), encoding="utf-8")
    mapping = state / skill_fitness._RENAME_MAP_REL
    mapping.write_text(json.dumps({"legacy-review": "review"}), encoding="utf-8")

    inventory = skill_fitness.skill_fitness_inventory(state, repo)
    assert inventory["resolved"] == 1
    assert inventory["unresolvable"] == 0
    assert skill_fitness.migrate_skill_keys(state, repo)["migrated"] == 1
    row = json.loads(path.read_text(encoding="utf-8"))["reads"][0]
    assert row["skill"] == "review"
    assert row["skill_key_original"] == "legacy-review"


def test_sidecar_is_protected_and_module_denied(tmp_path: Path):
    assert skill_fitness.SIDECAR_REL in scorecard.FITNESS_SIDECARS
    assert runtime_deny._is_runtime_deny("nanobot/runtime/skill_fitness.py")
    state = tmp_path / "state"
    path = state / skill_fitness.SIDECAR_REL
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"schema_version": skill_fitness.SCHEMA_VERSION, "reads": []}))
    before = scorecard.fitness_sidecar_hashes(state)[skill_fitness.SIDECAR_REL]
    path.write_text(json.dumps({"schema_version": skill_fitness.SCHEMA_VERSION,
                                "reads": [{"skill": "forged"}]}))
    after = scorecard.fitness_sidecar_hashes(state)[skill_fitness.SIDECAR_REL]
    assert before != after


def test_cycle_skill_scan_records_zero_reads(tmp_path: Path):
    """#1666 phase 1 / #1654: a cycle that read no skills must still leave a
    row -- present with skill_count=0 -- distinguishable from a cycle where
    this was never called at all (no row)."""
    state = tmp_path / "state"
    skill_fitness.record_cycle_skill_scan(state, cycle_id="cycle-empty", skills_read=[])
    rows = [json.loads(l) for l in (state / skill_fitness.CYCLE_SCAN_REL).read_text(encoding="utf-8").splitlines()]
    assert rows == [{
        "cycle_id": "cycle-empty", "ts": rows[0]["ts"], "skill_count": 0, "skills_read": [],
        # #1767: always written, empty included -- the same reason the row
        # itself is unconditional.
        "skills_attempted_not_found": [],
    }]


def test_cycle_skill_scan_records_positive_reads(tmp_path: Path):
    state = tmp_path / "state"
    skill_fitness.record_cycle_skill_scan(
        state, cycle_id="cycle-hit", skills_read=["review", "run-tests", "review"],
    )
    rows = [json.loads(l) for l in (state / skill_fitness.CYCLE_SCAN_REL).read_text(encoding="utf-8").splitlines()]
    assert rows[0]["skill_count"] == 3
    assert rows[0]["skills_read"] == ["review", "run-tests"]  # deduped, sorted


def test_cycle_skill_scan_is_bounded(tmp_path: Path):
    state = tmp_path / "state"
    for i in range(skill_fitness._MAX_CYCLE_SCANS + 5):
        skill_fitness.record_cycle_skill_scan(state, cycle_id=f"cycle-{i}", skills_read=[])
    rows = (state / skill_fitness.CYCLE_SCAN_REL).read_text(encoding="utf-8").splitlines()
    assert len(rows) == skill_fitness._MAX_CYCLE_SCANS


def test_cycle_skill_scan_never_raises(tmp_path: Path, monkeypatch):
    def broken_mkdir(self, *a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "mkdir", broken_mkdir)
    skill_fitness.record_cycle_skill_scan(tmp_path / "state", cycle_id="cycle-x", skills_read=["review"])


# ---------------------------------------------------------------------------
# #1767 -- the failure half of the skill-read question
# ---------------------------------------------------------------------------

def test_cycle_skill_scan_records_attempted_but_missing_skills(tmp_path: Path):
    """#1767: `skill_count: 0` was ambiguous between "never asked" and
    "asked and was refused" -- two findings whose fixes point in opposite
    directions. The row now carries both halves."""
    state = tmp_path / "state"
    skill_fitness.record_cycle_skill_scan(
        state,
        cycle_id="cycle-miss",
        skills_read=[],
        skills_attempted_not_found=["skip-when-done", "skip-when-done", "targeted-test-discovery"],
    )
    rows = [json.loads(l) for l in (state / skill_fitness.CYCLE_SCAN_REL).read_text(encoding="utf-8").splitlines()]
    assert rows[0]["skill_count"] == 0
    assert rows[0]["skills_read"] == []
    assert rows[0]["skills_attempted_not_found"] == ["skip-when-done", "targeted-test-discovery"]


def test_cycle_skill_scan_attempted_field_is_always_present(tmp_path: Path):
    """Omitting the key when nothing failed would rebuild, in a new field,
    exactly the no-data-versus-zero ambiguity this row exists to remove."""
    state = tmp_path / "state"
    skill_fitness.record_cycle_skill_scan(state, cycle_id="cycle-a", skills_read=["review"])
    rows = [json.loads(l) for l in (state / skill_fitness.CYCLE_SCAN_REL).read_text(encoding="utf-8").splitlines()]
    assert "skills_attempted_not_found" in rows[0]
    assert rows[0]["skills_attempted_not_found"] == []


def test_cycle_skill_scan_never_raises_on_bad_attempted_values(tmp_path: Path):
    """Fail-open, same discipline as every other writer here."""
    state = tmp_path / "state"
    skill_fitness.record_cycle_skill_scan(
        state, cycle_id="cycle-b", skills_read=[], skills_attempted_not_found=["", "  ", "ok"],
    )
    rows = [json.loads(l) for l in (state / skill_fitness.CYCLE_SCAN_REL).read_text(encoding="utf-8").splitlines()]
    assert rows[0]["skills_attempted_not_found"] == ["ok"]
