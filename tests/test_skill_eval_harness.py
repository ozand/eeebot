from __future__ import annotations

import json
from pathlib import Path

from nanobot.runtime import runtime_deny, scorecard
from nanobot.runtime import skill_eval_harness as harness


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    skill = repo / "skills" / "demo"
    (skill / "evals").mkdir(parents=True)
    (skill / "SKILL.md").write_text("# demo\n", encoding="utf-8")
    (skill / "evals" / "evals.json").write_text(json.dumps({"evals": [{"id": "one", "prompt": "p", "expected_output": "ok", "assertions": ["ok"]}]}), encoding="utf-8")
    return repo


def _runner(prompt, with_skill, path, timeout):
    return {"output": "ok" if with_skill else "no", "tokens": 3 if with_skill else 4, "duration": 0.01}


def test_default_off_and_validation(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.delenv(harness.ENABLED_ENV, raising=False)
    assert harness.evaluate_skill(tmp_path / "state", repo, "demo")["ran"] is False
    (repo / "skills" / "demo" / "evals" / "evals.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv(harness.ENABLED_ENV, "1")
    assert harness.load_cases(repo, "demo") is None


def test_ab_delta_and_watermark(tmp_path, monkeypatch):
    monkeypatch.setenv(harness.ENABLED_ENV, "1")
    repo = _repo(tmp_path)
    state = tmp_path / "state"
    first = harness.evaluate_skill(state, repo, "demo", runner=_runner)
    assert first["ran"] and first["cases"][0]["delta"] == 1
    assert harness.evaluate_skill(state, repo, "demo", runner=_runner)["reason"] == "unchanged"
    assert len(harness.fitness_rows(state, "demo")) == 1


def test_forged_rows_are_replaced_and_negative_demand(tmp_path, monkeypatch):
    monkeypatch.setenv(harness.ENABLED_ENV, "1")
    repo = _repo(tmp_path)
    state = tmp_path / "state"
    path = state / harness.SIDECAR_REL
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"schema": harness.SCHEMA, "skill": "evil"}) + "\n", encoding="utf-8")
    def bad_runner(prompt, with_skill, path, timeout):
        path2 = state / harness.SIDECAR_REL
        with path2.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"schema": harness.SCHEMA, "skill": "forged"}) + "\n")
        return {"output": "ok", "duration": 0.01}
    harness.evaluate_skill(state, repo, "demo", runner=bad_runner)
    rows = harness.fitness_rows(state)
    assert all(row["skill"] == "demo" for row in rows)
    assert harness.SIDECAR_REL in scorecard.FITNESS_SIDECARS
    assert runtime_deny._is_runtime_deny("nanobot/runtime/skill_eval_harness.py")


def test_negative_delta_is_bounded_demand(tmp_path, monkeypatch):
    monkeypatch.setenv(harness.ENABLED_ENV, "1")
    repo = _repo(tmp_path)
    state = tmp_path / "state"
    def runner(prompt, with_skill, path, timeout):
        return {"output": "ok" if not with_skill else "bad", "duration": 0.01}
    harness.evaluate_skill(state, repo, "demo", runner=runner)
    items = harness.negative_delta_demand(state)
    assert len(items) == 1 and items[0]["kind"] == "defect"
