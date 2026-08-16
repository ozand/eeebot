# Change: Enforceable-vs-advisory audit of the bounded gate

- **change-id:** gate-enforceable-advisory-map
- **issue:** #846
- **capability:** docs/specs/self-evolving-runtime (bounded gate / precheck / confirmation)
- **role / workstream:** role:developer / workstream:trust-safety

## Problem

a-evolve (KB `agents/a-evolve`) distinguishes ENFORCEABLE controls (code-enforced:
a violation is structurally impossible or deterministically blocked/detected) from
ADVISORY ones (heuristics or prompt-level expectations that depend on the chosen
strategy or on the instance cooperating). Our bounded gate mixes both. Any check
that only guards trust/safety *advisorily* is a latent hole. This is a one-time
audit + close the real gaps.

## Enforceable / advisory map

Verdict: the system is **mostly enforceable**, with fail-closed defaults on the
gate-critical paths. The exceptions are enumerated below.

### Enforceable (code-enforced, fail-closed) — no action

| Check | file:line | Guards | Mechanism |
|---|---|---|---|
| Blocked filename patterns (`.env`, secret, token, id_rsa, lockfiles) | `bridge.py:2394-2429,2508-2549` | safety | hard block before `record_gate_decision`; recomputed after every repair turn |
| Mutation-surface allowlist + immutable deny-set (`_RUNTIME_DENY_ALWAYS_FILES`/`_RUNTIME_DENY_TOKENS`) | `bridge.py:2450-2479` | trust (#603/#812) | fail-closed token match on basename; traversal-normalized; deny-set always wins over the allow-slice |
| Runtime-slice env parse (`SELFEVO_RUNTIME_SLICE`) | `bridge.py:2482-2505` | trust | operator-only env (off instance surface); deny-set filtered out of the slice |
| Runtime-slice tier never auto-integrates | `bridge.py:2136-2166,2552-2605` | trust (#812) | green result always writes `review_status: not_ready_for_policy_review`; no code path flips it to accept |
| Smoke gate (import-smoke + targeted pytest) | `bridge.py:2726-2829` | safety | fail-closed on every branch (missing dir, empty selection, 0-collected, crash, timeout → False) |
| `_integrate_cycle_to_main` (only bridge path that advances origin/main) | `bridge.py:482-546` | safety | `--no-ff`, empty-merge detection, reset-on-failure, resolved-sha compare |
| #678 F5/F6 bookkeeping pushes | `bridge.py:393-420,2191-2252` | safety | `_diff_against_remote_touches_only` gate before each such push |
| `confirm_serves` 3-pass (tamper→benchmark→harness-signal) | `usage_evidence.py:536-795` | trust (#789/#813/#819/#838) | foreign `signal` stripped + re-derived every call; no stored confirmed status trusted at face value |
| `_sidecar_corroborates_use` reference-forgery guard | `usage_evidence.py:501-533` | trust | requires independent sidecar evidence strictly after completion ts |
| `benchmark_evidence.verify_benchmark` | `benchmark_evidence.py:352-468` | trust (#813/#819) | `SELFEVO_BENCHMARK_TRUST` operator-only, default OFF; re-derives from protected history, never the artifact's own numbers |
| Held-out pack invisibility (#780) | `heldout/__init__.py` | trust | checkers live only in `nanobot/` (off the instance's committable surface); fixtures tmpdir-only |
| Held-out sandbox (subprocess, env-stripped) | `heldout/__init__.py:142-169` | safety | `sys.executable`, `cwd=tmpdir`, 30s timeout, minimal env |
| Flaky-check exclusion (#842) | `heldout/__init__.py:172-201` | trust | non-deterministic verdicts forced to `skip`, trusted neither way |
| `promotion.review_promotion_candidate` (as a function) | `promotion.py:207-282` | trust | even `accept` only writes JSON; no code in `nanobot/` applies an accepted patch |

### Partial / advisory — the real holes

| # | Check | file:line | Guards | Why it is a hole | Disposition |
|---|---|---|---|---|---|
| 1 | Subagent "do NOT `git push`" | `bridge.py:1120-1130` (+ `agent/tools/shell.py:40-50` deny_patterns has no git pattern) | safety (critical) | prompt-only; subagent shares the workspace git creds → a deliberate `git push origin HEAD:main` bypasses the entire gate; bridge only defends the accidental case | **HARDENED here** — detection backstop |
| 2 | Coordinator self-evolving lane auto-accepts its own promotion candidates | `coordinator.py:219,449-462,592-599` | trust (critical) | `result_status=PASS` unconditional; `review_status=ready` from a mode check not gated on `_has_concrete_changes`; coordinator itself calls `review_promotion_candidate(decision="accept")` with `base_commit/patch_hash=None`. Blast radius null today (nothing reads `accepted/`) but fabricates a false "reviewed" trail | **Follow-up #853** (live lane, behavioral care) |
| 3 | Suite-shrink guard counts tests, not content | `bridge.py:2886-2912` | safety | blocks a count drop but N real tests → N `def test_x(): pass` stubs keeps count flat | **HARDENED here** — name-superset check |
| 4 | `reference` usage-signal self-manufacturable | `usage_evidence.py:340-408` | trust (V2 metric only) | derived from instance-authored import text; a no-op companion importing the stem fakes it. Affects the value metric, not integration | **Follow-up #854** (needs #838 rework) |
| — | #789 spawn-hash detect-not-restore | `bridge.py:2085-2109` | trust | detection-only by design | acceptable, honestly documented |
| — | #812 bridge prompt "MANDATORY" language | `bridge.py:1120-1145` | cooperative | non-compliance degrades to a failed cycle, caught downstream | acceptable defense-in-depth |

## Intended change

Backstop the two advisory safety holes that are safe to convert in-place, and file
the two behaviorally-sensitive ones as follow-ups.

- **Hole #1 (out-of-band push):** add `_detect_out_of_band_main` — the bridge loop is
  serial, so within one cycle origin/main can only move via this cycle's own
  `_integrate_cycle_to_main`. Any other movement is a push that bypassed the gate:
  record an `out_of_band_main_push` integrity incident (same ledger channel as #789)
  and refuse to integrate on the now-stale base. Positive-detection-only and
  FAIL-OPEN (a detector error/network failure never blocks a legitimate cycle).
- **Hole #3 (test gutting):** the suite-shrink guard additionally requires the main
  baseline's test *function names* to be a subset of the current set — swapping real
  tests for stubs now fails even at a flat count.

## Acceptance

- [x] documented enforceable/advisory map of the gate (this file)
- [x] advisory trust/safety checks converted or backstopped, with tests — holes #1 and #3
- [x] behaviorally-sensitive holes filed with concrete backstops — #853 (coordinator), #854 (reference)

## Out of scope

- Rolling back / force-resetting an out-of-band-pushed origin/main (detection + refuse
  only; a force-reset on the live remote is riskier than the incident it would remedy).
- Credential separation / network sandbox for the subagent exec tool (larger design;
  the detection backstop closes the *masking* of the event, which was the gap).
- Hole #2 (coordinator auto-accept, #853) and hole #4 (reference chaining, #854).
