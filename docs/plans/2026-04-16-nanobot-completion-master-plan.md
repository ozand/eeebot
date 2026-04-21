# Nanobot Completion Master Plan

> **For Hermes:** Execute this plan from top to bottom without pausing for handoff unless a real blocker appears. Keep proofs, tests, deploy checks, and docs aligned at each slice.

**Goal:** Bring Nanobot to a project-complete state where live eeepc truth is readable and provable, repo-side runtime outputs are comparable and traceable, promotion lifecycle is operator-visible, deploy/verification workflow is reproducible, and completion criteria are explicitly documented.

**Architecture:** The completion path does not replace the live eeepc control-plane. Instead, it converges the repo-side runtime and documentation around the already validated live authority root and adds the minimum read/write/promotion proof layers needed for a coherent finished system. Each slice must end with code or docs plus focused verification.

**Tech Stack:** Python, Typer CLI, pytest, repo-side runtime state helpers, eeepc side-by-side verification releases, markdown docs

---

## Final Completion Goal

Nanobot is considered complete when all of the following are true:

1. live eeepc self-evolving truth is readable through one explicit authority boundary
2. repo-side bounded runtime emits comparable summary/index artifacts
3. promotion lifecycle is visible end-to-end in status and durable files
4. live eeepc PASS proof and repo-side bounded proof both exist and are documented
5. deploy/verification/rollback workflow is reproducible and documented
6. one canonical completion contract and done criteria document exist

## Phase 1 — already completed foundations

These are already done and should be preserved, not reworked:
- docs vs code vs live-host audit
- minimal durable runtime slice locally
- GitHub publish flow
- eeepc deploy + compatibility fix
- live blocker diagnosis and BLOCK -> PASS host proof
- read-path authority convergence
- write-path comparable `outbox/report.index.json`
- promotion pointers in report index
- promotion summary in status
- deterministic promotion precedence
- promotion decision-trail visibility in status

## Phase 2 — finish promotion proof package

### Task 2.1 — Full repo-side promotion trail proof note
**Objective:** Record one coherent repo-side example showing candidate -> summary -> decision record -> accepted record visibility.

**Deliverable:**
- create `docs/EEEPC_REPO_SIDE_PROMOTION_TRAIL_PROOF.md`

**Verification:**
- ensure status/reader tests remain green

### Task 2.2 — Add focused reviewed/accepted trail test if needed
**Objective:** Lock a concrete accepted promotion example into tests if the current status coverage is still too synthetic.

**Files:**
- modify `tests/test_commands.py` only if the existing coverage does not already prove the full trail

**Verification:**
- `python3 -m pytest tests/test_commands.py -k promotion -v`

## Phase 3 — finish repo-side bounded cycle proof

### Task 3.1 — Repo-side cycle proof note
**Objective:** Document how to read a bounded repo-side cycle from report + report.index + promotions store.

**Deliverable:**
- create `docs/EEEPC_REPO_SIDE_BOUNDED_CYCLE_PROOF.md`

**Required proof fields:**
- status
- report source
- goal id
- approval summary
- artifact list
- promotion summary
- decision-trail visibility

**Verification:**
- focused coordinator/status tests green

## Phase 4 — stabilize eeepc verification/deploy workflow

### Task 4.1 — Consolidated eeepc verification/deploy runbook
**Objective:** Replace scattered deployment knowledge with one canonical side-by-side verification and rollout runbook.

**Deliverable:**
- create `docs/EEEPC_DEPLOY_VERIFY_ROLLBACK_RUNBOOK.md`

**Must include:**
- archive/scp pattern
- side-by-side unpack
- verification release usage with `PYTHONPATH`
- live authority status command
- optional symlink switch
- rollback rule

### Task 4.2 — release validation checklist
**Objective:** Add a concise pre-deploy and post-deploy checklist.

**Deliverable:**
- include checklist in the same runbook or companion note

## Phase 5 — write canonical completion contract

### Task 5.1 — Nanobot completion contract
**Objective:** Define the exact end-state contract for the project.

**Deliverable:**
- create `docs/NANOBOT_COMPLETION_CONTRACT.md`

**Must define:**
- canonical repo/source truth
- live host truth
- repo-side write-path truth
- promotion precedence truth
- proof obligations
- what is in scope for “done”
- what is intentionally out of scope

### Task 5.2 — self-evolving done criteria note
**Objective:** Add a short explicit done-criteria note for the self-evolving restoration/convergence project.

**Deliverable:**
- create `docs/NANOBOT_DONE_CRITERIA.md`

## Phase 6 — final proof bundle

### Task 6.1 — refresh live eeepc proof if needed
**Objective:** Ensure there is one clean final live proof record using the current status surface and authority root.

**If runtime behavior has not changed materially since the latest live proof, reuse existing live proof note. If it has changed materially, rerun a fresh side-by-side verification release.**

### Task 6.2 — final project completion summary
**Objective:** Produce one concise document summarizing what is complete and what remains intentionally out of scope.

**Deliverable:**
- create `docs/NANOBOT_FINAL_COMPLETION_SUMMARY.md`

**Must include:**
- live authority proof status
- repo-side write-path proof status
- promotion proof status
- deploy/runbook status
- completion contract status
- remaining non-goals

## Execution Order

Run tasks in this exact order:
1. Phase 2.1
2. Phase 2.2 if needed
3. Phase 3.1
4. Phase 4.1
5. Phase 4.2
6. Phase 5.1
7. Phase 5.2
8. Phase 6.1 if needed
9. Phase 6.2

## Completion Definition

The project is finished when:
- all listed deliverable docs exist in repo
- focused tests for status/coordinator/promotion remain green
- live eeepc proof is still valid or has been refreshed
- the completion contract and final summary are committed and pushed

## Current Immediate Next Step

Status note: this is historical plan guidance tied to the plan date, not a live runtime/task status surface.

Start with Phase 2.1:
- write `docs/EEEPC_REPO_SIDE_PROMOTION_TRAIL_PROOF.md`
- verify promotion/status tests
- commit and push
