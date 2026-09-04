"""Stepping-stone archive — BRIDGE-lane, proposer-steering only (#844).

A small (<=5) diversity archive of gate-passing BRIDGE candidate variants,
keyed on a behavior signature (the primary changed area), that the LLM
proposer surfaces as optional stepping-stones — DGM-style "here are other
validated branches you could extend" rather than a MAP-Elites grid.
Proposer-steering ONLY: this never touches the gate, fitness, confirm, or
FITNESS_SIDECARS. Persisted at ``state/steering/stepping_stones.json``.

History: this module also held the coordinator-lane ``CycleArchive``
(``state/goals/cycle_archive.json``, reward per cycle, ``stalled()`` = last
5 cycles all reward < 0.8) that was the sole trigger of the #877 line
switch. Retired in #1225: its only writer was the coordinator deleted in
#916/#923, the file froze on 2026-08-21T23:00:58Z with 200 entries all at
reward 1.0, ``tree.json`` recorded 0 switches and no ``evo/node-*`` keeper
branch ever existed — a lever that never worked, not one that stopped.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from nanobot.runtime._io import write_json_atomic

_STEPPING_STONES_MAX = 5  # #844: <=5 diverse gate-passing variants (not a grid)
_STEPPING_STONE_SUMMARY_MAX = 160


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

        write_json_atomic(path, entries)
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
