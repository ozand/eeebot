"""Population archive — retain all cycle variants for escape from local optima.

Inspired by Darwin Mode Archive ADR-073 (ruvnet/agent-harness-generator):
  'Non-promoted variants are RETAINED, not deleted. Selection samples the
   WHOLE archive — including older, non-promoted branches — which is how
   evolution escapes hill-climbing.'

The archive is persisted as state/goals/cycle_archive.json.
Max 200 entries (oldest pruned beyond limit).
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


MAX_ARCHIVE_ENTRIES = 200
STALL_WINDOW = 5          # check last N cycles for stall detection
STALL_THRESHOLD = 0.8     # all cycles below this → stalled


@dataclass
class ArchiveEntry:
    """Immutable record of one coordinator cycle in the archive."""
    cycle_id: str
    reward: float
    fd_mode: str
    task_id: str
    commits_pushed: int
    parent_id: str | None
    timestamp: float  # Unix timestamp (UTC)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ArchiveEntry":
        return cls(
            cycle_id=str(d.get("cycle_id") or ""),
            reward=float(d.get("reward") or 0.0),
            fd_mode=str(d.get("fd_mode") or ""),
            task_id=str(d.get("task_id") or ""),
            commits_pushed=int(d.get("commits_pushed") or 0),
            parent_id=d.get("parent_id"),
            timestamp=float(d.get("timestamp") or 0.0),
        )


class CycleArchive:
    """Population archive of all coordinator cycle results.

    Entries are stored newest-first internally.  add() is idempotent
    (re-adding the same cycle_id is a no-op).  Max entries enforced on add().
    """

    def __init__(self) -> None:
        self._entries: list[ArchiveEntry] = []  # newest-first

    # ── Mutation ──────────────────────────────────────────────────────────────

    def add(
        self,
        cycle_id: str,
        reward: float,
        fd_mode: str,
        task_id: str,
        commits_pushed: int = 0,
        parent_id: str | None = None,
        timestamp: float | None = None,
    ) -> None:
        """Add a cycle to the archive.  Idempotent — re-adding same cycle_id is a no-op."""
        if any(e.cycle_id == cycle_id for e in self._entries):
            return
        entry = ArchiveEntry(
            cycle_id=cycle_id,
            reward=reward,
            fd_mode=fd_mode,
            task_id=task_id,
            commits_pushed=commits_pushed,
            parent_id=parent_id,
            timestamp=timestamp if timestamp is not None else time.time(),
        )
        self._entries.insert(0, entry)  # prepend → newest-first
        # Prune oldest beyond limit
        if len(self._entries) > MAX_ARCHIVE_ENTRIES:
            self._entries = self._entries[:MAX_ARCHIVE_ENTRIES]

    # ── Queries ───────────────────────────────────────────────────────────────

    def all(self) -> list[ArchiveEntry]:
        """All entries, newest-first."""
        return list(self._entries)

    def best(self, n: int = 1) -> list[ArchiveEntry]:
        """Top-n entries by reward, descending."""
        return sorted(self._entries, key=lambda e: e.reward, reverse=True)[:n]

    def select_diverse(self, n: int = 3) -> list[ArchiveEntry]:
        """Mix of top performers and under-explored entries for escape from local optima.

        Strategy: half from best(), half from oldest non-top entries.
        Inspired by Darwin Mode Archive.selectElites(): samples whole archive on stall.
        """
        if not self._entries:
            return []
        top_n = max(1, n // 2)
        top = self.best(top_n)
        top_ids = {e.cycle_id for e in top}
        rest = [e for e in reversed(self._entries) if e.cycle_id not in top_ids]
        diverse = (top + rest[: n - len(top)])[:n]
        return diverse

    def stalled(self, window: int = STALL_WINDOW, threshold: float = STALL_THRESHOLD) -> bool:
        """True if the last `window` cycles all have reward < `threshold`.

        Signals that the coordinator is hill-climbing and needs diverse exploration.
        """
        recent = self._entries[:window]
        if len(recent) < window:
            return False  # not enough history yet
        return all(e.reward < threshold for e in recent)

    # ── Persistence ───────────────────────────────────────────────────────────

    def save(self, path: Path) -> None:
        """Persist archive to JSON (newest-first list)."""
        path.parent.mkdir(parents=True, exist_ok=True)
        data = [e.as_dict() for e in self._entries]
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def load(self, path: Path) -> None:
        """Load archive from JSON.  Tolerates missing / corrupt file (starts empty)."""
        self._entries = []
        if not path.exists():
            return
        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
            if not isinstance(data, list):
                return
            for item in data:
                if isinstance(item, dict):
                    try:
                        self._entries.append(ArchiveEntry.from_dict(item))
                    except Exception:
                        continue  # skip malformed entries
        except Exception:
            pass  # corrupt file → start empty

    def __len__(self) -> int:
        return len(self._entries)
