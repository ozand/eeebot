"""Tests for nanobot.observability.llm_telemetry (issues #675 and #2034)."""

# ADR-040
import json
from datetime import datetime, timedelta, timezone

import pytest

from nanobot.observability.llm_telemetry import (
    _rotate_and_prune,
    call_context,
    record_llm_call,
    record_llm_failure,
    record_llm_prompt,
    reset_call_context,
    set_call_context,
    telemetry_context,
    write_authoritative_attempt,
)


def _read_jsonl(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def test_failure_stream_is_outside_daily_rotation_globs(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_CALLS_DIR", str(tmp_path))
    failure_dir = tmp_path / "failures"
    failure_dir.mkdir()
    failure_file = failure_dir / "2020-01-01.jsonl"
    failure_file.write_text('{"error_type":"RateLimitError"}\n', encoding="utf-8")
    before = failure_file.read_bytes()

    from nanobot.observability.llm_telemetry import _rotate_and_prune
    _rotate_and_prune(tmp_path, today="2026-09-29", retention_days=1)
    # The call and prompt retention routines only inspect direct-child daily
    # files; failure stream has its own retention and is not treated as a day.
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "2020-01-01.jsonl").write_text("{}\n", encoding="utf-8")
    _rotate_and_prune(prompts, today="2026-09-29", retention_days=1)

    assert failure_file.exists()
    assert failure_file.read_bytes() == before
    assert not list(failure_dir.glob("*.jsonl.gz"))


def test_executor_prompt_capture_prunes_resolutions_once_after_append(tmp_path, monkeypatch):
    import nanobot.observability.llm_telemetry as telemetry

    root = tmp_path / "calls"
    prompts = root / "prompts"
    prompts.mkdir(parents=True)
    real_prune = telemetry._prune_attempt_resolutions
    observed = []

    def count_prune(prompts_dir):
        observed.append(list(prompts_dir.glob("*.jsonl")))
        return real_prune(prompts_dir)

    monkeypatch.setattr(telemetry, "_prune_attempt_resolutions", count_prune)
    with telemetry_context("cycle-prune-once", "executor", attempt_id="attempt-prune-once", root=root):
        record_llm_prompt(
            messages=[], content=None, reasoning_content=None, finish_reason="stop",
            model="test", prompt_tokens=0, completion_tokens=0,
        )

    assert len(observed) == 1
    assert len(observed[0]) == 1
    assert _read_jsonl(observed[0][0])[0]["attempt_id"] == "attempt-prune-once"


def test_authoritative_attempt_sidecar_is_pruned_with_prompt(tmp_path, monkeypatch):
    root = tmp_path / "calls"
    prompts = root / "prompts"
    prompts.mkdir(parents=True)
    day = datetime.now(timezone.utc).date().isoformat()
    with telemetry_context("cycle-retain", "executor", attempt_id="spawn-retain", root=root):
        record_llm_prompt(
            messages=[], content=None, reasoning_content=None, finish_reason="stop",
            model="test", prompt_tokens=0, completion_tokens=0,
        )
    assert write_authoritative_attempt(root, "cycle-retain", "spawn-retain")
    sidecar = next((prompts / "attempt_resolutions").glob("*.json"))
    expected_expiry = (datetime.fromisoformat(day).date() + timedelta(days=14)).isoformat()
    assert json.loads(sidecar.read_text(encoding="utf-8"))["expires_at"].startswith(expected_expiry)

    (prompts / f"{day}.jsonl").unlink()
    _rotate_and_prune(prompts, today=day, retention_days=1)

    assert not sidecar.exists()


def test_authoritative_attempt_writer_fails_closed_on_active_corrupt_archive(tmp_path):
    root = tmp_path / "calls"
    from nanobot.observability.llm_telemetry import telemetry_context

    with telemetry_context("valid-cycle", "executor", attempt_id="valid-attempt", root=root):
        record_llm_prompt(
            messages=[], content=None, reasoning_content=None, finish_reason="stop",
            model="test", prompt_tokens=0, completion_tokens=0,
        )
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    (root / "prompts" / f"{day}.jsonl.gz").write_bytes(b"not a gzip stream")

    assert not write_authoritative_attempt(root, "valid-cycle", "valid-attempt")


def test_resolution_reader_preserves_valid_sidecar_on_temporary_sidecar_read_error(tmp_path, monkeypatch):
    from pathlib import Path

    import nanobot.observability.llm_telemetry as telemetry

    root = tmp_path / "calls"
    with telemetry.telemetry_context("sidecar-read-error", "executor", attempt_id="attempt-sidecar-error", root=root):
        record_llm_prompt(
            messages=[], content=None, reasoning_content=None, finish_reason="stop",
            model="test", prompt_tokens=0, completion_tokens=0,
        )
    assert telemetry.write_authoritative_attempt(root, "sidecar-read-error", "attempt-sidecar-error")
    sidecar = next((root / "prompts/attempt_resolutions").glob("*.json"))
    real_read_text = Path.read_text

    def fail_sidecar_read(path, *args, **kwargs):
        if path == sidecar:
            raise PermissionError("temporary sidecar read failure")
        return real_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_sidecar_read)
    invalid = []
    assert telemetry.read_authoritative_attempt(root, "sidecar-read-error", invalid=invalid) is None
    assert invalid == []
    assert sidecar.exists()


