# eeepc Self-Evolving Host Proof

Last updated: 2026-04-15 UTC

## Canonical Goal

Prove that the bounded self-evolving runtime on the live `eeepc` host can move from a blocked cycle to a real `PASS` cycle with durable host evidence, rather than only producing local docs or chat-style summaries.

## What Was Verified End-to-End

1. Local bounded runtime slice was implemented in the `nanobot` repo, tested, committed, and pushed to GitHub.
2. The updated `nanobot` gateway runtime was deployed to `eeepc` under a new pinned release directory.
3. The gateway service recovered successfully after a host-compatibility hotfix for missing `tiktoken`.
4. The live self-evolving source of truth on `eeepc` was confirmed to be the host control-plane state under `/var/lib/eeepc-agent/self-evolving-agent/state`, not the gateway workspace state.
5. The actual live blocker was identified from host reports and host code: the approval gate file required for bounded apply was missing.
6. After writing a valid approval gate file and rerunning the health service, the next live cycle finished with `PASS`.
7. The resulting host report shows a concrete applied artifact, not only a narration of queued work.

## Evidence Chain

### 1. Repo and publish proof

Local repo:
- `/home/ozand/herkoot/Projects/nanobot`

Key commits pushed to `origin/main`:
- `f1ffd6c` — durable self-evolving runtime and promotion workflow
- `6393aa1` — startup compatibility without `tiktoken`

### 2. Host deploy proof

Active deployed gateway release on `eeepc`:
- `/home/opencode/.nanobot-eeepc/runtime/pinned/20260415-0200-6393aa1`

Current symlink target:
- `/home/opencode/.nanobot-eeepc/runtime/pinned/current`

Gateway service:
- `nanobot-gateway-eeepc.service`

Observed healthy startup signals included:
- gateway process active
- cron service started
- heartbeat started
- Telegram bot connected

### 3. Live source-of-truth proof

The deployed gateway workspace did not contain the expected canonical self-evolving state:
- `/home/opencode/.nanobot-eeepc/workspace/state/...` was missing

The actual live self-evolving control-plane state was confirmed here:
- `/var/lib/eeepc-agent/self-evolving-agent/state`

Health trigger surfaces:
- `eeepc-self-evolving-agent-health.service`
- `eeepc-self-evolving-agent-health.timer`

### 4. Blocker proof

Host reports and host control-plane code showed bounded apply was blocked by a missing approval file.

Required gate file:
- `/var/lib/eeepc-agent/self-evolving-agent/state/approvals/apply.ok`

Required field:
- `expires_at_epoch`

Observed BLOCK characteristics before the fix:
- approval status effectively missing
- `promotion_execute_denied`
- `approval_required`
- bounded apply unavailable

### 5. Fix proof

A valid approval gate file was written to:
- `/var/lib/eeepc-agent/self-evolving-agent/state/approvals/apply.ok`

Then the host self-evolving trigger was rerun.

### 6. PASS proof

Fresh PASS report:
- `/var/lib/eeepc-agent/self-evolving-agent/state/reports/evolution-20260415T230020Z.json`

Key report facts:
- `capability_gate.approval.ok = true`
- `capability_gate.approval.reason = "valid"`
- `capability_gate.capabilities.bounded_apply.allowed = true`
- `capability_gate.capabilities.promotion_execute.allowed = true`
- `process_reflection.status = "PASS"`
- `follow_through.status = "artifact"`
- `follow_through.artifact_paths = ["prompts/diagnostics.md"]`
- the change was actually applied with a backup written under `state/backups/`

The report therefore proves a real bounded host action occurred after the approval gate was restored.

## What the System Currently Does

The live `eeepc` system is currently split across two different runtime surfaces:

1. Repo/local bounded runtime slice
- writes canonical workspace-style artifacts under `workspace/state/...`
- has explicit durable cycle, outbox, goal, and promotion surfaces
- is now pushed and deployable

2. Live host control-plane runtime
- writes canonical host truth under `/var/lib/eeepc-agent/self-evolving-agent/state`
- already has real hourly/health-triggered cycle execution
- truthfully enforces approval freshness for bounded apply
- can produce `BLOCK` and `PASS` with durable reports and artifact backups

## Gaps, Ranked by Impact

### 1. Biggest gap: runtime split between deployed gateway and host control-plane

Impact: highest

The repo implementation and the live host self-evolving truth do not currently converge on one shared state model. The newly implemented workspace-state runtime exists, but the real `eeepc` self-evolving authority is still the host control-plane rooted in `/var/lib/eeepc-agent/self-evolving-agent/state`.

### 2. Approval gate is operationally manual

Impact: high

The system can now pass, but continued bounded apply still depends on a short-lived operator approval file. That is acceptable for safety, but the operational workflow for refreshing or intentionally expiring it is not yet documented as a stable runbook.

### 3. Gateway deploy success is not the same as self-evolving convergence

Impact: medium-high

The gateway rollout is now validated, but gateway health alone does not prove the repo’s new workspace-state slice is the same thing the host uses for autonomous improvement.

### 4. Promotion and workspace-state surfaces are not yet the live host’s canonical promotion path

Impact: medium

The repo now writes promotion candidates and review artifacts, but the `eeepc` host proof came from the control-plane report/apply path rather than from the repo’s workspace promotion store.

### 5. Existing non-fatal environment drift remains

Impact: low-medium

The host still reports an MCP warning (`No module named 'mcp'`) for gateway startup. This was not fatal to the bounded self-evolving proof, but it is still drift.

## Most Likely Root Cause of the Remaining Architectural Mismatch

The system evolved along two partially overlapping paths:
- a newer repo-side bounded workspace runtime, and
- an older or separate host control-plane that already owns live autonomous execution.

Because the live host control-plane remains the real execution authority, deploying the repo gateway alone does not automatically migrate self-evolving truth to the repo’s new canonical workspace state model.

## Smallest Bounded Fix Sequence From Here

1. Document the live authority boundary explicitly.
- Treat `/var/lib/eeepc-agent/self-evolving-agent/state` as the current live self-evolving source of truth on `eeepc`.
- Treat the deployed gateway workspace runtime as a separate implementation slice until convergence is complete.

2. Add a short operator runbook for approval refresh.
- exact gate file path
- exact JSON schema
- TTL guidance
- verification command
- expected `PASS` evidence

3. Decide one convergence direction.
Choose exactly one:
- migrate the host control-plane to emit the same workspace-state surfaces as the repo runtime, or
- adapt the repo runtime/docs so the host control-plane state tree is the canonical authority.

4. Verify convergence with one proof target.
After the chosen convergence step, require one cycle to show:
- durable goal selection
- truthful approval state
- durable report
- durable artifact or promotion record
- operator-facing summary derived from the same source of truth

## Verification / Proof Required Going Forward

Any future claim that the self-evolving runtime is fully restored should include all of the following on the live host:
- a fresh report path
- cycle status (`PASS` or `BLOCK`)
- the goal ID selected
- approval status from the same report
- the concrete artifact path or promotion/evidence path produced by that cycle
- confirmation that the operator summary is derived from the same canonical state tree

## Bottom Line

The project has crossed the most important operational threshold:
- the live `eeepc` self-evolving cycle was not only deployed and observed, but moved from `BLOCK` to `PASS` after the real gate condition was satisfied.

The remaining problem is no longer “does it work at all?”
The remaining problem is “which runtime/state surface is canonical, and how do we converge the repo implementation with the live host control-plane?”
