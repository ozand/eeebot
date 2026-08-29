"""Unit tests for nanobot.runtime._io utilities."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from nanobot.runtime._io import (
    load_json_dict,
    read_json_strict,
    utc_iso_raw,
    utc_now,
    write_json,
    write_json_atomic,
)


def test_write_json_atomic_creates_parent_and_writes_valid_json(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "sub" / "data.json"
    payload = {"status": "ok", "items": [1, 2, 3], "unicode": "тест"}

    write_json_atomic(target, payload)

    assert target.exists()
    assert load_json_dict(target) == payload


def test_write_json_atomic_interrupted_write_preserves_old_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "config.json"
    original_payload = {"version": 1, "state": "initial"}
    write_json_atomic(target, original_payload)

    assert load_json_dict(target) == original_payload

    # Simulate an error during serialization before writing/replacement
    class UnserializableObject:
        pass

    with pytest.raises(TypeError):
        write_json_atomic(target, {"version": 2, "bad": UnserializableObject()})

    # The original file must remain untouched and valid
    assert load_json_dict(target) == original_payload

    # Also simulate failure in os.replace
    def failing_replace(src: Path | str, dst: Path | str) -> None:
        raise OSError("Simulated disk error during replace")

    monkeypatch.setattr(os, "replace", failing_replace)

    with pytest.raises(OSError, match="Simulated disk error"):
        write_json_atomic(target, {"version": 3, "state": "failed_replace"})

    # The original file must remain untouched and valid
    assert load_json_dict(target) == original_payload


def test_write_json_and_load_json_dict(tmp_path: Path) -> None:
    target = tmp_path / "plain.json"
    payload = {"key": "value", "num": 42}
    write_json(target, payload)
    assert load_json_dict(target) == payload


def test_load_json_dict_missing_or_invalid(tmp_path: Path) -> None:
    missing = tmp_path / "nonexistent.json"
    assert load_json_dict(missing) is None

    invalid = tmp_path / "invalid.json"
    invalid.write_text("not json", encoding="utf-8")
    assert load_json_dict(invalid) is None

    not_dict = tmp_path / "list.json"
    not_dict.write_text("[1, 2, 3]", encoding="utf-8")
    assert load_json_dict(not_dict) is None


def test_read_json_strict(tmp_path: Path) -> None:
    valid = tmp_path / "valid.json"
    valid.write_text('{"a": 1}', encoding="utf-8")
    assert read_json_strict(valid) == {"a": 1}

    missing = tmp_path / "missing.json"
    with pytest.raises(Exception):
        read_json_strict(missing)


def test_utc_helpers() -> None:
    now = utc_now()
    assert now.tzinfo is not None
    iso_str = utc_iso_raw(now)
    assert isinstance(iso_str, str)
    assert "T" in iso_str
    assert iso_str.endswith("Z") or "+00:00" not in iso_str


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits only")
def test_write_json_atomic_mode_0644_under_restrictive_umask(tmp_path: Path) -> None:
    """#1096: write_json_atomic must produce a 0644 file even when umask is 0077.

    The dashboard publisher runs as a different user than the agent; mkstemp
    creates 0600 temp files which os.replace preserves, locking out any
    non-agent reader after every atomic rewrite.  This test verifies the fix.
    """
    target = tmp_path / "state.json"
    old_umask = os.umask(0o077)
    try:
        write_json_atomic(target, {"key": "value"})
    finally:
        os.umask(old_umask)
    assert target.exists()
    assert target.stat().st_mode & 0o777 == 0o644, (
        f"Expected 0644, got {oct(target.stat().st_mode & 0o777)}"
    )