def test_resolution_reader_preserves_metadata_on_plain_prompt_permission_error(tmp_path, monkeypatch):
    from pathlib import Path

    import nanobot.observability.llm_telemetry as telemetry

    root = tmp_path / "calls"
    with telemetry.telemetry_context("permission-read", "executor", attempt_id="attempt-permission", root=root):
        record_llm_prompt(
            messages=[], content=None, reasoning_content=None, finish_reason="stop",
            model="test", prompt_tokens=0, completion_tokens=0,
        )
    assert telemetry.write_authoritative_attempt(root, "permission-read", "attempt-permission")
    sidecar = next((root / "prompts/attempt_resolutions").glob("*.json"))
    prompt = next((root / "prompts").glob("*.jsonl"))
    import nanobot.observability.llm_telemetry as telemetry_module
    real_open = telemetry_module._open_prompt_file

    def deny_plain_prompt(root_arg, name):
        if name == prompt.name:
            raise PermissionError("temporarily unavailable")
        return real_open(root_arg, name)

    monkeypatch.setattr(telemetry_module, "_open_prompt_file", deny_plain_prompt)
    invalid = []
    assert telemetry.read_authoritative_attempt(root, "permission-read", invalid=invalid) is None
    assert "prompt_missing" not in invalid
    assert sidecar.exists()


def test_resolution_reader_does_not_open_symlinked_prompt_before_rejection(tmp_path, monkeypatch):
    from pathlib import Path

    import nanobot.observability.llm_telemetry as telemetry

    root = tmp_path / "calls"
    with telemetry.telemetry_context("symlink-prompt", "executor", attempt_id="attempt-symlink", root=root):
        record_llm_prompt(
            messages=[], content=None, reasoning_content=None, finish_reason="stop",
            model="test", prompt_tokens=0, completion_tokens=0,
        )
    assert telemetry.write_authoritative_attempt(root, "symlink-prompt", "attempt-symlink")
    prompt = next((root / "prompts").glob("*.jsonl"))
    external = tmp_path / "external.jsonl"
    external.write_text(prompt.read_text(encoding="utf-8"), encoding="utf-8")
    prompt.unlink()
    try:
        prompt.symlink_to(external)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    real_open = Path.open

    def reject_symlink_open(path, *args, **kwargs):
        if path == prompt:
            raise AssertionError("symlinked path was opened before rejection")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", reject_symlink_open)
    assert telemetry.read_authoritative_attempt(root, "symlink-prompt") is None


