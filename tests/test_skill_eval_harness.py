"""Tests for the harness-run A/B skill evals (#941)."""
from __future__ import annotations

import json
import time
from pathlib import Path

from nanobot.runtime import demand, runtime_deny, scorecard
from nanobot.runtime import skill_eval_harness as harness


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    skill = repo / "skills" / "demo"
    (skill / "evals").mkdir(parents=True)
    (skill / "SKILL.md").write_text("# demo\n", encoding="utf-8")
    (skill / "evals" / "evals.json").write_text(
        json.dumps({"evals": [{"id": "one", "prompt": "p", "expected_output": "ok", "assertions": ["ok"]}]}),
        encoding="utf-8",
    )
    return repo


def _runner(prompt, with_skill, path, timeout):
    return {"output": "ok" if with_skill else "no", "tokens": 3 if with_skill else 4, "duration": 0.01}


def test_default_off_and_validation(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.delenv(harness.ENABLED_ENV, raising=False)
    assert harness.evaluate_skill(tmp_path / "state", repo, "demo")["ran"] is False
    # Inert when off: nothing is created anywhere in the state dir.
    assert not (tmp_path / "state").exists()
    (repo / "skills" / "demo" / "evals" / "evals.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv(harness.ENABLED_ENV, "1")
    assert harness.load_cases(repo, "demo") is None


def test_oversized_or_malformed_plan_never_runs(tmp_path, monkeypatch):
    monkeypatch.setenv(harness.ENABLED_ENV, "1")
    repo = _repo(tmp_path)
    state = tmp_path / "state"
    calls: list[str] = []

    def spy(prompt, with_skill, path, timeout):
        calls.append(prompt)
        return {"output": "ok"}

    evals = repo / "skills" / "demo" / "evals" / "evals.json"
    good_case = {"id": "a", "prompt": "p", "expected_output": "o", "assertions": []}
    for bad in (
        "x" * (harness.MAX_EVAL_BYTES + 1),                    # oversized
        "not json",                                            # malformed
        json.dumps({"evals": []}),                             # empty
        json.dumps({"evals": [{"id": "a", "prompt": "p"}]}),   # missing keys
        json.dumps({"evals": [{**good_case, "assertions": [1]}]}),  # wrong types
        json.dumps({"evals": [good_case] * (harness.MAX_CASES + 1)}),  # too many
    ):
        evals.write_text(bad, encoding="utf-8")
        result = harness.evaluate_skill(state, repo, "demo", runner=spy)
        assert result["ran"] is False and result["reason"] == "invalid_eval_plan"
    assert calls == []
    assert not (state / harness.SIDECAR_REL).is_file()


def test_ab_delta_and_watermark(tmp_path, monkeypatch):
    monkeypatch.setenv(harness.ENABLED_ENV, "1")
    repo = _repo(tmp_path)
    state = tmp_path / "state"
    first = harness.evaluate_skill(state, repo, "demo", runner=_runner)
    assert first["ran"] and first["cases"][0]["delta"] == 1
    assert harness.evaluate_skill(state, repo, "demo", runner=_runner)["reason"] == "unchanged"
    assert len(harness.fitness_rows(state, "demo")) == 1


def test_changed_skill_reruns_within_weekly_cap(tmp_path, monkeypatch):
    monkeypatch.setenv(harness.ENABLED_ENV, "1")
    monkeypatch.setattr(harness, "MAX_WEEKLY_RUNS", 2)
    repo = _repo(tmp_path)
    state = tmp_path / "state"
    skill_md = repo / "skills" / "demo" / "SKILL.md"
    assert harness.evaluate_skill(state, repo, "demo", runner=_runner)["ran"]
    assert harness.evaluate_skill(state, repo, "demo", runner=_runner)["reason"] == "unchanged"
    skill_md.write_text("# demo v2\n", encoding="utf-8")
    assert harness.evaluate_skill(state, repo, "demo", runner=_runner)["ran"]
    skill_md.write_text("# demo v3\n", encoding="utf-8")
    third = harness.evaluate_skill(state, repo, "demo", runner=_runner)
    assert third["ran"] is False and third["reason"] == "weekly_cap"


def test_total_budget_gates_before_any_call(tmp_path, monkeypatch):
    monkeypatch.setenv(harness.ENABLED_ENV, "1")
    monkeypatch.setenv("SELFEVO_HARNESS_RUN_BUDGET_S", "0.001")
    # Simulate elapsed time > 0.001 s so the first remaining-check fires
    _t = [0.0]

    def _mono():
        v = _t[0]
        _t[0] += 0.01
        return v
    monkeypatch.setattr(harness.time, "monotonic", _mono)
    repo = _repo(tmp_path)
    calls: list[str] = []

    def spy(prompt, with_skill, path, timeout):
        calls.append(prompt)
        return {"output": "ok"}

    result = harness.evaluate_skill(tmp_path / "state", repo, "demo", runner=spy)
    assert result["ran"] is False and result["reason"] == "no_cases_run"
    assert calls == []


def test_sleeping_eval_cannot_stall(tmp_path, monkeypatch):
    monkeypatch.setenv(harness.ENABLED_ENV, "1")
    monkeypatch.setenv("SELFEVO_HARNESS_CASE_TIMEOUT_S", "0.05")
    monkeypatch.setattr(harness, "_JOIN_GRACE_SECONDS", 0.05)
    repo = _repo(tmp_path)
    state = tmp_path / "state"

    def sleeper(prompt, with_skill, path, timeout):
        time.sleep(30)
        return {"output": "ok"}

    started = time.monotonic()
    result = harness.evaluate_skill(state, repo, "demo", runner=sleeper)
    assert time.monotonic() - started < 5.0
    assert result["ran"]
    case = result["cases"][0]
    assert case["with"]["error"] == "timeout" and case["without"]["error"] == "timeout"
    assert case["with"]["pass"] is False and case["delta"] == 0


def test_forged_rows_are_replaced_and_negative_demand(tmp_path, monkeypatch):
    monkeypatch.setenv(harness.ENABLED_ENV, "1")
    repo = _repo(tmp_path)
    state = tmp_path / "state"
    path = state / harness.SIDECAR_REL
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"schema": harness.SCHEMA, "skill": "evil"}) + "\n", encoding="utf-8")

    def bad_runner(prompt, with_skill, path2, timeout):
        sidecar = state / harness.SIDECAR_REL
        with sidecar.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"schema": harness.SCHEMA, "skill": "forged"}) + "\n")
        return {"output": "ok", "duration": 0.01}

    harness.evaluate_skill(state, repo, "demo", runner=bad_runner)
    rows = harness.fitness_rows(state)
    assert rows and all(row["skill"] == "demo" for row in rows)
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
    assert "fails its own evals" in items[0]["summary"]
    wired = demand._skill_eval_defect_items(state)
    assert len(wired) == 1 and wired[0]["kind"] == "defect"
    assert wired[0]["id"] and wired[0]["affected_path"] == "skills/demo"


