---
title: Bounded state access
status: accepted
date: 2026-09-02
authors: [eeebot maintainers]
related: ["#1173", "#1174"]
tags: [runtime, state, reliability]
---

# Status

Accepted — implemented for the migrations completed under #1173–#1179.

The dead-feature audit and residual reader decisions for #1173 are recorded
below. This ADR does not claim that every historical raw reader has been
migrated; the hygiene allowlist remains a floor, not a compliance certificate.

# Context

Runtime readers independently reimplemented ledger rotation, horizons, ordering, and size caps. Fail-open readers also collapsed unavailable state into empty state, making silent dead paths difficult to diagnose.

# Decision

`nanobot.runtime.state_access` is the single stdlib-only foundation for bounded runtime-state reads. It exposes immutable `Window`, `Latest`, and `Sidecar` results. Readers never raise: `Window.status` is `complete`, `partial`, or `unavailable`; `Window.covered_from` and `Window.covered_to` report the observed timestamp bounds (with `covered_to` making a recent-end cap boundary explicit); `Sidecar.status` is `present`, `absent`, `corrupt`, `oversize`, or `permission`.

Ledger windows read the active ledger and dated gzip archives newest-first, stop at the requested horizon and explicit byte cap, prefilter phases before JSON parsing, and report skipped/corrupt sources in notes. A non-permission I/O failure is reported as `io_error`, not `permission`. Artifact reads unify live and archived flat artifact directories with bounded candidate selection and deterministic mtime/name ordering. Latest-file reads break mtime ties by name. Sidecar reads enforce a caller-provided byte cap.

Migrated callers must state how they handle each non-complete status. The
foundation itself does not choose a caller policy: `unavailable` is a value,
not an exception, and callers must not interpret it as genuine emptiness.
D4 (`demand._result_file_defects`) and D11 (`usage_evidence._touched_from_results`)
now use explicit `results + archive` selection; request readers remain
requests-only. D12 (`state._live_recent_outcomes`) remains a Class-C display
reader over the live ledger tail: its quiet and unreadable cases are still a
known presentation ambiguity, not an autonomous decision input.

The dead-feature audit removed the coordinator-era `CycleArchive` family and
its line-switch trigger: no production caller of `CycleArchive.save` remained,
the only pre-retirement `save` call was test-only, and the bridge's load/stall
block was the sole production consumer. `cycle_archive.json` is now inert in
this repository. The instance-side frozen
dashboard reader is still live and currently renders the frozen list unlabelled
on :8080; its separately governed retirement/label change is tracked by the
still-open `eeebot-self-evolving#185`, so that surface is not complete or
deployed. The `orphan:#1225` writer-registry marker and its `ORPHAN_ISSUES`
reason were removed together.

Evidence strength is intentionally split. The D4/D11 directory roles, the
`CycleArchive` production consumer, and the state-path cleanup are proven by
source and call-site inspection plus focused tests. The following current raw
readers are audit judgement rather than complete call-graph proof:
`action_index`, `backlog_snapshot` (retired in #1356), `state._live_recent_outcomes`,
`loop_explorer`, `reflector`, `strategist` fallback reads, and
`skill_candidate_mining.read_sidecar`. Their classifications came from current
call paths and manual scanners, not direct `state_access.artifacts` call edges;
they remain candidates for per-reader policy review. Cross-repository/operator
consumers require their own verification.

The contract is a policy over these simple primitives, not a second abstraction:
readers use `ledger_window`, `artifacts`, `latest_file`, or `sidecar`; declare
horizon and size bounds; preserve status, notes, and coverage where evidence is
used; and test their caller-specific policy. Class-A consumers count complete
windows, use partial windows only as lower bounds without lowering persisted
counters, and retain the prior verdict on unavailable. Class-B read-modify-write
consumers replace only `absent`/`present` sidecars; corrupt, oversize, permission,
and I/O failures skip the write and report a labelled reason. Retired readers
must say `retired`/`unavailable`, never healthy empty. State paths require a live
writer or an explicit `orphan:#issue`; writer removal cannot leave an unlabeled
reader.

This contract deliberately does not cover producer correctness. In particular,
it would not have caught #1280: that incident was a writer-side integrity bug
where an executor failure was serialized as `result_status: completed`,
`exit_streak.json` retained `consecutive_failures: 0`, and the request was
unconditionally marked handled. Terminal status must come from the actual
executor outcome, failure streak updates must be durable, and handled markers
must be written only after truthful terminalization. Those are named non-goals
of this ADR and require a separate writer-side contract.

# Consequences

Unavailable is now distinguishable from genuine emptiness without making the runtime fail closed. Explicit caps keep reads bounded on the constrained host. Callers must make an intentional decision about partial and unavailable windows. Existing raw readers remain grandfathered until their dedicated migration issues move them to this contract.

# Alternatives considered

Per-reader fixes were rejected because prior fixes did not propagate. A generic exception/result hierarchy was rejected as unnecessary; these four state families share the small immutable result contract above. Unbounded full-history parsing was rejected for host resource safety.

# Test Contract

| Claim | Test / evidence | Status |
|---|---|---|
| Ledger windows preserve rotation, caps, and unavailable/partial status | `tests/test_state_access.py`, `tests/test_class_a_windows.py` | passing |
| Result evidence can select `results + archive` without request pollution and apply the bound after filtering | `tests/test_state_access.py::test_artifacts_filters_before_newest_bound_and_supports_directory_selection`, `tests/test_demand.py::test_archived_result_defect_is_visible`, merged PR #1276 | passing |
| Unreadable artifact sources are observable as `unavailable` | `tests/test_state_access.py::test_artifacts_unavailable_when_selected_sources_cannot_be_read` | passing |
| `CycleArchive` retirement has no remaining ordinary runtime path/reference | `tests/test_bridge_cycle_branch.py::TestLineSwitchRetired::test_nothing_in_the_runtime_reads_cycle_archive_or_stalls`, merged commit `56ea94d3` | passing; dynamic reflection/runtime string-building remain outside static coverage |
| Orphan registry remains load-bearing after removing `orphan:#1225` | `tests/test_state_path_writers.py` | passing |
| Ranking remains meaningful when `fitness.reward` is absent | `tests/test_evolution_tree.py::TestRankingWithoutRewardOrTrigger` in merged commit `56ea94d3` | passing |
| Every remaining allowlisted raw reader has a semantically safe Class A/B/C policy | No single enforcing test; requires per-reader review | audit judgement / not fully written |
| Writer-side truthfulness catches executor failure before handled marker emission | #1280 writer-side tests, separate issue/contract | outside this ADR; not covered |

# References

#790, #996, #1166, #1168, #1173, #1174, #1276, #1278, #1280
