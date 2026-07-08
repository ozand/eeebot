# Design: ledger/artifact memory schema

- **Issue:** #704 (loop-redesign ticket C)
- **Status:** design/contract — no code changed by this document
- **story_id:** docs/specs/self-evolving-runtime/spec.md

## Overview

Per #702, the state-light loop's only durable memory is **operational/
artifact truth** — never a control graph. Five ledgers/artifacts make up
that truth; three already exist and are reused unchanged, two are net-new.

## Existing vs net-new

| Ledger | Status | Source of truth today |
|---|---|---|
| Done ledger | **net-new** | none (approximated by `_task_already_done`'s 7-day git-log fuzzy match, `nanobot/runtime/bridge.py`) |
| Failure ledger | **net-new** | none (rejected/blocked cycles visible only per-result-file, not aggregated) |
| Integration ledger | **existing, reuse as-is** | `rollback` record in `_write_bridge_completed_result`'s payload, `state/subagents/results/result-*.json` |
| Prompt/response dump | **existing, reuse as-is** | `nanobot.observability.llm_telemetry.record_llm_prompt` (#693), `<STATE_DIR>/llm_calls/prompts/YYYY-MM-DD.jsonl` |
| Telemetry | **existing, reuse as-is** | `nanobot.observability.llm_telemetry.record_llm_call` (#675), `<STATE_DIR>/llm_calls/YYYY-MM-DD.jsonl` |

---

## 1. Done ledger (net-new)

**Purpose:** durable record of every change that reached `main`. Replaces
the `_task_already_done` git-log approximation #703's P2 check names as a
stopgap "until #704's ledger schema exists." Feeds context-builder dedup,
P2, the #672 harvest pass, and #705's integration-rate/harvest-yield.

**File layout:** `<STATE_DIR>/ledger/done/YYYY-MM-DD.jsonl`, one line per
integrated cycle. `<STATE_DIR>` resolves like `llm_telemetry._llm_calls_dir()`
today (env override, else `STATE_DIR`, else `~/.nanobot`). Rotation: same
shape as #693's `prompts/` (gzip previous day on next write, prune
`.jsonl.gz` past retention). Default retention **90 days** — see "Retention
policy."

**Fields:**

| Field | Type | Meaning |
|---|---|---|
| `ts` | string (UTC ISO-8601) | append time (integration completion) |
| `cycle_id` | string | joins to telemetry/prompt-dump/integration-ledger |
| `title` | string | proposed task/backlog title (same space P2 matches against) |
| `summary` | string | mirrors the result payload's `summary` |
| `files_changed` | list[string] | mirrors the result payload's `files_changed` |
| `commit_sha` | string | `rollback.main_sha_after` — git-verifiable anchor |
| `general_or_host_local` | enum: `general`\|`host_local`\|`unclassified` | #672 generality-filter tag; default `unclassified` until a harvest pass or the proposing LLM assigns one |
| `source_artifact` | string | mirrors `req.get('source_artifact')` on the result payload, when present |

**Write point:** appended once, alongside the bridge's `_write_bridge_
completed_result` calls where `result_status='completed'` and
`rollback.integrated is True` (`nanobot/runtime/bridge.py`, call sites near
lines 916/1008/1075). Description of where the write belongs, not an
implementation — #707 wires it using fields the result payload already
computes. `already_done`/`no_commit` outcomes do not write a done entry.

**Read consumers:** context-builder (recent `title`/`summary`/
`files_changed` → "don't repeat this" prompt content, replacing ad hoc
git-log context); #703 P2 (`precheck_duplicate_vs_done_ledger`, same
keyword-overlap matching, new data source); #672 harvest (`general` entries
as candidates, `commit_sha` to port); #705 (integration rate, harvest
yield).

---

## 2. Failure ledger (net-new)

**Purpose:** durable record of proposals that did not reach `main` —
precheck rejects/skips/aborts, gate failures, no-commit outcomes. Feeds
"don't repeat a just-rejected proposal" context and a queryable failure
trail (vs. reading individual blocked result stubs one at a time).

**File layout:** `<STATE_DIR>/ledger/failure/YYYY-MM-DD.jsonl`. Same
rotation mechanism as the done ledger. Default retention **30 days**
(higher-volume, lower long-term value than done entries).

**Fields:**

| Field | Type | Meaning |
|---|---|---|
| `ts` | string (UTC ISO-8601) | append time |
| `cycle_id` | string | the attempting cycle |
| `proposed_title` | string | the rejected/failed task title |
| `stage` | enum: `precheck`\|`gate`\|`no_commit` | which safety-shell stage produced the outcome |
| `reason` | string | #703's reason string for `stage='precheck'` (`precheck_mutation_surface_violation`, `precheck_duplicate_vs_done_ledger`, `precheck_head_not_on_main`, `precheck_dirty_tree`, `precheck_lock_not_held`), or `rollback.reason` (e.g. `gate_failed`, `mutation_surface_violation`, `blocked_file_present`) for `stage='gate'` |
| `target_paths` | list[string]\|null | the proposal's declared `target_paths` (#703 P1 input), when carried on the request |

**Write point:** alongside each outcome's existing write. Precheck
reject/skip/abort: alongside the existing `blocked`-result / R27 path (the
`_rollback_reason` assignments in `main()`, `nanobot/runtime/bridge.py`
~lines 1318-1418). Gate failure: alongside `_write_bridge_completed_result`
calls where `rollback.integrated is False` and `result_status` isn't
`already_done`. No-commit: alongside `result_status='no_commit'` calls.
`already_done` is a P2 precheck skip, not a failure — recorded once as
`stage='precheck'`/`reason='precheck_duplicate_vs_done_ledger'`, never
double-counted as a separate outcome.

**Read consumers:** context-builder (recent `proposed_title`/`reason` →
avoid repeating unproductive proposals); #705 (novelty rate = distinct
titles across done+failure ledgers; `reason` histogram for stall
diagnosis). #703's P2 does not read this ledger (P2 only checks done work).

---

## 3. Integration ledger (existing — map only)

Already implemented: the `rollback` record in `_write_bridge_completed_
result`'s payload (`nanobot/runtime/bridge.py` lines 2279-2386), at
`state/subagents/results/result-<request_id>.json`:

```
rollback = {"integrated": bool, "cycle_branch": str, "main_sha_before": str,
            "main_sha_after": str, "reason": str | None, "auto_committed": bool}
```

`main_sha_before == main_sha_after` whenever `integrated` is False — the
existing git-verifiable guarantee (#653, #666, #678) that a non-integrated
cycle never moves `main`. This document does not redesign it.

**Surfacing needed:** none beyond what's already emitted. `main_sha_after`
and `reason` are the direct sources for the done/failure ledgers above — the
two new ledgers are a derived, append-only index over this record's
history, so the context-builder and #705 don't have to glob and parse every
`result-*.json` to answer "what was recently done or rejected." No change
to `_write_bridge_completed_result`'s signature is proposed.

---

## 4. Prompt/response dump (existing — map only)

Already built (#693): `nanobot.observability.llm_telemetry.
record_llm_prompt`, `<STATE_DIR>/llm_calls/prompts/YYYY-MM-DD.jsonl`,
gzip-archived, pruned by `LLM_PROMPTS_RETENTION_DAYS` (default 14). Fields:
`ts`, `model`, `cycle_id`, `component`, `seq`, `prompt_tokens`,
`completion_tokens`, `finish_reason`, `messages`, `content`,
`reasoning_content`, secret-redacted. Reused as-is.

**Read consumers:** diagnosis (`scripts/llm_prompt_inspect.py`, existing) —
inspect what the LLM proposed when a `cycle_id` shows a surprising failure
`reason`; #705 cost analysis joined by `cycle_id`.

## 5. Telemetry (existing — map only)

Already built (#675): `nanobot.observability.llm_telemetry.
record_llm_call`, `<STATE_DIR>/llm_calls/YYYY-MM-DD.jsonl`, daily rotation,
no gzip. Fields: `ts`, `model`, `duration_ms`, `prompt_tokens`,
`completion_tokens`, `total_tokens`, `finish_reason`, `retries`,
`cycle_id`, `component`. Reused as-is.

**Read consumers:** #705 cost/latency metrics, joined by `cycle_id` against
done/failure ledgers for cost-per-integrated-change and
cost-per-rejected-proposal.

---

## Retention/compaction policy

One rotation shape across all ledger directories, consistent with #693's
shipped pattern, bounding disk growth on the constrained host:

| Directory | Rotation | Compression | Default retention |
|---|---|---|---|
| `llm_calls/YYYY-MM-DD.jsonl` (#675) | daily | none | unbounded (unchanged, out of scope) |
| `llm_calls/prompts/YYYY-MM-DD.jsonl` (#693) | daily, gzip prior day | gzip | 14 days (`LLM_PROMPTS_RETENTION_DAYS`) |
| `ledger/done/YYYY-MM-DD.jsonl` (net-new) | daily, gzip prior day | gzip | 90 days |
| `ledger/failure/YYYY-MM-DD.jsonl` (net-new) | daily, gzip prior day | gzip | 30 days |

Done entries are small (no `messages` payload) and are the harvest/novelty
corpus, so a longer 90-day window costs little disk while improving harvest
recall. Failure entries are higher-volume and lower long-term value, hence
30 days. Both are configuration, not architecture — #707 should expose
`LEDGER_DONE_RETENTION_DAYS`/`LEDGER_FAILURE_RETENTION_DAYS` env overrides
mirroring `LLM_PROMPTS_RETENTION_DAYS`'s pattern, not hardcode them.
Rotation/pruning should reuse `_rotate_and_prune`'s shape
(`nanobot/observability/llm_telemetry.py` lines 167-202) — gzip prior-day
plain files, prune expired `.jsonl.gz`, best-effort per file — rather than
reinventing rotation logic.

## Coverage check — #705 metrics

| Metric | Ledger field(s) | Present? |
|---|---|---|
| Novelty rate | done `title` + failure `proposed_title`, both with `ts`/`cycle_id` | yes |
| Integration rate | done-ledger entry count vs. total cycles (from telemetry `cycle_id` cardinality) | yes |
| Harvest yield | done `general_or_host_local == 'general'` count per window | yes |
| Cost | telemetry `total_tokens`/`duration_ms` joined by `cycle_id` to done/failure ledgers | yes |

## Coverage check — context-builder dedup

Needs, per cycle: recent done titles (avoid re-proposing finished work) and
recent failure titles+reasons (avoid re-proposing just-rejected work). Both
are direct `ts`-bounded reads of the fields specified above — nothing
additional required.

## References

- `docs/changes/702-ledger-loop-architecture-decision/decision.md` —
  architecture decision this schema implements.
- `docs/changes/703-safety-shell-invariants/precheck-contract.md` — P2's
  dependency on the done ledger; P1's `target_paths` recorded by the
  failure ledger.
- `nanobot/observability/llm_telemetry.py` — existing telemetry (#675) and
  prompt-dump (#693), reused as-is and as the rotation-logic template.
- `nanobot/runtime/bridge.py` — `_write_bridge_completed_result` (integration
  ledger, lines 2279-2386), `_task_already_done` (git-log approximation
  superseded for P2, lines 1862-1903), `_rollback_reason` assignment sites
  (~lines 1318-1418, this ticket's failure-ledger write points).
- `docs/changes/672-product-instance-flow/design.md` — the generality filter
  the done ledger's `general_or_host_local` tag reuses, and the harvest
  mechanism consuming it.
- `docs/specs/self-evolving-runtime/spec.md` — "Evidence / observability"
  section this design extends.
