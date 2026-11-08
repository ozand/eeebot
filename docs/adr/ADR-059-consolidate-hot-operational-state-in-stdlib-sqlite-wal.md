---
title: Evaluate transactional hot state before selecting SQLite WAL
status: proposed
date: 2026-11-07
authors: [eeebot maintainers]
related: ["#2181", "#2177", "#2068", "#2162", "#2050"]
tags: [runtime, state, durability]
---

# Status

Proposed evaluation, not approval to migrate production state.

# Context

Related issues concern dispositions, retries and recovery. A database may simplify coupled updates, but the actual writer/reader inventory and benefit versus current file storage remain to be established. No measured latency, RAM bound or target-host compatibility is claimed.

# Decision

Before running an evaluation, define the representative coupled transition/workload, identical inputs and baseline, target environment (including any constrained/32-bit host), failure model, interruption points, measurements, and decision thresholds. #2068 separately owns disposition semantics and its reserved owner decisions; #2162 owns stale-call reconciliation; #2050 tracks distinct recovery/deadlock work. This evaluation does not decide or absorb those contracts, add a competing writer, or relax their authority boundaries. Compare current storage with a disposable standard-library `sqlite3` prototype; inject failures before, during and after commit, reopen and inspect consistency, and record latency, memory, recovery effort and environment. The evaluation issue reports evidence and a recommendation only; it does not authorize production adoption, migration, reader cutover or host operations.

Select journal and synchronization settings against the declared failure model. WAL with `synchronous=NORMAL` does not promise retention of the latest commits after power loss; process-interruption tests do not establish power-loss durability. Local transactions cannot atomically commit remote effects, subprocess/tool actions, or Git integration. Explicitly state which failures were and were not tested.

A separate governing issue and reviewed effective ADR are required before adoption. That decision must compare results against the predeclared thresholds, account for reader/writer inventory and target compatibility, and include schema migration, backup/restore (including WAL), disk-full behavior and rollback/cutover evidence. If the defined benefit is absent or thresholds are unmet, retain existing storage. Preserve archives and current single-writer authority meanwhile. No daemon, ORM, group commit or historical archive rewrite is required.

# Consequences

A transaction may simplify consistency for a measured coupled local transition, but adds schema, backup/restore, disk-full, reader cutover and rollback costs. Evaluation results are workload- and environment-specific; they do not guarantee constant RAM, sub-millisecond commits, target-host compatibility, or atomicity with external effects. Current storage and host state remain unchanged unless a separate authorized adoption decision and implementation are completed.

# Alternatives considered

Existing file storage remains a valid outcome if cheaper and adequate. Immediate database migration is rejected without evidence. External databases add unjustified operational cost.

# Test Contract

| Claim | Test | Currently |
|---|---|---|
| For the predeclared representative transition and tested process-failure points, SQLite reopen exposes only the states allowed by the documented transaction contract | `tests/test_operational_sqlite.py::test_atomic_reopen` | deferred (#2181); experiment evidence only, not power-loss proof |
| Reader compatibility, backup/restore and rollback/cutover behavior are characterized before any separate adoption decision | `tests/test_operational_sqlite.py::test_reader_and_rollback_compatibility` | deferred (#2181); test scope/thresholds must be fixed before evaluation |

# References

- #2181; #2068 (disposition semantics remain separately owner-controlled); #2162 (stale-call recovery remains separately owned); #2050. These links do not transfer ownership or authorize state changes.
- https://www.sqlite.org/pragma.html#pragma_synchronous
- https://earendil.com/posts/pi-durable/ (inspiration, not measured eeebot evidence).
