"""Who writes each ``<state_dir>/<segment>`` that the runtime reads (#1219).

``state/research/`` was read by five code paths for twelve days after its
only writer, ``cycle_planning._write_research_feed``, was deleted with the
planner module (#916/#923, recorded in #924). Nothing noticed: a frozen input
reads as healthy to every check, and ``scorecard._FEEDS`` is a hand-listed
tuple of five paths that cannot see a sixth. The failure mode is a reader
whose writer is gone — so this module declares, for every state subpath the
runtime reads, the writer(s) that own it, and ``tests/test_state_path_writers.py``
checks two things:

1. **Every read segment has an entry.** The test scans ``nanobot/`` for the
   greppable read forms (``state_dir / "<segment>"``, ``state_root /
   '<segment>'``, ``STATE_DIR / '<segment>'``) and fails naming the readers of
   any segment missing here. The scan is a floor — three of ``_FEEDS``'s five paths reach state
   through other forms — so this registry may list more than the scan finds.
2. **Every declared writer still exists.** This is the load-bearing half:
   ``"nanobot.runtime.cycle_planning:_write_research_feed"`` stops resolving
   the moment that module is deleted, which is exactly #924's case, and it
   does not depend on finding every reader. A ref is resolved to a real module
   attribute; a non-empty string is not enough.

Reference forms (each verified by the test):

- ``"<module>:<attribute>"`` — importable Python writer.
- ``"repo:<path>"`` — a file in THIS repository that is the writer or the
  operator procedure (a host libexec script, a runbook for an operator-written
  file). The path must exist.
- ``"orphan:#<issue>"`` — a reader whose writer is gone or unknown, declared
  and tracked. Not an exemption: the test requires the issue to be listed in
  :data:`ORPHAN_ISSUES` with a one-line reason, and the PR that adds one must
  say so. Resolving the issue means replacing the entry with the real writer
  or removing the reader so the segment leaves the scan.

Granularity is the first path segment, so a directory with one live file and
one orphaned file (``control_plane/``) is declared by its live writer and the
orphaned read is recorded in the issue; see #1222.
"""
from __future__ import annotations

STATE_PATH_WRITERS: dict[str, tuple[str, ...]] = {
    "action_index": (
        "nanobot.runtime.action_index:build_action_index",
        "nanobot.observability.llm_telemetry:record_llm_prompt",
    ),
    # apply.ok is written by the operator (runbook), read by the bridge gate.
    "approvals": ("repo:docs/EEEPC_APPLY_OK_OPERATOR_RUNBOOK.md",),
    # control_plane/ and self_evolution/ lost their autoevolve reader (#1224).
    # evolution reports froze with the coordinator, but dashboard/preflight
    # readers survived: #1312 retires them explicitly, not as healthy emptiness.
    # This orphan names evolution-*.json only; external proof reports stay live.
    "reports": ("orphan:#1312",),
    "curator": (
        "nanobot.runtime.knowledge_curator:_write_decision",
        "nanobot.runtime.knowledge_curator:_stage_promotions",
        "nanobot.runtime.knowledge_curator:_stage_lesson_cards",
        "nanobot.runtime.knowledge_curator:_append_manifest_entries",
        "nanobot.runtime.knowledge_curator:promote_reflector_recommendations_to_v2",
    ),
    "demand": (
        "nanobot.runtime.demand:_write_json",
        "nanobot.runtime.demand:mark_skill_retired",
        "nanobot.runtime.demand:record_escalation",
        "nanobot.runtime.goal_gap_futility:_save",
    ),
    # derived_priorities.json is live (goal_review); goal_text.json is the
    # operator's canon and, since #1222, the only source of the active goal id
    # (goal_review.active_goal_id). cycle_archive.json (frozen 2026-08-21T23:00Z
    # with the deleted planner) has no reader since #1225 retired the #877
    # line-switch trigger; the host file is an inert artifact, not a duty.
    "goals": (
        "nanobot.runtime.goal_review:_write_derived_priorities",
        "repo:host/eeepc/scripts/deploy_release.sh",  # seeds goals/goal_text.json, the operator's canon
    ),
    "heldout": (
        "nanobot.runtime.heldout:_save_results",
        "nanobot.runtime.heldout.microbench:_save_microbench_file",
    ),
    # backlog.json (bridge, per cycle), lifecycle.json, and durable.json —
    # written by append_hypotheses from the strategist's daily run (#1222
    # thought that one came from outside the repo; the docstring lied).
    "hypotheses": (
        "nanobot.runtime.backlog_snapshot:write_backlog_snapshot",
        "nanobot.runtime.hypothesis_backlog:_save_lifecycle",
        "nanobot.runtime.hypothesis_backlog:append_hypotheses",
    ),
    # llm-proposed requests remain live; materialized-cycle evidence is retired.
    "improvements": ("nanobot.runtime.llm_proposer:write_request", "orphan:#1312"),
    "ledger": ("nanobot.runtime.cycle_ledger:append_event",),
    "llm_calls": (
        "nanobot.observability.llm_telemetry:record_llm_call",
        "nanobot.observability.llm_telemetry:record_llm_prompt",
    ),
    "promotions": (
        "nanobot.runtime.bridge:_record_runtime_slice_candidate",
        "nanobot.runtime.promotions_rotation:rotate_promotions",
    ),
    "reflector": (
        "nanobot.runtime.reflector:_append_journal",
        "nanobot.runtime.reflector:_save_watermark",
    ),
    "scorecard": ("nanobot.runtime.scorecard:compute_scorecard",),
    # knowledge_curator's nested-layout fallback (``<state_dir>/state/reflector``)
    # — an alias of the reflector journal, not a directory of its own.
    "state": (
        "nanobot.runtime.reflector:_append_journal",
    ),
    "strategist": (
        "nanobot.runtime.strategist:_write_advisories",
        "nanobot.runtime.strategist:save_watermark",
        "nanobot.runtime.strategist:_record_decision",
    ),
    "subagent_bridge": ("nanobot.runtime.bridge:_main_impl_body",),  # handled_<id>.txt markers
    "subagents": (
        "nanobot.runtime.llm_proposer:write_request",
        "nanobot.runtime.bridge:_write_bridge_completed_result",
        "repo:scripts/cleanup_subagent_queue.py",  # results/requests -> archive/
    ),
}

# Every ``orphan:#N`` reference must name an issue listed here, with the
# one-line reason the orphan is being carried rather than fixed in place.
ORPHAN_ISSUES: dict[str, str] = {
    "#1312": "Materialized-cycle and evolution writers are retired; dashboard and preflight retain explicit retired labels, not artifact reads.",
}
