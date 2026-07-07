"""Tests for nanobot.observability.llm_telemetry (issue #675)."""

import json

import pytest

from nanobot.observability.llm_telemetry import (
    call_context,
    record_llm_call,
    reset_call_context,
    set_call_context,
)


def _read_jsonl(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def test_record_llm_call_writes_well_formed_line(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_CALLS_DIR", str(tmp_path))

    record_llm_call(
        model="un/qwen3.6-27b-mtp",
        duration_ms=123.456,
        usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        finish_reason="stop",
        retries=0,
    )

    files = list(tmp_path.glob("*.jsonl"))
    assert len(files) == 1
    records = _read_jsonl(files[0])
    assert len(records) == 1
    rec = records[0]

    assert rec["model"] == "un/qwen3.6-27b-mtp"
    assert rec["duration_ms"] == pytest.approx(123.456)
    assert rec["prompt_tokens"] == 10
    assert rec["completion_tokens"] == 5
    assert rec["total_tokens"] == 15
    assert rec["finish_reason"] == "stop"
    assert rec["retries"] == 0
    assert rec["cycle_id"] == ""
    assert rec["component"] == ""
    assert rec["ts"].endswith("Z")


def test_record_llm_call_defaults_missing_usage_fields_to_zero(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_CALLS_DIR", str(tmp_path))

    record_llm_call(model=None, duration_ms=1.0, usage=None, finish_reason=None, retries=2)

    rec = _read_jsonl(next(tmp_path.glob("*.jsonl")))[0]
    assert rec["model"] == ""
    assert rec["prompt_tokens"] == 0
    assert rec["completion_tokens"] == 0
    assert rec["total_tokens"] == 0
    assert rec["finish_reason"] == ""
    assert rec["retries"] == 2


def test_state_dir_fallback_used_when_llm_calls_dir_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("LLM_CALLS_DIR", raising=False)
    monkeypatch.setenv("STATE_DIR", str(tmp_path))

    record_llm_call(model="m", duration_ms=1.0, usage={}, finish_reason="stop", retries=0)

    assert (tmp_path / "llm_calls").is_dir()
    assert list((tmp_path / "llm_calls").glob("*.jsonl"))


def test_call_context_flows_into_record(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_CALLS_DIR", str(tmp_path))

    with call_context("cycle-abc", "bridge"):
        record_llm_call(model="m", duration_ms=1.0, usage={}, finish_reason="stop", retries=0)

    rec = _read_jsonl(next(tmp_path.glob("*.jsonl")))[0]
    assert rec["cycle_id"] == "cycle-abc"
    assert rec["component"] == "bridge"


def test_call_context_nesting_restores_prior_context(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_CALLS_DIR", str(tmp_path))

    with call_context("outer", "coordinator"):
        with call_context("inner", "tool_harness"):
            record_llm_call(model="m", duration_ms=1.0, usage={}, finish_reason="stop", retries=0)
        record_llm_call(model="m", duration_ms=1.0, usage={}, finish_reason="stop", retries=0)

    records = _read_jsonl(next(tmp_path.glob("*.jsonl")))
    assert records[0]["cycle_id"] == "inner"
    assert records[0]["component"] == "tool_harness"
    assert records[1]["cycle_id"] == "outer"
    assert records[1]["component"] == "coordinator"


def test_set_and_reset_call_context_token(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_CALLS_DIR", str(tmp_path))

    token = set_call_context("cycle-1", "bridge")
    record_llm_call(model="m", duration_ms=1.0, usage={}, finish_reason="stop", retries=0)
    reset_call_context(token)
    record_llm_call(model="m", duration_ms=1.0, usage={}, finish_reason="stop", retries=0)

    records = _read_jsonl(next(tmp_path.glob("*.jsonl")))
    assert records[0]["cycle_id"] == "cycle-1"
    assert records[1]["cycle_id"] == ""


def test_record_llm_call_is_best_effort_on_fs_error(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_CALLS_DIR", str(tmp_path))

    def _raise_open(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("builtins.open", _raise_open)

    # Must not raise despite the simulated fs failure.
    record_llm_call(model="m", duration_ms=1.0, usage={}, finish_reason="stop", retries=0)


def test_record_llm_call_is_best_effort_on_bad_usage_shape(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_CALLS_DIR", str(tmp_path))

    # usage is not a dict-like object at all -> .get() would raise AttributeError.
    record_llm_call(model="m", duration_ms=1.0, usage="not-a-dict", finish_reason="stop", retries=0)  # type: ignore[arg-type]
