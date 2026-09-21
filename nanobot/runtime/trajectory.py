"""Trajectory-level verification (#1825, ADR-011 rule 3).

The system verifies actions (gate, pytest, CI) and, partially, state
transitions (the scorecard, the artifact graph). Nothing reads a
WINDOW of cycles and asks what they have in common. On 2026-09-19,
76% of 37 proposals were the same shape ("add one function to an
existing script, plus a test") and every one of those cycles was
individually correct -- nothing in the system noticed. This module is
that missing third level: two reports over a rolling window of ledger
rows, reporting concentration, never judging a cycle.

## Report only -- ADR-011 rule 3, enforced structurally

A concentration figure wired into task selection is a target the loop
can satisfy by varying SHAPE without varying VALUE -- the exact
failure this record exists to prevent. Neither report function here
is imported by ``nanobot.runtime.demand`` (demand collection),
``nanobot.runtime.llm_proposer`` (the proposer),
``nanobot.runtime.demand_ranking`` (queue ranking), ``scorecard.py``,
or ``nanobot.agent.context`` (prompt assembly) -- see
``tests/test_trajectory_report_never_gates.py``, which greps each of
those files' source for this module's own name and fails if any of
them import it. The only caller is ``nanobot.runtime.bridge``'s
post-cycle report-writing step (the same call site
``skill_fitness``/``lesson_v2``/``diary_fitness`` already write from),
which publishes to ``state/demand/trajectory_report.json`` for the
dashboard, per ADR-023: the artifact graph this module reads is
already itself dashboard/demand-ranking-input-only, never shown to the
loop as fact (see ``artifact_graph``'s own module docstring) -- this
report inherits the same restriction.

## Windows are row counts, not calendar days

Both windows below are "the last N ledger rows of a given phase",
never a calendar-day bucket. ADR-029 / #1831 is migrating every
DAY-boundary determination in this codebase from UTC to host-local;
a row-count window has no day boundary to get wrong, so this module
carries no clock-choice question at all (unlike, say,
``nanobot.runtime.diary_fitness._today``, which does and says so).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nanobot.runtime.task_shape import TASK_SHAPES, classify_task_shape

SCHEMA_VERSION = "trajectory-report-v1"
REPORT_REL = "demand/trajectory_report.json"

#: Rolling-window sizes. #1825's own hand measurement used a day's worth of
#: proposals (37) for the shape figure and the last 20 successful cycles for
#: the in-degree figure -- these defaults are close to both without being
#: tied to either, since the window is a row count, not "a day" (see the
#: module docstring).
_DEFAULT_SHAPE_WINDOW = 40
_DEFAULT_TARGET_WINDOW = 20
#: Below this many rows, a share is not reported as a finding -- AC: "The
#: report states its own window and row count, so a thin window cannot read
#: as a confident finding."
_MIN_ROWS_FOR_A_FINDING = 5

#: In-degree bands, matching #1825's own hand-measurement language exactly
#: (leaves / components with 1-2 consumers / the most-used artifacts).
_BAND_LEAF = "in_degree_0"
_BAND_LOW = "in_degree_1_2"
_BAND_HIGH = "in_degree_3_plus"
IN_DEGREE_BANDS = (_BAND_LEAF, _BAND_LOW, _BAND_HIGH)


def _in_degree_band(in_degree: int) -> str:
    if in_degree <= 0:
        return _BAND_LEAF
    if in_degree <= 2:
        return _BAND_LOW
    return _BAND_HIGH


def _is_test_path(path: str) -> bool:
    """Mirrors ``nanobot.runtime.demand._is_test_path`` exactly (a
    one-line check not worth importing a private name across modules
    for): a changed path under ``tests/`` is not itself a target."""
    return str(path).replace("\\", "/").strip().lstrip("/").startswith("tests/")


def _target_node_id(path: str) -> "str | None":
    """The artifact-graph node id *path* would resolve to, or ``None`` when
    the path is not one of the three node shapes the graph tracks (a
    ``scripts/*.py``, ``surfaces/*``, or ``skills/*/SKILL.md`` file) --
    see ``nanobot.runtime.artifact_graph._discover_nodes``, whose id
    scheme this mirrors rather than re-derives (both read the same three
    prefixes, and only this module needs the reverse: path -> id)."""
    normalized = str(path).replace("\\", "/").strip().lstrip("/")
    if normalized.startswith("scripts/") and normalized.endswith(".py"):
        return f"scripts/{normalized[len('scripts/'):-len('.py')]}"
    if normalized.startswith("surfaces/") and "/" not in normalized[len("surfaces/"):]:
        return f"surfaces/{normalized[len('surfaces/'):]}"
    if normalized.startswith("skills/") and normalized.endswith("/SKILL.md"):
        name = normalized[len("skills/"):-len("/SKILL.md")]
        if "/" not in name:
            return f"skills/{name}"
    return None


def shape_concentration(
    state_dir: Path, *, repo: "Path | None" = None, window: int = _DEFAULT_SHAPE_WINDOW,
) -> dict[str, Any]:
    """The dominant task shape over the last *window* ``'proposed'`` rows,
    its share, and the direction of travel versus the immediately
    preceding window of the same size.

    ``ok: False`` (with ``reason: "insufficient_data"``) when fewer than
    :data:`_MIN_ROWS_FOR_A_FINDING` proposed rows exist at all -- a thin
    window must never read as a confident finding.
    """
    from nanobot.runtime.cycle_ledger import read_events_across_rotation

    rows = read_events_across_rotation(state_dir, phases={"proposed"})
    if len(rows) < _MIN_ROWS_FOR_A_FINDING:
        return {
            "ok": False, "reason": "insufficient_data", "window": window,
            "row_count": len(rows), "dominant_shape": None, "share": None, "trend": None,
        }

    current = rows[-window:]
    prior = rows[-2 * window:-window] if len(rows) > window else []

    # PR #1834 review: a 'proposed' row does not itself carry the cycle's
    # base sha (it is written before the cycle even starts) -- only the
    # LATER 'outcome' row does, as `main_sha_before`. Without it,
    # classify_task_shape cannot tell "extend existing" from "new leaf"
    # (both exist by the time anyone looks), which is exactly the defect
    # measured live: 3 of 13 scripts/*.py targets in a real sample were
    # created by the very cycle that targeted them, and all three were
    # called extend_existing_script. Joined here, once, by cycle_id --
    # never re-derived per row.
    base_sha_by_cycle: dict[str, str] = {}
    if repo is not None:
        needed = {str(r.get("cycle_id")) for r in current + prior if r.get("cycle_id")}
        for outcome_row in read_events_across_rotation(state_dir, phases={"outcome"}):
            cid = str(outcome_row.get("cycle_id") or "")
            sha = outcome_row.get("main_sha_before")
            if cid in needed and sha:
                base_sha_by_cycle[cid] = str(sha)

    def _shapes(batch: list[dict]) -> dict[str, int]:
        counts: dict[str, int] = {shape: 0 for shape in TASK_SHAPES}
        for row in batch:
            shape = classify_task_shape(
                task_title=str(row.get("task_title") or ""),
                target_path=str(row.get("target_path") or ""),
                repo=repo,
                base_sha=base_sha_by_cycle.get(str(row.get("cycle_id") or "")),
            )
            counts[shape] = counts.get(shape, 0) + 1
        return counts

    # `current` is never empty here: `rows` already passed the
    # `_MIN_ROWS_FOR_A_FINDING` check above and `window` is always >= 1.
    current_counts = _shapes(current)
    # Deterministic tie-break: TASK_SHAPES' own declared order, not dict
    # iteration order (which happens to match here, but stated as an
    # explicit key rather than relied upon).
    dominant_shape = max(TASK_SHAPES, key=lambda s: (current_counts[s], -TASK_SHAPES.index(s)))
    current_share = current_counts[dominant_shape] / len(current)

    trend: "dict[str, Any] | None" = None
    if len(prior) >= _MIN_ROWS_FOR_A_FINDING:
        prior_counts = _shapes(prior)
        prior_share = prior_counts[dominant_shape] / len(prior)
        delta = current_share - prior_share
        direction = "flat"
        if delta > 0.01:
            direction = "up"
        elif delta < -0.01:
            direction = "down"
        trend = {"prior_share": prior_share, "delta": delta, "direction": direction, "prior_row_count": len(prior)}

    return {
        "ok": True,
        "window": window,
        "row_count": len(current),
        "dominant_shape": dominant_shape,
        "share": current_share,
        "counts": current_counts,
        "trend": trend,
    }


def target_concentration(
    state_dir: Path, *, window: int = _DEFAULT_TARGET_WINDOW,
) -> dict[str, Any]:
    """In-degree band counts for the last *window* SUCCESSFUL cycles'
    code-bearing changes -- reproduces the shape of #1825's own hand
    measurement (20 successful cycles, 13 code-bearing, 9 landed on a
    leaf, 4 on a 1-2-consumer component, 0 on the most-used artifacts).

    Reads the already-published graph (``nanobot.runtime.scorecard``'s
    own ``_artifact_graph_section`` publishes it every cycle) rather than
    rebuilding it -- ``build_artifact_graph`` walks the whole instance
    repository, and a second reader rebuilding it on every call would be
    a needless duplicate of that walk.

    ``ok: False`` (``reason: "artifact_graph_unavailable"``) when the
    published artifact graph itself is unavailable -- band counts are
    never reported against a graph that could not be built, which would
    misreport "no data" as "every cycle targeted a leaf".
    """
    from nanobot.runtime.artifact_graph import read_latest_artifact_graph
    from nanobot.runtime.cycle_ledger import read_events_across_rotation
    from nanobot.runtime.demand import classify_change_tier

    graph = read_latest_artifact_graph(state_dir)
    if graph.status != "complete":
        return {
            "ok": False, "reason": "artifact_graph_unavailable", "window": window,
            "successful_in_window": 0, "code_bearing_in_window": 0, "bands": None,
        }

    rows = read_events_across_rotation(state_dir, phases={"outcome"})
    successful = [r for r in rows if r.get("outcome") == "success"]
    successful_window = successful[-window:]

    bands: dict[str, int] = {band: 0 for band in IN_DEGREE_BANDS}
    code_bearing = 0
    unresolved_targets = 0
    ambiguous_targets = 0
    for row in successful_window:
        files_changed = row.get("files_changed")
        if not isinstance(files_changed, list) or not files_changed:
            continue
        if classify_change_tier(files_changed) != "code-bearing":
            continue
        code_bearing += 1
        primary_candidates = sorted(str(f) for f in files_changed if not _is_test_path(str(f)))
        if not primary_candidates:
            unresolved_targets += 1  # test-only code-bearing change: no target artifact
            continue
        if len(primary_candidates) > 1:
            ambiguous_targets += 1  # counted, still classified on the first (deterministic)
        node_id = _target_node_id(primary_candidates[0])
        if node_id is None or node_id not in graph.nodes:
            unresolved_targets += 1
            continue
        band = _in_degree_band(graph.in_degree(node_id))
        bands[band] += 1

    return {
        "ok": True,
        "window": window,
        "successful_in_window": len(successful_window),
        "code_bearing_in_window": code_bearing,
        "bands": bands,
        "unresolved_targets": unresolved_targets,
        "ambiguous_targets": ambiguous_targets,
    }


def write_trajectory_report(
    state_dir: Path, repo: Path, *, now: "datetime | None" = None,
) -> dict[str, Any]:
    """Write both reports to ``state/demand/trajectory_report.json``.

    Report-only, harness-side, written every cycle regardless of that
    cycle's own outcome -- same fail-open, every-cycle contract as the
    skill/lesson/diary censuses already written from the same call site
    (``nanobot.runtime.bridge._write_post_cycle_censuses``).
    """
    payload = {
        "schema": SCHEMA_VERSION,
        "written_at": (now or datetime.now(timezone.utc)).isoformat().replace("+00:00", "Z"),
        "shape_concentration": shape_concentration(state_dir, repo=repo),
        "target_concentration": target_concentration(state_dir),
    }
    path = Path(state_dir) / REPORT_REL
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(path)
    except Exception:
        return {"ok": False, "written": False, "path": str(path)}
    return {"ok": True, "written": True, "path": str(path)}
