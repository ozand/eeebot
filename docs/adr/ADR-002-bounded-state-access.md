---
title: Bounded state access
status: accepted
date: 2026-09-02
authors: [eeebot maintainers]
related: ["#1173", "#1174"]
tags: [runtime, state, reliability]
---

# Status

Accepted as the foundation for the state-reader migrations tracked by #1175–#1179.

# Context

Runtime readers independently reimplemented ledger rotation, horizons, ordering, and size caps. Fail-open readers also collapsed unavailable state into empty state, making silent dead paths difficult to diagnose.

# Decision

`nanobot.runtime.state_access` is the single stdlib-only foundation for bounded runtime-state reads. It exposes immutable `Window`, `Latest`, and `Sidecar` results. Readers never raise: `Window.status` is `complete`, `partial`, or `unavailable`; `Window.covered_from` and `Window.covered_to` report the observed timestamp bounds (with `covered_to` making a recent-end cap boundary explicit); `Sidecar.status` is `present`, `absent`, `corrupt`, `oversize`, or `permission`.

Ledger windows read the active ledger and dated gzip archives newest-first, stop at the requested horizon and explicit byte cap, prefilter phases before JSON parsing, and report skipped/corrupt sources in notes. A non-permission I/O failure is reported as `io_error`, not `permission`. Artifact reads unify live and archived flat artifact directories with bounded candidate selection and deterministic mtime/name ordering. Latest-file reads break mtime ties by name. Sidecar reads enforce a caller-provided byte cap.

This foundation changes no production caller. Subsequent migration issues must state how their caller handles each non-complete status. The foundation itself does not choose a caller policy: `unavailable` is a value, not an exception, and callers must not interpret it as genuine emptiness.

# Consequences

Unavailable is now distinguishable from genuine emptiness without making the runtime fail closed. Explicit caps keep reads bounded on the constrained host. Callers must make an intentional decision about partial and unavailable windows. Existing raw readers remain grandfathered until their dedicated migration issues move them to this contract.

# Alternatives considered

Per-reader fixes were rejected because prior fixes did not propagate. A generic exception/result hierarchy was rejected as unnecessary; these four state families share the small immutable result contract above. Unbounded full-history parsing was rejected for host resource safety.

# References

#790, #996, #1166, #1168, #1173, #1174
