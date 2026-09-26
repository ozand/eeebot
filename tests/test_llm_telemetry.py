"""Tests for nanobot.observability.llm_telemetry (issue #675)."""

import json
from datetime import datetime, timezone

import pytest

from nanobot.observability.llm_telemetry import (
    call_context,
    record_llm_call,
    record_llm_prompt,
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


def test_duration_and_matching_prompt_records_share_call_seq(tmp_path, monkeypatch):
    """Duration records must carry the same sequence as their prompt row."""
    monkeypatch.setenv("LLM_CALLS_DIR", str(tmp_path))
    monkeypatch.delenv("LLM_CAPTURE_PROMPTS", raising=False)

    with call_context("cycle-duration-seq", "bridge"):
        record_llm_call(model="m", duration_ms=1.0, usage={}, finish_reason="stop", retries=0)
        record_llm_prompt(
            messages=[{"role": "user", "content": "call one"}],
            content="one",
            reasoning_content=None,
            finish_reason="stop",
            model="m",
            prompt_tokens=1,
            completion_tokens=1,
        )

    duration = _read_jsonl(next(tmp_path.glob("*.jsonl")))[0]
    prompt = _read_jsonl(next((tmp_path / "prompts").glob("*.jsonl")))[0]
    assert duration["seq"] == prompt["seq"]


def test_call_sequence_does_not_leak_across_call_contexts(tmp_path, monkeypatch):
    """An unpaired duration cannot lend its seq to another context's prompt."""
    monkeypatch.setenv("LLM_CALLS_DIR", str(tmp_path))
    monkeypatch.delenv("LLM_CAPTURE_PROMPTS", raising=False)

    with call_context("cycle-first", "executor"):
        record_llm_call(model="m", duration_ms=1.0, usage={}, finish_reason="stop", retries=0)
        record_llm_call(model="m", duration_ms=1.0, usage={}, finish_reason="stop", retries=0)

    with call_context("cycle-second", "proposer"):
        record_llm_prompt(
            messages=[{"role": "user", "content": "prompt only"}],
            content="answer", reasoning_content=None, finish_reason="stop",
            model="m", prompt_tokens=1, completion_tokens=1,
        )

    prompt = _read_jsonl(next((tmp_path / "prompts").glob("*.jsonl")))[0]
    assert (prompt["cycle_id"], prompt["component"], prompt["seq"]) == (
        "cycle-second", "proposer", 1,
    )


def test_legacy_duration_row_without_seq_is_still_readable_by_duration_readers(
    tmp_path, monkeypatch
):
    """Existing readers use optional lookups and must accept old telemetry."""
    monkeypatch.setenv("LLM_CALLS_DIR", str(tmp_path))
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = tmp_path / f"{day}.jsonl"
    path.write_text(
        json.dumps({"cycle_id": "cycle-legacy", "component": "bridge", "duration_ms": 4.0}) + "\n",
        encoding="utf-8",
    )

    from scripts import llm_calls_report

    rows = llm_calls_report.load_records(tmp_path)
    assert rows[0].get("seq") is None
    assert llm_calls_report.aggregate(rows)["totals"]["calls"] == 1


def test_duration_seq_increments_with_each_call_and_stays_component_scoped(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_CALLS_DIR", str(tmp_path))
    monkeypatch.delenv("LLM_CAPTURE_PROMPTS", raising=False)

    with call_context("cycle-seq-multi", "bridge"):
        record_llm_call(model="m", duration_ms=1.0, usage={}, finish_reason="stop", retries=0)
        record_llm_prompt(
            messages=[{"role": "user", "content": "one"}],
            content="one",
            reasoning_content=None,
            finish_reason="stop",
            model="m",
            prompt_tokens=1,
            completion_tokens=1,
        )
        record_llm_call(model="m", duration_ms=1.0, usage={}, finish_reason="stop", retries=0)
        record_llm_prompt(
            messages=[{"role": "user", "content": "two"}],
            content="two",
            reasoning_content=None,
            finish_reason="stop",
            model="m",
            prompt_tokens=1,
            completion_tokens=1,
        )
    with call_context("cycle-seq-multi", "proposer"):
        record_llm_call(model="m", duration_ms=1.0, usage={}, finish_reason="stop", retries=0)
        record_llm_prompt(
            messages=[{"role": "user", "content": "propose"}],
            content="propose",
            reasoning_content=None,
            finish_reason="stop",
            model="m",
            prompt_tokens=1,
            completion_tokens=1,
        )

    durations = _read_jsonl(next(tmp_path.glob("*.jsonl")))
    prompts = _read_jsonl(next((tmp_path / "prompts").glob("*.jsonl")))
    assert [row["seq"] for row in durations] == [1, 2, 1]
    assert [row["seq"] for row in prompts] == [1, 2, 1]


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


def test_record_llm_call_logs_fs_failure(caplog, monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_CALLS_DIR", str(tmp_path))

    def _raise_open(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("builtins.open", _raise_open)
    with caplog.at_level("WARNING"):
        record_llm_call(
            model="executor-model", duration_ms=1.0, usage={}, finish_reason="error", retries=0
        )
    assert "llm call telemetry recording failed: disk full" in caplog.text


def test_record_llm_call_is_best_effort_on_bad_usage_shape(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_CALLS_DIR", str(tmp_path))

    # usage is not a dict-like object at all -> .get() would raise AttributeError.
    record_llm_call(model="m", duration_ms=1.0, usage="not-a-dict", finish_reason="stop", retries=0)  # type: ignore[arg-type]


# ─── #1755: context_window is written straight through, never guessed ──────


def test_record_llm_call_carries_context_window_for_a_known_model(tmp_path, monkeypatch):
    """The caller resolved a real window (e.g. via
    nanobot.providers.model_window.resolve_context_window) -- it lands in the
    row unchanged."""
    monkeypatch.setenv("LLM_CALLS_DIR", str(tmp_path))

    record_llm_call(
        model="un/qwen3.8-27b-gguf",
        duration_ms=1.0,
        usage={},
        finish_reason="stop",
        retries=0,
        context_window=98304,
    )

    rec = _read_jsonl(next(tmp_path.glob("*.jsonl")))[0]
    assert rec["context_window"] == 98304


def test_record_llm_call_defaults_context_window_to_none(tmp_path, monkeypatch):
    """A model whose window is unknown writes null, never a guess (e.g. never
    falling back to AgentDefaults.context_window_tokens) -- the default value
    of the parameter itself is None, and None round-trips as JSON null."""
    monkeypatch.setenv("LLM_CALLS_DIR", str(tmp_path))

    record_llm_call(
        model="unknown-model", duration_ms=1.0, usage={}, finish_reason="stop", retries=0
    )

    rec = _read_jsonl(next(tmp_path.glob("*.jsonl")))[0]
    assert "context_window" in rec
    assert rec["context_window"] is None


def test_record_llm_call_context_window_survives_a_registry_error(tmp_path, monkeypatch):
    """A caller whose window-resolution failed (network error, non-200,
    model absent from the registry response) passes context_window=None
    explicitly -- the row is still written, with null, not dropped."""
    monkeypatch.setenv("LLM_CALLS_DIR", str(tmp_path))

    record_llm_call(
        model="un/qwen3.8-27b-gguf",
        duration_ms=1.0,
        usage={},
        finish_reason="stop",
        retries=0,
        context_window=None,
    )

    rec = _read_jsonl(next(tmp_path.glob("*.jsonl")))[0]
    assert rec["context_window"] is None