def test_resolution_reader_rejects_prompt_directory_symlink(tmp_path):
    import os

    import nanobot.observability.llm_telemetry as telemetry

    root = tmp_path / "calls"
    outside = tmp_path / "outside"
    outside.mkdir()
    with telemetry.telemetry_context("parent-symlink", "executor", attempt_id="attempt-parent", root=root):
        record_llm_prompt(
            messages=[], content=None, reasoning_content=None, finish_reason="stop",
            model="test", prompt_tokens=0, completion_tokens=0,
        )
    assert telemetry.write_authoritative_attempt(root, "parent-symlink", "attempt-parent")
    prompts = root / "prompts"
    for file in prompts.glob("*.jsonl"):
        file.replace(outside / file.name)
    import shutil
    shutil.rmtree(prompts)
    try:
        os.symlink(outside, prompts, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlink creation unavailable")

    assert telemetry.read_authoritative_attempt(root, "parent-symlink") is None


@pytest.mark.skipif(__import__("os").name != "posix", reason="FIFO open safety is POSIX-specific")
def test_resolution_reader_does_not_block_on_fifo_prompt(tmp_path):
    import os
    import subprocess
    import sys

    import nanobot.observability.llm_telemetry as telemetry

    root = tmp_path / "calls"
    with telemetry.telemetry_context("fifo-prompt", "executor", attempt_id="attempt-fifo", root=root):
        record_llm_prompt(
            messages=[], content=None, reasoning_content=None, finish_reason="stop",
            model="test", prompt_tokens=0, completion_tokens=0,
        )
    assert telemetry.write_authoritative_attempt(root, "fifo-prompt", "attempt-fifo")
    prompt = next((root / "prompts").glob("*.jsonl"))
    prompt.unlink()
    os.mkfifo(prompt)

    code = (
        "from pathlib import Path; "
        "from nanobot.observability.llm_telemetry import read_authoritative_attempt; "
        f"assert read_authoritative_attempt(Path({str(root)!r}), 'fifo-prompt') is None"
    )
    subprocess.run([sys.executable, "-c", code], check=True, timeout=3)


def test_resolution_reader_rejects_prompt_directory_replaced_with_symlink(tmp_path, monkeypatch):
    import contextlib
    import os

    import nanobot.observability.llm_telemetry as telemetry

    root = tmp_path / "calls"
    outside = tmp_path / "outside"
    outside.mkdir()
    with telemetry.telemetry_context("root-swap", "executor", attempt_id="attempt-root-swap", root=root):
        record_llm_prompt(
            messages=[], content=None, reasoning_content=None, finish_reason="stop",
            model="test", prompt_tokens=0, completion_tokens=0,
        )
    assert telemetry.write_authoritative_attempt(root, "root-swap", "attempt-root-swap")
    prompts = root / "prompts"
    sidecar = next((prompts / "attempt_resolutions").glob("*.json"))
    sidecar_bytes = sidecar.read_bytes()
    moved = root / "prompts-original"
    swapped = []

    def swap_prompt_path():
        prompts.rename(moved)
        os.symlink(outside, prompts, target_is_directory=True)
        swapped.append(True)

    if os.name == "posix":
        prompt_name = next(prompts.glob("*.jsonl")).name
        real_os_open = os.open

        def swap_after_prompt_directory_handle(path, flags, *args, **kwargs):
            if path == prompt_name and kwargs.get("dir_fd") is not None and not swapped:
                # The production helper already owns the real prompts
                # directory descriptor; replace only its pathname before the
                # final component is opened relative to that descriptor.
                swap_prompt_path()
            return real_os_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(telemetry.os, "open", swap_after_prompt_directory_handle)
        record = telemetry.read_authoritative_attempt(root, "root-swap")
        assert swapped == [True]
        assert record is None
        assert not list(outside.iterdir()), "handle-relative open must not read the replacement target"
        moved_sidecar = moved / sidecar.relative_to(prompts)
        assert moved_sidecar.exists(), "original resolution metadata must remain in the renamed directory"
        assert moved_sidecar.read_bytes() == sidecar_bytes
    else:
        real_open = telemetry._open_prompt_file_windows

        def swap_before_windows_open(root_arg, name):
            if not swapped:
                swap_prompt_path()
            yield from real_open(root_arg, name)

        monkeypatch.setattr(telemetry, "_open_prompt_file_windows", swap_before_windows_open)
        assert telemetry.read_authoritative_attempt(root, "root-swap") is None
        assert swapped == [True]


def test_resolution_reader_preserves_metadata_across_plain_to_gzip_rotation(tmp_path, monkeypatch):
    import nanobot.observability.llm_telemetry as telemetry

    root = tmp_path / "calls"
    with telemetry.telemetry_context("rotate-race", "executor", attempt_id="attempt-rotate", root=root):
        record_llm_prompt(
            messages=[], content=None, reasoning_content=None, finish_reason="stop",
            model="test", prompt_tokens=0, completion_tokens=0,
        )
    assert telemetry.write_authoritative_attempt(root, "rotate-race", "attempt-rotate")
    prompts = root / "prompts"
    plain = next(prompts.glob("*.jsonl"))
    archive = plain.with_suffix(plain.suffix + ".gz")
    real_decode = telemetry._decode_prompt_rows
    rotated = []

    def rotate_after_open(stream):
        if not rotated and plain.exists():
            import gzip
            with plain.open("rb") as source, gzip.open(archive, "wb") as target:
                target.write(source.read())
            plain.unlink()
            rotated.append(True)
        yield from real_decode(stream)

    monkeypatch.setattr(telemetry, "_decode_prompt_rows", rotate_after_open)
    record = telemetry.read_authoritative_attempt(root, "rotate-race")

    assert record is not None
    assert record["attempt_id"] == "attempt-rotate"
    assert next((prompts / "attempt_resolutions").glob("*.json")).exists()


def test_malformed_sidecar_is_discarded_so_expired_prompt_cleanup_continues(tmp_path):
    import nanobot.observability.llm_telemetry as telemetry

    prompts = tmp_path / "prompts"
    sidecars = prompts / "attempt_resolutions"
    sidecars.mkdir(parents=True)
    expired_prompt = prompts / "2020-01-01.jsonl.gz"
    expired_prompt.write_bytes(b"expired prompt")
    malformed = sidecars / "bad.json"
    malformed.write_text("{bad json", encoding="utf-8")

    telemetry._rotate_and_prune(prompts, today="2026-10-30", retention_days=1)

    assert not expired_prompt.exists()
    assert not malformed.exists()
    assert not list(sidecars.glob("*.invalid"))


def test_resolution_cleanup_io_failure_preserves_expired_prompt(tmp_path, monkeypatch):
    from pathlib import Path

    import nanobot.observability.llm_telemetry as telemetry

    prompts = tmp_path / "prompts"
    prompts.mkdir()
    expired_prompt = prompts / "2020-01-01.jsonl.gz"
    expired_prompt.write_bytes(b"expired prompt")
    sidecars = prompts / "attempt_resolutions"
    sidecars.mkdir()
    (sidecars / "unreadable.json").write_text("{}", encoding="utf-8")
    real_read_text = Path.read_text

    def fail_sidecar_read(path, *args, **kwargs):
        if path.parent.name == "attempt_resolutions":
            raise OSError("sidecar storage unavailable")
        return real_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_sidecar_read)
    telemetry._rotate_and_prune(prompts, today="2026-10-30", retention_days=1)

    assert expired_prompt.exists()


