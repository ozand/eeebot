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
