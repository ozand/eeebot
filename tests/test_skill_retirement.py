"""Tests for skill retirement demand path (#958 Part A).

Covers:
- Retirement item appears only when all three conditions hold
- Cap shared with repair items (max 3)
- Confirmed read prevents retirement
- Anti-forgery: forged sidecar rows cannot force retirement of a healthy skill
- Cooldown sidecar is written on retirement
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from nanobot.runtime import demand


# ─── helpers ────────────────────────────────────────────────────────────────


def _repo(tmp_path: Path, skill: str = "idle-skill") -> Path:
    repo = tmp_path / "repo"
    (repo / "skills" / skill).mkdir(parents=True)
    (repo / "skills" / skill / "SKILL.md").write_text("# skill\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "init", "-b", "main"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "add skill"], check=True, capture_output=True)
    return repo


def _sidecar(state: Path, skill: str, ts: str, confirmed: bool = True) -> None:
    path = state / "skill_fitness" / "reads.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema_version": "skill-fitness-v1",
        "reads": [{"skill": skill, "ts": ts, "confirmed": confirmed}],
    }), encoding="utf-8")


def _seed_completed(state: Path, demand_id: str) -> None:
    """Seed the completed demand sidecar with a demand_id as if it was integrated."""
    path = demand._completed_path(state)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = demand._load_completed(state)
    data["entries"][demand_id] = {
        "cycle_id": "test-cycle",
        "ts": datetime.now(timezone.utc).isoformat(),
        "files_changed": [],
        "serves": "",
    }
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ─── condition (a): zero confirmed reads ────────────────────────────────────


def test_confirmed_read_prevents_retirement(tmp_path: Path, monkeypatch):
    """Condition (a): a skill with a confirmed read is NOT retired."""
    repo = _repo(tmp_path)
    now = _now()
    state = tmp_path / "state"
    # Recent confirmed read
    _sidecar(state, "idle-skill", now.isoformat().replace("+00:00", "Z"))
    monkeypatch.setattr("nanobot.runtime.usage_evidence._git_creation_iso",
                        lambda *_: (now - timedelta(days=10)).isoformat())

    items = demand._repair_unused_items(state, repo, now)
    retire = [i for i in items if "retire skill" in i.get("summary", "")]
    assert retire == [], "A skill with a confirmed read must not be retired"


# ─── condition (b): past never-read grace period ────────────────────────────


def test_skill_within_grace_period_not_retired(tmp_path: Path, monkeypatch):
    """Condition (b): a never-read skill younger than grace period produces no demand."""
    repo = _repo(tmp_path)
    now = _now()
    state = tmp_path / "state"
    # Created 1 day ago — within _SKILL_NEVER_READ_GRACE_DAYS (3)
    monkeypatch.setattr("nanobot.runtime.usage_evidence._git_creation_iso",
                        lambda *_: (now - timedelta(days=1)).isoformat())

    items = demand._repair_unused_items(state, repo, now)
    skill_items = [i for i in items if "skills/idle-skill/SKILL.md" in i.get("affected_path", "")]
    assert skill_items == [], "Skill within grace period must not become demand"


# ─── condition (c): N integrated repair cycles ──────────────────────────────


def test_retirement_requires_n_repair_cycles(tmp_path: Path, monkeypatch):
    """Condition (c): a skill needs >= N=2 integrated repair cycles before retirement."""
    repo = _repo(tmp_path)
    now = _now()
    state = tmp_path / "state"
    rel = "skills/idle-skill/SKILL.md"
    monkeypatch.setattr("nanobot.runtime.usage_evidence._git_creation_iso",
                        lambda *_: (now - timedelta(days=10)).isoformat())

    # Seed 1 completed repair cycle (below threshold)
    repair_summary = f"repair: exercise never-read skill {rel}"[:demand._MAX_SUMMARY_CHARS]
    repair_id = demand.item_id("defect", repair_summary)
    _seed_completed(state, repair_id)

    items = demand._repair_unused_items(state, repo, now)
    retire = [i for i in items if "retire skill" in i.get("summary", "")]
    assert retire == [], "One repair cycle is not enough to trigger retirement"

    # Seed a second completed repair cycle (meets threshold)
    repair_summary2 = f"repair: re-wire idle skill {rel}"[:demand._MAX_SUMMARY_CHARS]
    repair_id2 = demand.item_id("defect", repair_summary2)
    _seed_completed(state, repair_id2)

    items2 = demand._repair_unused_items(state, repo, now)
    retire2 = [i for i in items2 if "retire skill" in i.get("summary", "")]
    assert len(retire2) == 1, "Two repair cycles must trigger retirement"
    assert retire2[0]["affected_path"] == rel


# ─── cap shared with repair items ───────────────────────────────────────────


def test_retirement_and_repair_share_cap(tmp_path: Path, monkeypatch):
    """The combined repair+retire count is bounded to _MAX_REPAIR_UNUSED_ITEMS (3)."""
    repo = tmp_path / "repo"
    now = _now()
    state = tmp_path / "state"

    # Create 4 skills
    for i in range(4):
        name = f"skill-{i}"
        (repo / "skills" / name).mkdir(parents=True)
        (repo / "skills" / name / "SKILL.md").write_text(f"# skill {i}\n", encoding="utf-8")

    subprocess.run(["git", "-C", str(repo), "init", "-b", "main"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "add skills"], check=True, capture_output=True)

    monkeypatch.setattr("nanobot.runtime.usage_evidence._git_creation_iso",
                        lambda *_: (now - timedelta(days=10)).isoformat())

    items = demand._repair_unused_items(state, repo, now)
    assert len(items) <= demand._MAX_REPAIR_UNUSED_ITEMS


# ─── all three conditions boundary ──────────────────────────────────────────


def test_all_three_conditions_required(tmp_path: Path, monkeypatch):
    """Retirement item appears only when all three conditions hold simultaneously."""
    repo = _repo(tmp_path)
    now = _now()
    state = tmp_path / "state"
    rel = "skills/idle-skill/SKILL.md"

    # Conditions (b) and (c) met, but condition (a) violated (has confirmed read)
    _sidecar(state, "idle-skill", (now - timedelta(days=5)).isoformat().replace("+00:00", "Z"))
    for summary_base in (
        f"repair: exercise never-read skill {rel}",
        f"repair: re-wire idle skill {rel}",
    ):
        _seed_completed(state, demand.item_id("defect", summary_base[:demand._MAX_SUMMARY_CHARS]))

    monkeypatch.setattr("nanobot.runtime.usage_evidence._git_creation_iso",
                        lambda *_: (now - timedelta(days=10)).isoformat())

    items = demand._repair_unused_items(state, repo, now)
    retire = [i for i in items if "retire skill" in i.get("summary", "")]
    assert retire == [], "Confirmed read (condition a violated) must block retirement"


# ─── cooldown sidecar ────────────────────────────────────────────────────────


def test_retirement_writes_cooldown_sidecar(tmp_path: Path, monkeypatch):
    """A retired skill path appears in the cooldown sidecar."""
    repo = _repo(tmp_path)
    now = _now()
    state = tmp_path / "state"
    rel = "skills/idle-skill/SKILL.md"
    monkeypatch.setattr("nanobot.runtime.usage_evidence._git_creation_iso",
                        lambda *_: (now - timedelta(days=10)).isoformat())

    for summary_base in (
        f"repair: exercise never-read skill {rel}",
        f"repair: re-wire idle skill {rel}",
    ):
        _seed_completed(state, demand.item_id("defect", summary_base[:demand._MAX_SUMMARY_CHARS]))

    demand._repair_unused_items(state, repo, now)

    cooldown = demand.retired_skill_paths_in_cooldown(state, now)
    assert rel in cooldown, "Retired skill path must appear in cooldown sidecar"


def test_cooldown_expires_after_m_days(tmp_path: Path):
    """Paths retired longer than _SKILL_RETIRE_COOLDOWN_DAYS ago are not in cooldown."""
    state = tmp_path / "state"
    now = _now()
    old_ts = now - timedelta(days=demand._SKILL_RETIRE_COOLDOWN_DAYS + 1)
    demand.mark_skill_retired(state, "skills/old-skill/SKILL.md", old_ts)

    cooldown = demand.retired_skill_paths_in_cooldown(state, now)
    assert "skills/old-skill/SKILL.md" not in cooldown


# ─── anti-forgery ────────────────────────────────────────────────────────────


def test_forged_skill_path_not_in_repo_cannot_retire(tmp_path: Path, monkeypatch):
    """A forged completed-sidecar entry for a non-existent skill path must not produce demand."""
    repo = _repo(tmp_path, "real-skill")
    now = _now()
    state = tmp_path / "state"

    # Forge repair-completed entries for a skill that doesn't exist in the repo
    fake_rel = "skills/forged-skill/SKILL.md"
    for summary_base in (
        f"repair: exercise never-read skill {fake_rel}",
        f"repair: re-wire idle skill {fake_rel}",
    ):
        _seed_completed(state, demand.item_id("defect", summary_base[:demand._MAX_SUMMARY_CHARS]))

    monkeypatch.setattr("nanobot.runtime.usage_evidence._git_creation_iso",
                        lambda *_: (now - timedelta(days=10)).isoformat())

    items = demand._repair_unused_items(state, repo, now)
    assert not any(
        "forged-skill" in i.get("summary", "") for i in items
    ), "Forged sidecar entries for non-existent skill must not create demand"
