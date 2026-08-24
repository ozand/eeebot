from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from nanobot.runtime import demand


def _repo(tmp_path: Path, skill: str = "review") -> Path:
    repo = tmp_path / "repo"
    (repo / "skills" / skill).mkdir(parents=True)
    (repo / "skills" / skill / "SKILL.md").write_text("# skill\n", encoding="utf-8")
    import subprocess
    subprocess.run(["git", "-C", str(repo), "init", "-b", "main"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "add skill"], check=True, capture_output=True)
    return repo


def _sidecar(state: Path, skill: str, ts: str, confirmed: bool = True) -> None:
    path = state / "skill_fitness" / "reads.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_version": "skill-fitness-v1", "reads": [
        {"skill": skill, "ts": ts, "confirmed": confirmed}
    ]}), encoding="utf-8")


def test_idle_confirmed_skill_becomes_bounded_repair_item(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    now = datetime.now(timezone.utc)
    state = tmp_path / "state"
    _sidecar(state, "review", (now - timedelta(days=5)).isoformat().replace("+00:00", "Z"))

    items = demand._repair_unused_items(state, repo, now)

    skills = [i for i in items if i.get("affected_path") == "skills/review/SKILL.md"]
    assert len(skills) == 1
    assert skills[0]["kind"] == "defect"
    assert "re-wire idle skill" in skills[0]["summary"]


def test_never_read_young_skill_and_current_read_are_not_demands(tmp_path):
    repo = _repo(tmp_path)
    now = datetime.now(timezone.utc)
    state = tmp_path / "state"
    _sidecar(state, "review", now.isoformat().replace("+00:00", "Z"))

    assert not any(i.get("affected_path", "").startswith("skills/")
                   for i in demand._repair_unused_items(state, repo, now))


def test_never_read_old_skill_becomes_demand(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    now = datetime.now(timezone.utc)
    state = tmp_path / "state"
    monkeypatch.setattr("nanobot.runtime.usage_evidence._git_creation_iso", lambda *_: (now - timedelta(days=5)).isoformat())
    items = demand._repair_unused_items(state, repo, now)
    assert any(i.get("affected_path") == "skills/review/SKILL.md" for i in items)


def test_unconfirmed_read_is_not_usage(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    now = datetime.now(timezone.utc)
    state = tmp_path / "state"
    _sidecar(state, "review", (now - timedelta(days=5)).isoformat().replace("+00:00", "Z"), confirmed=False)
    monkeypatch.setattr("nanobot.runtime.usage_evidence._git_creation_iso", lambda *_: (now - timedelta(days=1)).isoformat())
    assert not any(i.get("affected_path") == "skills/review/SKILL.md" for i in demand._repair_unused_items(state, repo, now))


def test_forged_or_missing_skill_rows_do_not_create_arbitrary_demand(tmp_path):
    repo = _repo(tmp_path)
    now = datetime.now(timezone.utc)
    state = tmp_path / "state"
    _sidecar(state, "does-not-exist", (now - timedelta(days=10)).isoformat().replace("+00:00", "Z"))

    assert not any("does-not-exist" in i.get("summary", "")
                   for i in demand._repair_unused_items(state, repo, now))
