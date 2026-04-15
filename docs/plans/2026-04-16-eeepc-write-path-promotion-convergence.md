# eeepc Write-Path and Promotion Convergence Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Add the smallest write-side convergence layer so repo-side bounded runtime cycles emit a host-comparable summary/index contract and promotion pointers, without replacing the live eeepc control-plane executor.

**Architecture:** Keep `runtime/coordinator.py` as the repo-side bounded writer, but add one compact summary/index contract that mirrors the minimal fields already proven in the live host control-plane. Treat this as a compatibility bridge, not a full unification. The `status` reader already knows how to consume host-style authority surfaces; this slice should make repo-side writes more comparable to that shape.

**Tech Stack:** Python, existing `nanobot.runtime.coordinator`, `nanobot.runtime.state`, `nanobot.runtime.promotion`, pytest

---

## Codebase Facts Confirmed

Repo-side writer:
- `nanobot/runtime/coordinator.py`

Repo-side promotion review flow:
- `nanobot/runtime/promotion.py`

Repo-side status/reader path:
- `nanobot/runtime/state.py`
- `nanobot/cli/commands.py`

Existing repo-side write surfaces:
- `state/reports/evolution-*.json`
- `state/goals/active.json`
- `state/outbox/latest.json`
- `state/promotions/latest.json`
- `state/promotions/<candidate>.json`

Live eeepc proof surfaces already validated:
- `reports/evolution-*.json`
- `goals/registry.json`
- `outbox/report.index.json`

## Target Behavior

After this slice:
- repo-side bounded cycles emit a stable outbox index comparable to `report.index.json`
- the index points to the latest report and includes goal/status/follow-through/artifact/approval summary
- when promotion candidates exist, the index includes promotion pointers
- status/reader code can compare repo-side and live eeepc proof surfaces more directly

---

### Task 1: Add failing test for a host-comparable outbox report index

**Objective:** Lock the desired repo-side compatibility artifact before changing production code.

**Files:**
- Modify: `tests/test_runtime_coordinator.py`
- Modify: `tests/test_commands.py` (only if an additional status assertion is needed)

**Step 1: Write failing test**

Add a test after a fresh repo-side cycle that expects:
- `state/outbox/report.index.json` to exist
- fields such as:
  - `status`
  - `source`
  - `goal.goal_id`
  - `goal.follow_through.artifact_paths`
  - `capability_gate.approval` when approval is present

**Step 2: Run test to verify failure**

Run:
`python3 -m pytest tests/test_runtime_coordinator.py -k report_index -v`

Expected: FAIL — file does not exist yet.

**Step 3: Write minimal implementation**

In `nanobot/runtime/coordinator.py`:
- after writing `outbox/latest.json`, also write `outbox/report.index.json`
- keep payload minimal and host-comparable
- do not attempt to clone the full live eeepc control-plane JSON

**Step 4: Run test to verify pass**

Run:
`python3 -m pytest tests/test_runtime_coordinator.py -k report_index -v`

Expected: PASS

**Step 5: Commit**

```bash
git add nanobot/runtime/coordinator.py tests/test_runtime_coordinator.py
git commit -m "feat: emit host-comparable report index"
```

---

### Task 2: Add promotion pointer coverage to the report index

**Objective:** Ensure the compatibility index can lead operator/reporting code to promotion candidates when they exist.

**Files:**
- Modify: `nanobot/runtime/coordinator.py`
- Modify: `tests/test_runtime_coordinator.py`
- Optionally modify: `tests/test_promotion_workflow.py`

**Step 1: Write failing test**

For a PASS repo-side cycle, assert the new `report.index.json` includes:
- promotion candidate id
- candidate path or promotion summary pointer

**Step 2: Run test to verify failure**

Run:
`python3 -m pytest tests/test_runtime_coordinator.py -k promotion_index -v`

Expected: FAIL — pointer not present yet.

**Step 3: Write minimal implementation**

Add a small `promotion` object or flat promotion pointer fields to `report.index.json`.
Keep it compact.

**Step 4: Run test to verify pass**

Run:
`python3 -m pytest tests/test_runtime_coordinator.py -k promotion_index -v`

Expected: PASS

**Step 5: Commit**

```bash
git add nanobot/runtime/coordinator.py tests/test_runtime_coordinator.py
git commit -m "feat: include promotion pointers in report index"
```

---

### Task 3: Make reader docs/status expectations explicit for repo-side comparable index

**Objective:** Document how repo-side write convergence now maps onto the live read-path truth already validated.

**Files:**
- Create or modify: `docs/EEEPC_WRITE_PATH_PROMOTION_CONVERGENCE_NOTE.md`
- Optionally modify: `docs/EEEPC_RUNTIME_STATE_AUTHORITY_USAGE.md`

**Step 1: Write doc update**

Document:
- repo-side `outbox/report.index.json`
- how it compares to live eeepc `outbox/report.index.json`
- what is intentionally still different
- which fields are meant to stay comparable

**Step 2: Verify tests still pass**

Run:
`python3 -m pytest tests/test_runtime_coordinator.py tests/test_commands.py -v`

Expected: PASS

**Step 3: Commit**

```bash
git add docs/...
git commit -m "docs: record write-path convergence contract"
```

---

### Task 4: Add one comparison proof artifact

**Objective:** Produce one small proof note showing repo-side comparable index vs live eeepc index fields.

**Files:**
- Create: `docs/EEEPC_WRITE_PATH_CONVERGENCE_PROOF.md`

**Step 1: Record the compared fields**

At minimum compare:
- status
- report source
- goal id
- follow-through artifact paths
- approval summary
- promotion pointer presence

**Step 2: Keep it bounded**

Do not claim full unification.
Claim only that a comparable summary/index contract now exists.

**Step 3: Commit**

```bash
git add docs/EEEPC_WRITE_PATH_CONVERGENCE_PROOF.md
git commit -m "docs: add write-path convergence proof note"
```

---

## Final Verification

Run the focused suite:

```bash
python3 -m pytest \
  tests/test_runtime_coordinator.py \
  tests/test_promotion_workflow.py \
  tests/test_commands.py \
  -v
```

Expected:
- repo-side cycles emit a stable report index
- promotion candidates are discoverable from that index when present
- existing reader/status tests remain green

## Notes

- Do not change the live eeepc executor in this slice.
- Do not auto-promote into canonical repos.
- Do not replace `outbox/latest.json`; add the comparable index beside it.
- The success condition is a comparable write-side summary contract, not complete runtime unification.
