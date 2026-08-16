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

# ── #844: BRIDGE-lane stepping-stone archive ────────────────────────────────
# A small (<=5) diversity archive of gate-passing BRIDGE candidate variants,
# keyed on a behavior signature (the primary changed area), that the LLM
# proposer surfaces as optional stepping-stones — DGM-style "here are other
# validated branches you could extend" rather than a MAP-Elites grid.
# Proposer-steering ONLY: this never touches the gate, fitness, confirm, or
# FITNESS_SIDECARS. Persisted at state/steering/stepping_stones.json,
# separate from CycleArchive (coordinator-lane, state/goals/cycle_archive.json)
# above — same module, different lane, different file.
_STEPPING_STONES_MAX = 5  # #844: <=5 diverse gate-passing variants (not a grid)
_STEPPING_STONE_SUMMARY_MAX = 160


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


# ── #844: stepping-stone functions (BRIDGE lane, proposer-steering only) ────

def _stepping_stone_signature(files_changed: Any) -> str:
    """Behavior signature = the primary changed area (#844). Prefer the first
    scripts/*.py path (the loop's main surface), else the first path, else
    'misc'. Fail-open to 'misc'."""
    try:
        paths = [str(p).strip() for p in (files_changed or []) if str(p).strip()]
        for p in sorted(paths):
            if p.startswith("scripts/") and p.endswith(".py"):
                return p
        return sorted(paths)[0] if paths else "misc"
    except Exception:
        return "misc"


def _stepping_stones_path(state_dir: Any) -> Path:
    return Path(state_dir) / "steering" / "stepping_stones.json"


def record_stepping_stone(
    state_dir: Any,
    cycle_id: str,
    files_changed: Any,
    summary: str,
    *,
    now: float | None = None,
) -> None:
    """Append one gate-passing variant to the bounded stepping-stone archive
    (#844), keyed on behavior signature. Keeps the NEWEST entry per signature,
    at most _STEPPING_STONES_MAX distinct signatures (evict oldest signature) —
    so the archive stays diverse across areas, not 5 of the same lineage.
    Steering-only (state/steering/stepping_stones.json); NOT a fitness sidecar.
    Fail-open: any error is swallowed."""
    try:
        path = _stepping_stones_path(state_dir)
        entries: list[dict[str, Any]] = []
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, list):
                    entries = [e for e in raw if isinstance(e, dict)]
            except Exception:
                entries = []

        sig = _stepping_stone_signature(files_changed)
        ts = now if now is not None else time.time()
        try:
            fc = [str(p) for p in (files_changed or [])][:20]
        except Exception:
            fc = []
        entry = {
            "cycle_id": str(cycle_id or ""),
            "signature": sig,
            "files_changed": fc,
            "summary": str(summary or "")[:_STEPPING_STONE_SUMMARY_MAX],
            "ts": ts,
        }

        # Newest-per-signature wins; also drop any prior entry with the same
        # cycle_id (idempotent re-record of the same cycle).
        entries = [
            e for e in entries
            if e.get("signature") != sig and str(e.get("cycle_id") or "") != entry["cycle_id"]
        ]
        entries.append(entry)
        entries.sort(key=lambda e: float(e.get("ts") or 0.0), reverse=True)
        entries = entries[:_STEPPING_STONES_MAX]

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    except Exception:
        pass  # steering archive is non-blocking (#844)


def read_stepping_stones(state_dir: Any) -> list[dict[str, Any]]:
    """Return the archived stepping-stones (list of dicts), newest-first,
    bounded to _STEPPING_STONES_MAX. Fail-open to []."""
    try:
        path = _stepping_stones_path(state_dir)
        if not path.exists():
            return []
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            return []
        entries = [e for e in raw if isinstance(e, dict)]
        entries.sort(key=lambda e: float(e.get("ts") or 0.0), reverse=True)
        return entries[:_STEPPING_STONES_MAX]
    except Exception:
        return []