# ADR-040: prompt retention does not proceed when associated sidecar cleanup fails.
def test_attempt_resolution_cleanup_failure_preserves_prompt(tmp_path, monkeypatch):
    import nanobot.observability.llm_telemetry as telemetry

    prompts = tmp_path / "prompts"
    prompts.mkdir()
    old_prompt = prompts / "2020-01-01.jsonl.gz"
    old_prompt.write_bytes(b"retained until resolution cleanup succeeds")
    monkeypatch.setattr(telemetry, "_delete_resolutions_for_day", lambda *_: False)

    telemetry._rotate_and_prune(prompts, today="2026-10-30", retention_days=1)

    assert old_prompt.exists()


def test_record_llm_failure_writes_only_bounded_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_CALLS_DIR", str(tmp_path))
    with call_context("cycle-fail", "proposer"):
        record_llm_failure(
            component="proposer", requested_model="model-a", error_type="RateLimitError",
            http_status=429, retry_after_s=10,
        )
    file = next((tmp_path / "failures").glob("*.jsonl"))
    row = _read_jsonl(file)[0]
    assert row["ok"] is False
    assert row["cycle_id"] == "cycle-fail"
    assert row["seq"] == 1
    assert "prompt" not in row and "response" not in row


def test_record_llm_failure_invalid_timestamp_cannot_escape_failures_dir(tmp_path, monkeypatch):
    from datetime import datetime, timezone

    monkeypatch.setenv("LLM_CALLS_DIR", str(tmp_path / "llm_calls"))
    record_llm_failure(
        component="proposer", requested_model="model-a", error_type="RateLimitError",
        ts="../../evil-2026-01-01T00:00:00Z",
    )

    failures_root = tmp_path / "llm_calls" / "failures"
    rows = list(failures_root.glob("*.jsonl"))
    assert len(rows) == 1
    assert rows[0].name == datetime.now(timezone.utc).strftime("%Y-%m-%d.jsonl")
    assert not (tmp_path / "evil-2026-01-01T00:00:00Z.jsonl").exists()


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


def test_nested_call_context_restores_outer_duration_sequence(tmp_path, monkeypatch):
    """An inner call context must not erase the outer call's pending seq."""
    monkeypatch.setenv("LLM_CALLS_DIR", str(tmp_path))
    monkeypatch.delenv("LLM_CAPTURE_PROMPTS", raising=False)

    with call_context("cycle-outer", "bridge"):
        record_llm_call(model="m", duration_ms=1.0, usage={}, finish_reason="stop", retries=0)
        with call_context("cycle-inner", "proposer"):
            record_llm_call(model="m", duration_ms=1.0, usage={}, finish_reason="stop", retries=0)
            record_llm_prompt(
                messages=[], content="inner", reasoning_content=None,
                finish_reason="stop", model="m", prompt_tokens=1, completion_tokens=1,
            )
        record_llm_prompt(
            messages=[], content="outer", reasoning_content=None,
            finish_reason="stop", model="m", prompt_tokens=1, completion_tokens=1,
        )

    rows = [json.loads(line) for path in tmp_path.rglob("*.jsonl") for line in path.read_text().splitlines()]
    prompts = [row for row in rows if "messages" in row]
    assert {(row["cycle_id"], row["component"], row["seq"]) for row in prompts} == {
        ("cycle-inner", "proposer", 1), ("cycle-outer", "bridge", 1),
    }


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