def test_passing_delta_produces_no_demand(tmp_path, monkeypatch):
    monkeypatch.setenv(harness.ENABLED_ENV, "1")
    repo = _repo(tmp_path)
    state = tmp_path / "state"
    harness.evaluate_skill(state, repo, "demo", runner=_runner)  # delta +1
    assert harness.negative_delta_demand(state) == []
    assert demand._skill_eval_defect_items(state) == []


def test_zero_delta_with_token_cost_is_demand(tmp_path, monkeypatch):
    monkeypatch.setenv(harness.ENABLED_ENV, "1")
    repo = _repo(tmp_path)
    state = tmp_path / "state"

    def runner(prompt, with_skill, path, timeout):
        return {"output": "ok", "tokens": 100 if with_skill else 10, "duration": 0.01}

    harness.evaluate_skill(state, repo, "demo", runner=runner)
    items = harness.negative_delta_demand(state)
    assert len(items) == 1 and "costs more than it buys" in items[0]["summary"]


def test_advisory_assertions_never_grade(tmp_path, monkeypatch):
    monkeypatch.setenv(harness.ENABLED_ENV, "1")
    repo = _repo(tmp_path)
    state = tmp_path / "state"
    (repo / "skills" / "demo" / "evals" / "evals.json").write_text(
        json.dumps({"evals": [{
            "id": "one", "prompt": "p", "expected_output": "ok",
            "assertions": ["ok", "llm: answer is polite"],
        }]}),
        encoding="utf-8",
    )
    result = harness.evaluate_skill(state, repo, "demo", runner=_runner)
    case = result["cases"][0]
    # The llm: assertion is recorded, but "ok" output still passes without it.
    assert case["with"]["pass"] is True and case["advisory"] == ["llm: answer is polite"]


