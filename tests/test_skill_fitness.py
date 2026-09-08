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
