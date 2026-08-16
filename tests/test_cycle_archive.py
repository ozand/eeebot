"""Tests for #529: population archive.

Verifies CycleArchive stores all variants, enforces max entries,
detects stalls, and persists/loads correctly.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from nanobot.runtime.archive import (
    MAX_ARCHIVE_ENTRIES,
    STALL_THRESHOLD,
    STALL_WINDOW,
    ArchiveEntry,
    CycleArchive,
)


def _make_archive(*entries: tuple) -> CycleArchive:
    """Helper: create archive from (cycle_id, reward, fd_mode, task_id) tuples."""
    arch = CycleArchive()
    for i, (cid, reward, mode, task) in enumerate(entries):
        arch.add(cycle_id=cid, reward=reward, fd_mode=mode, task_id=task,
                 timestamp=1000.0 + i)
    return arch


# ─── basic add / all ──────────────────────────────────────────────────────────

def test_add_3_entries_returns_3_newest_first():
    arch = _make_archive(
        ('cycle-1', 0.9, 'keep', 'task-a'),
        ('cycle-2', 0.7, 'retry', 'task-b'),
        ('cycle-3', 1.0, 'keep', 'task-c'),
    )
    entries = arch.all()
    assert len(entries) == 3
    # newest-first (cycle-3 was added last → index 0)
    assert entries[0].cycle_id == 'cycle-3'
    assert entries[-1].cycle_id == 'cycle-1'


def test_add_is_idempotent():
    """Re-adding same cycle_id is a no-op."""
    arch = CycleArchive()
    arch.add('cycle-1', 1.0, 'keep', 'task')
    arch.add('cycle-1', 0.5, 'discard', 'task')  # duplicate
    assert len(arch) == 1
    assert arch.all()[0].reward == 1.0  # original value kept


# ─── best ─────────────────────────────────────────────────────────────────────

def test_best_2_returns_top_2_by_reward():
    arch = _make_archive(
        ('c1', 0.5, 'm', 't'),
        ('c2', 1.2, 'm', 't'),
        ('c3', 0.8, 'm', 't'),
        ('c4', 1.0, 'm', 't'),
    )
    top = arch.best(2)
    assert len(top) == 2
    assert top[0].reward == 1.2
    assert top[1].reward == 1.0


# ─── stalled ─────────────────────────────────────────────────────────────────

def test_stalled_true_when_5_low_reward():
    arch = _make_archive(
        ('c1', 0.5, 'm', 't'),
        ('c2', 0.6, 'm', 't'),
        ('c3', 0.7, 'm', 't'),
        ('c4', 0.4, 'm', 't'),
        ('c5', 0.6, 'm', 't'),
    )
    # All 5 < 0.8 → stalled
    assert arch.stalled() is True


def test_stalled_false_with_mixed_rewards():
    arch = _make_archive(
        ('c1', 0.5, 'm', 't'),
        ('c2', 0.6, 'm', 't'),
        ('c3', 1.1, 'm', 't'),  # one high → not stalled
        ('c4', 0.4, 'm', 't'),
        ('c5', 0.6, 'm', 't'),
    )
    assert arch.stalled() is False


def test_stalled_false_with_insufficient_history():
    arch = _make_archive(
        ('c1', 0.3, 'm', 't'),
        ('c2', 0.3, 'm', 't'),
        # Only 2 entries < STALL_WINDOW (5) → not stalled yet
    )
    assert arch.stalled() is False


# ─── persistence ─────────────────────────────────────────────────────────────

def test_save_load_round_trip(tmp_path):
    arch = _make_archive(
        ('c1', 1.0, 'keep', 'task-a'),
        ('c2', 0.7, 'retry', 'task-b'),
    )
    path = tmp_path / 'cycle_archive.json'
    arch.save(path)

    arch2 = CycleArchive()
    arch2.load(path)
    assert len(arch2) == 2
    entries = arch2.all()
    assert entries[0].cycle_id == 'c2'  # newest first (added after c1)
    assert entries[0].reward == 0.7
    assert entries[1].cycle_id == 'c1'
    assert entries[1].reward == 1.0


def test_load_missing_file_starts_empty(tmp_path):
    arch = CycleArchive()
    arch.load(tmp_path / 'nonexistent.json')
    assert len(arch) == 0


def test_load_corrupt_file_starts_empty(tmp_path):
    path = tmp_path / 'corrupt.json'
    path.write_text('NOT JSON!!!')
    arch = CycleArchive()
    arch.load(path)
    assert len(arch) == 0


# ─── max entries enforcement ─────────────────────────────────────────────────

def test_max_entries_prunes_oldest():
    arch = CycleArchive()
    for i in range(MAX_ARCHIVE_ENTRIES + 5):
        arch.add(f'cycle-{i}', 0.5, 'm', 't', timestamp=float(i))
    assert len(arch) == MAX_ARCHIVE_ENTRIES
    # oldest should be gone
    ids = {e.cycle_id for e in arch.all()}
    assert 'cycle-0' not in ids  # oldest pruned
    assert f'cycle-{MAX_ARCHIVE_ENTRIES + 4}' in ids  # newest kept