def test_run_all_scans_and_respects_flag(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    state = tmp_path / "state"
    monkeypatch.delenv(harness.ENABLED_ENV, raising=False)
    assert harness.run_all(state, repo)["enabled"] is False
    monkeypatch.setenv(harness.ENABLED_ENV, "1")
    summary = harness.run_all(state, repo, runner=_runner)
    assert summary["skills"]["demo"] == {"ran": True, "reason": "ok"}


def test_run_all_skips_when_bridge_holds_lock(tmp_path, monkeypatch):
    monkeypatch.setenv(harness.ENABLED_ENV, "1")
    repo = _repo(tmp_path)
    state = tmp_path / "state"
    monkeypatch.setattr(harness, "_acquire_bridge_lock", lambda _state: None)
    summary = harness.run_all(state, repo, runner=_runner)
    assert summary["skipped"] == "bridge_busy" and summary["skills"] == {}
    assert not (state / harness.SIDECAR_REL).is_file()


def test_watermark_is_a_protected_sidecar():
    assert harness.WATERMARK_REL in scorecard.FITNESS_SIDECARS


# ─── #1104: finish_reason, warmup, and budget wiring ─────────────────────────


def test_finish_reason_in_arm_rows(tmp_path, monkeypatch):
    """Each arm row must contain a finish_reason field."""
    monkeypatch.setenv(harness.ENABLED_ENV, "1")
    repo = _repo(tmp_path)
    state = tmp_path / "state"

    def runner_with_reason(prompt, with_skill, path, timeout):
        return {"output": "ok", "tokens": 5, "duration": 0.01, "finish_reason": "stop"}

    result = harness.evaluate_skill(state, repo, "demo", runner=runner_with_reason)
    assert result["ran"]
    case = result["cases"][0]
    assert "finish_reason" in case["with"]
    assert "finish_reason" in case["without"]
    assert case["with"]["finish_reason"] == "stop"


def test_finish_reason_missing_from_runner_defaults_to_empty(tmp_path, monkeypatch):
    """Runner without finish_reason key should not crash; defaults to ''."""
    monkeypatch.setenv(harness.ENABLED_ENV, "1")
    repo = _repo(tmp_path)
    state = tmp_path / "state"

    def runner_no_reason(prompt, with_skill, path, timeout):
        return {"output": "ok", "tokens": 5, "duration": 0.01}

    result = harness.evaluate_skill(state, repo, "demo", runner=runner_no_reason)
    assert result["ran"]
    case = result["cases"][0]
    assert case["with"]["finish_reason"] == ""
    assert case["without"]["finish_reason"] == ""


def test_warmup_call_precedes_timed_cases(tmp_path, monkeypatch):
    """One warmup call (prompt='warmup', with_skill=False) must occur before
    the first timed case in a run_all invocation; it must NOT appear in the
    sidecar rows and must fire exactly once regardless of how many skills run."""
    monkeypatch.setenv(harness.ENABLED_ENV, "1")
    repo = _repo(tmp_path)
    # Add a second skill so we can confirm warmup fires only once total
    skill2 = repo / "skills" / "demo2"
    (skill2 / "evals").mkdir(parents=True)
    (skill2 / "SKILL.md").write_text("# demo2\n", encoding="utf-8")
    (skill2 / "evals" / "evals.json").write_text(
        json.dumps({"evals": [{"id": "two", "prompt": "q", "expected_output": "ok", "assertions": ["ok"]}]}),
        encoding="utf-8",
    )
    state = tmp_path / "state"
    calls: list[tuple] = []

    def spy_runner(prompt, with_skill, path, timeout):
        calls.append((prompt, with_skill))
        return {"output": "ok", "tokens": 1, "duration": 0.01}

    harness.run_all(state, repo, runner=spy_runner)
    # Exactly one warmup call total (not one per skill)
    warmup_calls = [c for c in calls if c[0] == "warmup"]
    assert len(warmup_calls) == 1
    # Warmup does not appear in sidecar rows
    rows = harness.fitness_rows(state)
    assert all(row.get("eval_id") != "warmup" for row in rows)
    # At least one real case was run after the warmup
    case_calls = [c for c in calls if c[0] != "warmup"]
    assert len(case_calls) >= 2  # at least one case x 2 arms


def test_env_resolved_run_budget_overrides_constant(tmp_path, monkeypatch):
    """Setting SELFEVO_HARNESS_RUN_BUDGET_S should control the per-skill budget."""
    monkeypatch.setenv(harness.ENABLED_ENV, "1")
    monkeypatch.setenv("SELFEVO_HARNESS_RUN_BUDGET_S", "1800")
    repo = _repo(tmp_path)
    state = tmp_path / "state"

    result = harness.evaluate_skill(state, repo, "demo", runner=_runner)
    assert result["ran"]


def test_env_resolved_case_timeout_300_accepted(tmp_path, monkeypatch):
    """SELFEVO_HARNESS_CASE_TIMEOUT_S=300 is within the new 600 clamp."""
    monkeypatch.setenv(harness.ENABLED_ENV, "1")
    monkeypatch.setenv("SELFEVO_HARNESS_CASE_TIMEOUT_S", "300")
    repo = _repo(tmp_path)
    state = tmp_path / "state"

    result = harness.evaluate_skill(state, repo, "demo", runner=_runner)
    assert result["ran"]
