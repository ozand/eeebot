"""Tests for knowledge lift harness (#1093)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from nanobot.runtime import demand, knowledge_lift, runtime_deny, scorecard


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    s = tmp_path / "state"
    s.mkdir()
    return s


def test_runtime_deny_and_scorecard_sidecars():
    """Knowledge lift module is runtime-denied and sidecars are in FITNESS_SIDECARS."""
    assert runtime_deny._is_runtime_deny("nanobot/runtime/knowledge_lift.py")
    assert knowledge_lift.SIDECAR_REL in scorecard.FITNESS_SIDECARS
    assert knowledge_lift.WATERMARK_REL in scorecard.FITNESS_SIDECARS


def test_default_off(monkeypatch, state_dir: Path):
    """When env is unset or false, knowledge lift is disabled and inert."""
    monkeypatch.delenv("SELFEVO_KNOWLEDGE_LIFT_ENABLED", raising=False)
    assert not knowledge_lift.is_enabled()

    plan = {
        "cases": [
            {
                "case_id": "test_1",
                "prompt": "Say hello",
                "assertions": [{"type": "contains", "value": "hello"}],
            }
        ]
    }

    def dummy_runner(prompt, with_knowledge, timeout):
        return {"output": "hello", "exit_code": 0, "tokens": 10}

    res = knowledge_lift.execute_knowledge_lift(state_dir, plan, runner=dummy_runner)
    assert res["status"] == "disabled"
    assert not (state_dir / knowledge_lift.SIDECAR_REL).exists()


def test_schema_validation_and_rejection():
    """Malformed or oversized plans fail closed without executing."""
    # Not a dict
    cases, err = knowledge_lift.validate_eval_plan(["not a dict"])
    assert cases == []
    assert err is not None

    # Missing cases
    cases, err = knowledge_lift.validate_eval_plan({})
    assert cases == []
    assert "cases" in err

    # Empty cases
    cases, err = knowledge_lift.validate_eval_plan({"cases": []})
    assert cases == []
    assert "empty" in err

    # Too many cases
    huge_cases = [
        {"case_id": f"c_{i}", "prompt": "p", "assertions": [{"type": "exit_code_zero"}]}
        for i in range(knowledge_lift.MAX_CASES_PER_SET + 1)
    ]
    cases, err = knowledge_lift.validate_eval_plan({"cases": huge_cases})
    assert cases == []
    assert "exceeds maximum" in err

    # Duplicate case_id
    dup_cases = [
        {"case_id": "dup", "prompt": "p", "assertions": [{"type": "exit_code_zero"}]},
        {"case_id": "dup", "prompt": "p2", "assertions": [{"type": "exit_code_zero"}]},
    ]
    cases, err = knowledge_lift.validate_eval_plan({"cases": dup_cases})
    assert cases == []
    assert "duplicate" in err

    # Invalid assertion type
    bad_assert = [
        {"case_id": "c1", "prompt": "p", "assertions": [{"type": "unknown_assert"}]}
    ]
    cases, err = knowledge_lift.validate_eval_plan({"cases": bad_assert})
    assert cases == []
    assert "unknown type" in err


def test_ab_execution_and_parent_atomic_rewrite(monkeypatch, state_dir: Path):
    """A/B run writes protected sidecar and survives forged rows."""
    monkeypatch.setenv("SELFEVO_KNOWLEDGE_LIFT_ENABLED", "1")

    plan = {
        "cases": [
            {
                "case_id": "lift_case",
                "prompt": "fix error X",
                "assertions": [
                    {"type": "contains", "value": "solution"},
                    {"type": "exit_code_zero"},
                ],
            }
        ]
    }

    # Runner succeeds only WITH knowledge
    def mock_runner(prompt, with_knowledge, timeout):
        if with_knowledge:
            return {"output": "solution applied", "exit_code": 0, "tokens": 150}
        return {"output": "failed error", "exit_code": 1, "tokens": 100}

    res = knowledge_lift.execute_knowledge_lift(state_dir, plan, runner=mock_runner)
    assert res["status"] == "completed"
    assert res["rows_written"] == 1

    summary = knowledge_lift.read_knowledge_lift_summary(state_dir)
    assert summary["total_evals"] == 1
    assert summary["pass_lift"] == 1
    assert summary["net_benefit"] is True

    # Test forged row discarded / parent atomic rewrite
    sidecar_path = state_dir / knowledge_lift.SIDECAR_REL
    with open(sidecar_path, "a", encoding="utf-8") as f:
        f.write("invalid json row\n")
        f.write(json.dumps({"forged": "no case_id"}) + "\n")

    summary2 = knowledge_lift.read_knowledge_lift_summary(state_dir)
    assert summary2["total_evals"] == 1  # Forged rows ignored

    # Run another plan with force=True, ensure rewrite produces clean rows
    plan2 = {
        "cases": [
            {
                "case_id": "case_2",
                "prompt": "do something",
                "assertions": [{"type": "contains", "value": "ok"}],
            }
        ]
    }
    res2 = knowledge_lift.execute_knowledge_lift(state_dir, plan2, runner=lambda p, k, t: {"output": "ok", "exit_code": 0, "tokens": 50}, force=True)
    assert res2["status"] == "completed"
    summary3 = knowledge_lift.read_knowledge_lift_summary(state_dir)
    assert summary3["total_evals"] == 2


def test_timeouts_and_hung_case(monkeypatch, state_dir: Path):
    """Per-case errors/timeouts and total timeouts are handled gracefully."""
    monkeypatch.setenv("SELFEVO_KNOWLEDGE_LIFT_ENABLED", "1")

    plan = {
        "cases": [
            {
                "case_id": "hang_case",
                "prompt": "hang",
                "timeout_seconds": 0.1,
                "assertions": [{"type": "exit_code_zero"}],
            },
            {
                "case_id": "fast_case",
                "prompt": "fast",
                "timeout_seconds": 1.0,
                "assertions": [{"type": "contains", "value": "fast"}],
            },
        ]
    }

    def hung_runner(prompt, with_knowledge, timeout):
        if prompt == "hang":
            raise TimeoutError("Execution timed out")
        return {"output": "fast result", "exit_code": 0, "tokens": 20}

    res = knowledge_lift.execute_knowledge_lift(
        state_dir,
        plan,
        runner=hung_runner,
        total_timeout_s=5.0,
    )
    assert res["status"] == "completed"
    assert res["rows_written"] == 2

    rows = knowledge_lift._read_eval_rows(state_dir / knowledge_lift.SIDECAR_REL)
    assert len(rows) == 2
    hang_row = next(r for r in rows if r["case_id"] == "hang_case")
    assert hang_row["with_pass"] is False
    assert hang_row["without_pass"] is False


def test_watermark_and_weekly_cap(monkeypatch, state_dir: Path, tmp_path: Path):
    """Digest watermark and weekly cap prevent runaway execution."""
    monkeypatch.setenv("SELFEVO_KNOWLEDGE_LIFT_ENABLED", "1")

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    lessons_dir = repo_dir / "lessons"
    lessons_dir.mkdir()
    (lessons_dir / "lessons.yaml").write_text("initial lesson content", encoding="utf-8")

    plan = {
        "cases": [
            {
                "case_id": "c1",
                "prompt": "p",
                "assertions": [{"type": "exit_code_zero"}],
            }
        ]
    }

    def runner(prompt, with_k, timeout):
        return {"output": "", "exit_code": 0, "tokens": 10}

    now = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)
    # First execution succeeds
    res1 = knowledge_lift.execute_knowledge_lift(
        state_dir, plan, runner=runner, selfevo_repo=repo_dir, now=now
    )
    assert res1["status"] == "completed"

    # Second execution immediately after without change is watermarked
    res2 = knowledge_lift.execute_knowledge_lift(
        state_dir, plan, runner=runner, selfevo_repo=repo_dir, now=now + timedelta(minutes=10)
    )
    assert res2["status"] == "watermarked"
    assert res2["reason"] == "knowledge_unchanged"

    # Updating knowledge allows new run
    (lessons_dir / "lessons.yaml").write_text("updated lesson content", encoding="utf-8")
    res3 = knowledge_lift.execute_knowledge_lift(
        state_dir, plan, runner=runner, selfevo_repo=repo_dir, now=now + timedelta(hours=1)
    )
    assert res3["status"] == "completed"

    # Weekly cap check: fill runs up to MAX_WEEKLY_RUNS
    watermark_path = state_dir / knowledge_lift.WATERMARK_REL
    wm = json.loads(watermark_path.read_text(encoding="utf-8"))
    base_ts = now.timestamp()
    wm["runs"] = [base_ts + i * 3600 for i in range(knowledge_lift.MAX_WEEKLY_RUNS)]
    watermark_path.write_text(json.dumps(wm), encoding="utf-8")

    (lessons_dir / "lessons.yaml").write_text("brand new content", encoding="utf-8")
    res_capped = knowledge_lift.execute_knowledge_lift(
        state_dir, plan, runner=runner, selfevo_repo=repo_dir, now=now + timedelta(hours=2)
    )
    assert res_capped["status"] == "rate_limited"
    assert res_capped["reason"] == "weekly_cap_exceeded"


def test_scorecard_and_negative_demand(state_dir: Path):
    """Scorecard reports knowledge_lift and negative lift mints defect demand."""
    # Write negative lift rows directly via atomic helper
    sidecar_path = state_dir / knowledge_lift.SIDECAR_REL
    rows = [
        {
            "schema": knowledge_lift.SCHEMA,
            "case_id": "neg_case_1",
            "with_pass": False,
            "without_pass": True,
            "with_tokens": 500,
            "without_tokens": 100,
            "with_duration_s": 2.5,
            "without_duration_s": 1.0,
            "delta_pass": -1,
            "delta_tokens": 400,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
    ]
    knowledge_lift._atomic_write_eval_rows(sidecar_path, rows)

    summary = knowledge_lift.read_knowledge_lift_summary(state_dir)
    assert summary["total_evals"] == 1
    assert summary["pass_lift"] == -1
    assert summary["net_benefit"] is False

    # Check scorecard includes knowledge_lift key
    sc = scorecard.compute_scorecard(state_dir, None, force=True)
    assert "knowledge_lift" in sc
    assert sc["knowledge_lift"]["total_evals"] == 1
    assert sc["knowledge_lift"]["pass_lift"] == -1
    assert sc["knowledge_lift"]["net_benefit"] is False

    # Check negative demand adapter
    defects = demand._knowledge_lift_defect_items(state_dir)
    assert len(defects) == 1
    assert "negative lift detected" in defects[0]["summary"]
    assert "reflection_context.py" in defects[0]["affected_path"]

    # Check demand collection integrates it
    items = demand.collect_demand(state_dir, None)
    demand_item = next((item for item in items if "Knowledge context negative lift" in item.get("summary", "")), None)
    assert demand_item is not None


# ─── #1104: finish_reason, warmup, and max_tokens in knowledge_lift ───────────


def _make_simple_plan() -> dict:
    """Minimal valid eval plan for testing."""
    return {
        "cases": [
            {
                "case_id": "test-1",
                "prompt": "say hello",
                "assertions": [{"type": "contains", "value": "hello"}],
                "task_title": "test task",
                "target_path": "test.py",
                "timeout_seconds": 5.0,
            }
        ]
    }


def test_run_eval_case_includes_finish_reason():
    """run_eval_case must include with_finish_reason and without_finish_reason."""
    def runner_with_reason(prompt, with_knowledge, timeout):
        return {"output": "hello", "exit_code": 0, "tokens": 10, "finish_reason": "stop"}

    plan = _make_simple_plan()
    cases, _ = knowledge_lift.validate_eval_plan(plan)
    row = knowledge_lift.run_eval_case(cases[0], runner_with_reason)
    assert "with_finish_reason" in row
    assert "without_finish_reason" in row
    assert row["with_finish_reason"] == "stop"
    assert row["without_finish_reason"] == "stop"


def test_run_eval_case_missing_finish_reason_defaults_to_empty():
    """Runner without finish_reason must not crash; defaults to empty string."""
    def runner_no_reason(prompt, with_knowledge, timeout):
        return {"output": "hello", "exit_code": 0, "tokens": 5}

    plan = _make_simple_plan()
    cases, _ = knowledge_lift.validate_eval_plan(plan)
    row = knowledge_lift.run_eval_case(cases[0], runner_no_reason)
    assert row["with_finish_reason"] == ""
    assert row["without_finish_reason"] == ""


def test_execute_knowledge_lift_warmup_counted_but_not_in_rows(monkeypatch, state_dir):
    """Warmup call must happen but produce no sidecar row."""
    monkeypatch.setenv("SELFEVO_KNOWLEDGE_LIFT_ENABLED", "1")
    calls: list[tuple] = []

    def spy_runner(prompt, with_knowledge, timeout):
        calls.append((prompt, with_knowledge))
        return {"output": "hello", "exit_code": 0, "tokens": 5, "finish_reason": "stop"}

    plan = _make_simple_plan()
    result = knowledge_lift.execute_knowledge_lift(
        state_dir, plan, runner=spy_runner, force=True
    )
    assert result["status"] == "completed"
    # warmup call present
    warmup_calls = [(p, w) for p, w in calls if p == "warmup"]
    assert len(warmup_calls) == 1
    # sidecar rows exclude warmup
    sidecar = state_dir / knowledge_lift.SIDECAR_REL
    rows = knowledge_lift._read_eval_rows(sidecar)
    assert all(r.get("case_id") != "warmup" for r in rows)


def test_validate_eval_plan_case_timeout_ceiling_follows_env(monkeypatch):
    """When SELFEVO_HARNESS_CASE_TIMEOUT_S=300, case timeout_seconds up to 300 is valid."""
    monkeypatch.setenv("SELFEVO_HARNESS_CASE_TIMEOUT_S", "300")
    plan = {
        "cases": [
            {
                "case_id": "t1",
                "prompt": "hello",
                "assertions": [{"type": "contains", "value": "hi"}],
                "task_title": "t",
                "target_path": "",
                "timeout_seconds": 250.0,  # > default 30, but <= 300
            }
        ]
    }
    cases, err = knowledge_lift.validate_eval_plan(plan)
    assert err is None
    assert cases[0]["timeout_seconds"] == 250.0
