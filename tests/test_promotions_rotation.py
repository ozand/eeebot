"""Tests for nanobot/runtime/promotions_rotation.py (#1039)."""

import gzip
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from nanobot.runtime import bridge
from nanobot.runtime.promotions_rotation import rotate_promotions


def test_promotions_rotation_leaves_today_and_latest(tmp_path: Path):
    promotions_dir = tmp_path / "promotions"
    promotions_dir.mkdir(parents=True, exist_ok=True)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_file = promotions_dir / f"cand-{today}-01.json"
    today_file.write_text(json.dumps({"id": "cand-today-01"}), encoding="utf-8")

    latest_file = promotions_dir / "latest.json"
    latest_file.write_text(json.dumps({"id": "latest"}), encoding="utf-8")

    # Rotate
    rotate_promotions(promotions_dir)

    assert today_file.exists()
    assert latest_file.exists()
    assert not (promotions_dir / f"cand-{today}-01.json.gz").exists()


def test_promotions_rotation_gzips_prior_day_json(tmp_path: Path):
    promotions_dir = tmp_path / "promotions"
    promotions_dir.mkdir(parents=True, exist_ok=True)

    old_date = "2026-06-01"
    old_file = promotions_dir / f"cand-{old_date}-01.json"
    old_file.write_text(json.dumps({"id": "cand-old-01", "desc": "old promotion"}), encoding="utf-8")

    # Set mtime to past date
    past_ts = (datetime.now(timezone.utc) - timedelta(days=10)).timestamp()
    os.utime(old_file, (past_ts, past_ts))

    rotate_promotions(promotions_dir)

    assert not old_file.exists()
    archive_dir = promotions_dir / "archive"
    assert archive_dir.exists()
    gz_files = list(archive_dir.glob("*.jsonl.gz"))
    assert len(gz_files) == 1

    with gzip.open(gz_files[0], "rt", encoding="utf-8") as f:
        line = f.readline().strip()
        data = json.loads(line)
    assert data["id"] == "cand-old-01"


def test_promotions_rotation_prunes_older_than_retention(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("EEEBOT_PROMOTIONS_RETENTION_DAYS", "90")
    promotions_dir = tmp_path / "promotions"
    promotions_dir.mkdir(parents=True, exist_ok=True)
    archive_dir = promotions_dir / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)

    # 100 days old archive (past retention)
    very_old_date = (datetime.now(timezone.utc) - timedelta(days=100)).strftime("%Y-%m-%d")
    very_old_gz = archive_dir / f"promotions-{very_old_date}.jsonl.gz"
    with gzip.open(very_old_gz, "wt", encoding="utf-8") as f:
        f.write(json.dumps({"id": "cand-very-old"}) + "\n")
    past_ts = (datetime.now(timezone.utc) - timedelta(days=100)).timestamp()
    os.utime(very_old_gz, (past_ts, past_ts))

    # 30 days old gz (within retention)
    recent_date = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
    recent_gz = archive_dir / f"promotions-{recent_date}.jsonl.gz"
    with gzip.open(recent_gz, "wt", encoding="utf-8") as f:
        f.write(json.dumps({"id": "cand-recent"}) + "\n")
    recent_ts = (datetime.now(timezone.utc) - timedelta(days=30)).timestamp()
    os.utime(recent_gz, (recent_ts, recent_ts))

    rotate_promotions(promotions_dir)

    assert not very_old_gz.exists()
    assert recent_gz.exists()


def test_bridge_record_runtime_slice_candidate_writes_latest_and_rotates(tmp_path: Path):
    cand_id = bridge._record_runtime_slice_candidate(
        state_dir=tmp_path,
        repo_root=tmp_path,
        cycle_id="cycle-test-123",
        cycle_branch="cycle/cycle-test-123",
        base_sha="deadbeef",
        changed_files=["nanobot/runtime/promotions_rotation.py"],
    )
    assert cand_id.startswith("promotion-runtime-")
    cand_file = tmp_path / "promotions" / f"{cand_id}.json"
    latest_file = tmp_path / "promotions" / "latest.json"

    assert cand_file.exists()
    assert latest_file.exists()

    cand_data = json.loads(cand_file.read_text(encoding="utf-8"))
    latest_data = json.loads(latest_file.read_text(encoding="utf-8"))

    assert cand_data["origin_cycle_id"] == "cycle-test-123"
    assert latest_data["origin_cycle_id"] == "cycle-test-123"
    assert cand_data["rollback_record"]["cycle_branch"] == "cycle/cycle-test-123"
    assert cand_data["rollback_record"]["base_sha"] == "deadbeef"
    assert latest_data == cand_data


def test_promotions_rotation_lock_mechanism(tmp_path: Path, monkeypatch):
    """Test that promotions rotation acquires and releases lock file properly."""
    from nanobot.runtime.promotions_rotation import _acquire_promotions_lock

    promotions_dir = tmp_path / "promotions"
    promotions_dir.mkdir(parents=True)

    lock = _acquire_promotions_lock(promotions_dir)
    assert lock is not None
    lock.close()
