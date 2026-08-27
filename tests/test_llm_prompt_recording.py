"""Tests for nanobot.observability.llm_telemetry.record_llm_prompt (issue #693)."""

from __future__ import annotations

import gzip
import json
from datetime import datetime, timedelta, timezone

import pytest

from nanobot.observability.llm_telemetry import call_context, record_llm_prompt
from scripts import llm_prompt_inspect


def _read_jsonl(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _read_jsonl_gz(path):
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _sample_messages():
    return [
        {"role": "system", "content": "You are a helpful agent."},
        {"role": "user", "content": "Do the thing."},
        {"role": "assistant", "content": "Sure, doing it now."},
    ]


def test_record_llm_prompt_writes_well_formed_line(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_CALLS_DIR", str(tmp_path))
    monkeypatch.delenv("LLM_CAPTURE_PROMPTS", raising=False)

    with call_context("cycle-1", "coordinator"):
        record_llm_prompt(
            messages=_sample_messages(),
            content="all done",
            reasoning_content="thinking...",
            finish_reason="stop",
            model="un/qwen3.6-27b-mtp",
            prompt_tokens=42,
            completion_tokens=7,
        )

    prompts_dir = tmp_path / "prompts"
    files = list(prompts_dir.glob("*.jsonl"))
    assert len(files) == 1
    records = _read_jsonl(files[0])
    assert len(records) == 1
    rec = records[0]

    assert rec["model"] == "un/qwen3.6-27b-mtp"
    assert rec["cycle_id"] == "cycle-1"
    assert rec["component"] == "coordinator"
    assert rec["seq"] == 1
    assert rec["prompt_tokens"] == 42
    assert rec["completion_tokens"] == 7
    assert rec["finish_reason"] == "stop"
    assert rec["content"] == "all done"
    assert rec["reasoning_content"] == "thinking..."
    assert rec["messages"] == _sample_messages()
    assert rec["ts"].endswith("Z")


def test_record_llm_prompt_sequence_increments_per_cycle(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_CALLS_DIR", str(tmp_path))

    with call_context("cycle-seq", "bridge"):
        record_llm_prompt(
            messages=_sample_messages(), content="a", reasoning_content=None,
            finish_reason="stop", model="m", prompt_tokens=1, completion_tokens=1,
        )
        record_llm_prompt(
            messages=_sample_messages(), content="b", reasoning_content=None,
            finish_reason="stop", model="m", prompt_tokens=1, completion_tokens=1,
        )

    records = _read_jsonl(next((tmp_path / "prompts").glob("*.jsonl")))
    assert [r["seq"] for r in records] == [1, 2]


def test_toggle_off_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_CALLS_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_CAPTURE_PROMPTS", "0")

    record_llm_prompt(
        messages=_sample_messages(), content="x", reasoning_content=None,
        finish_reason="stop", model="m", prompt_tokens=1, completion_tokens=1,
    )

    assert not (tmp_path / "prompts").exists() or not list((tmp_path / "prompts").glob("*.jsonl"))


@pytest.mark.parametrize("off_value", ["0", "false", "False", ""])
def test_toggle_off_variants(tmp_path, monkeypatch, off_value):
    monkeypatch.setenv("LLM_CALLS_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_CAPTURE_PROMPTS", off_value)

    record_llm_prompt(
        messages=_sample_messages(), content="x", reasoning_content=None,
        finish_reason="stop", model="m", prompt_tokens=1, completion_tokens=1,
    )

    prompts_dir = tmp_path / "prompts"
    assert not prompts_dir.exists() or not list(prompts_dir.glob("*.jsonl"))


def test_toggle_on_explicit_value(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_CALLS_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_CAPTURE_PROMPTS", "1")

    record_llm_prompt(
        messages=_sample_messages(), content="x", reasoning_content=None,
        finish_reason="stop", model="m", prompt_tokens=1, completion_tokens=1,
    )

    assert list((tmp_path / "prompts").glob("*.jsonl"))


def test_secret_scrub_redacts_persisted_record(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_CALLS_DIR", str(tmp_path))

    messages = [
        {"role": "user", "content": "here is my key sk-abcdefghijklmnop and Bearer abcdefgh12345678"},
    ]
    record_llm_prompt(
        messages=messages, content="ok", reasoning_content=None,
        finish_reason="stop", model="m", prompt_tokens=1, completion_tokens=1,
    )

    files = list((tmp_path / "prompts").glob("*.jsonl"))
    raw = files[0].read_text(encoding="utf-8")
    assert "sk-abcdefghijklmnop" not in raw
    assert "Bearer abcdefgh12345678" not in raw
    assert "[REDACTED]" in raw


def test_day_rollover_gzips_previous_day_and_prunes_old(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_CALLS_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_PROMPTS_RETENTION_DAYS", "14")

    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir(parents=True)

    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    too_old = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")

    yesterday_file = prompts_dir / f"{yesterday}.jsonl"
    yesterday_file.write_text(json.dumps({"seq": 1}) + "\n", encoding="utf-8")

    old_gz = prompts_dir / f"{too_old}.jsonl.gz"
    with gzip.open(old_gz, "wt", encoding="utf-8") as fh:
        fh.write(json.dumps({"seq": 1}) + "\n")

    record_llm_prompt(
        messages=_sample_messages(), content="today", reasoning_content=None,
        finish_reason="stop", model="m", prompt_tokens=1, completion_tokens=1,
    )

    # Yesterday's plain file got gzipped and removed.
    assert not yesterday_file.exists()
    assert (prompts_dir / f"{yesterday}.jsonl.gz").exists()
    gz_records = _read_jsonl_gz(prompts_dir / f"{yesterday}.jsonl.gz")
    assert gz_records == [{"seq": 1}]

    # The too-old archive got pruned.
    assert not old_gz.exists()

    # Today's file stays plain and appendable.
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert (prompts_dir / f"{today}.jsonl").exists()


def test_best_effort_open_failure_does_not_raise(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_CALLS_DIR", str(tmp_path))

    def _raise_open(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("builtins.open", _raise_open)

    record_llm_prompt(
        messages=_sample_messages(), content="x", reasoning_content=None,
        finish_reason="stop", model="m", prompt_tokens=1, completion_tokens=1,
    )


def test_best_effort_gzip_failure_does_not_raise(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_CALLS_DIR", str(tmp_path))

    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir(parents=True)
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    (prompts_dir / f"{yesterday}.jsonl").write_text("{}\n", encoding="utf-8")

    import nanobot.observability.llm_telemetry as telemetry

    def _raise_gzip_open(*args, **kwargs):
        raise OSError("no space")

    monkeypatch.setattr(telemetry.gzip, "open", _raise_gzip_open)

    # Must not raise despite the simulated gzip failure; today's write still happens.
    record_llm_prompt(
        messages=_sample_messages(), content="x", reasoning_content=None,
        finish_reason="stop", model="m", prompt_tokens=1, completion_tokens=1,
    )
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert (prompts_dir / f"{today}.jsonl").exists()


# --- scripts/llm_prompt_inspect.py -----------------------------------------


def test_llm_prompt_inspect_aggregates_plain_and_gz(tmp_path):
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir(parents=True)

    plain_record = {
        "ts": "2026-07-01T00:00:00Z",
        "model": "m1",
        "cycle_id": "cycle-a",
        "component": "coordinator",
        "seq": 1,
        "prompt_tokens": 100,
        "completion_tokens": 10,
        "finish_reason": "stop",
        "messages": [
            {"role": "system", "content": "x" * 40},
            {"role": "user", "content": "y" * 20},
        ],
        "content": "response text",
        "reasoning_content": None,
    }
    gz_record = {
        "ts": "2026-06-30T00:00:00Z",
        "model": "m2",
        "cycle_id": "cycle-b",
        "component": "bridge",
        "seq": 1,
        "prompt_tokens": 50,
        "completion_tokens": 5,
        "finish_reason": "stop",
        "messages": [{"role": "user", "content": "z" * 8}],
        "content": "ok",
        "reasoning_content": None,
    }

    (prompts_dir / "2026-07-01.jsonl").write_text(json.dumps(plain_record) + "\n", encoding="utf-8")
    with gzip.open(prompts_dir / "2026-06-30.jsonl.gz", "wt", encoding="utf-8") as fh:
        fh.write(json.dumps(gz_record) + "\n")

    records = llm_prompt_inspect.load_records(prompts_dir)
    assert len(records) == 2

    cycle_a = llm_prompt_inspect.select_calls(records, "cycle-a", None)
    assert len(cycle_a) == 1

    summary = llm_prompt_inspect.summarize_call(cycle_a[0])
    assert summary["message_count"] == 2
    assert summary["total_message_bytes"] == 60
    assert summary["total_message_est_tokens"] == 60 // 4
    assert summary["response_bytes"] == len("response text")

    call = llm_prompt_inspect.select_calls(records, "cycle-b", 1)
    assert len(call) == 1
    assert call[0]["model"] == "m2"


def test_llm_prompt_inspect_date_filter(tmp_path):
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir(parents=True)
    (prompts_dir / "2026-07-01.jsonl").write_text(
        json.dumps({"seq": 1, "cycle_id": "a", "messages": []}) + "\n", encoding="utf-8"
    )
    (prompts_dir / "2026-07-02.jsonl").write_text(
        json.dumps({"seq": 1, "cycle_id": "b", "messages": []}) + "\n", encoding="utf-8"
    )

    records = llm_prompt_inspect.load_records(prompts_dir, date="2026-07-01")
    assert len(records) == 1
    assert records[0]["cycle_id"] == "a"


def test_telemetry_hook_passes_max_days_and_rate_limits(tmp_path, monkeypatch):
    """Issue #1059: telemetry prompt hook only scans recent days and enforces rate limit."""
    from nanobot.observability.llm_telemetry import call_context, record_llm_prompt

    monkeypatch.setenv("LLM_CALLS_DIR", str(tmp_path / "state" / "llm_calls"))

    calls = []

    def mock_build_action_index(state_root, prompts_dir=None, *, max_days=None, force_regenerate=False):
        calls.append({"max_days": max_days, "prompts_dir": prompts_dir})
        return {}

    monkeypatch.setattr("nanobot.runtime.action_index.build_action_index", mock_build_action_index)

    # First call triggers indexer with max_days=2 (yesterday + today)
    with call_context("cycle-1", "coordinator"):
        record_llm_prompt(
            messages=_sample_messages(), content="1", reasoning_content=None,
            finish_reason="stop", model="m", prompt_tokens=1, completion_tokens=1,
        )

    assert len(calls) == 1
    assert calls[0]["max_days"] == 2
    assert (tmp_path / "state" / "action_index" / ".last_hook_run").exists()

    # Immediate second call in burst should be rate-limited and skip build_action_index
    with call_context("cycle-1", "coordinator"):
        record_llm_prompt(
            messages=_sample_messages(), content="2", reasoning_content=None,
            finish_reason="stop", model="m", prompt_tokens=1, completion_tokens=1,
        )

    assert len(calls) == 1  # Rate-limited!


def test_telemetry_hook_failure_still_writes_prompt(tmp_path, monkeypatch):
    """Failure in action index hook must not prevent recording prompt file."""
    from nanobot.observability.llm_telemetry import call_context, record_llm_prompt

    monkeypatch.setenv("LLM_CALLS_DIR", str(tmp_path / "state" / "llm_calls"))

    def crashing_build_action_index(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("nanobot.runtime.action_index.build_action_index", crashing_build_action_index)

    with call_context("cycle-fail", "coordinator"):
        record_llm_prompt(
            messages=_sample_messages(), content="saved", reasoning_content=None,
            finish_reason="stop", model="m", prompt_tokens=1, completion_tokens=1,
        )

    prompts = list((tmp_path / "state" / "llm_calls" / "prompts").glob("*.jsonl"))
    assert len(prompts) == 1
    lines = prompts[0].read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["cycle_id"] == "cycle-fail"
