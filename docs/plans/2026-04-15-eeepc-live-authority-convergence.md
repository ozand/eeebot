# eeepc Live Authority Convergence Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Add the smallest possible authority-selection boundary so `nanobot status` and runtime-state readers can truthfully report from the live `eeepc` host control-plane state tree instead of silently assuming the workspace-state slice.

**Architecture:** Keep the existing workspace-state runtime intact, but introduce one explicit runtime-state source selection layer. The new reader should support at least two sources: `workspace_state` and `host_control_plane`. The CLI status command should use this boundary and surface which authority it is reading from.

**Tech Stack:** Python, Typer CLI, existing `nanobot.runtime.state` helpers, pytest

---

## Codebase Facts Confirmed

Current minimal reader path:
- `nanobot/runtime/state.py`

Current status command:
- `nanobot/cli/commands.py:1057-1073`

Current workspace-state writer path:
- `nanobot/runtime/coordinator.py`

Current relevant tests:
- `tests/test_commands.py` (`test_status_reports_runtime_surface` and nearby runtime-state tests)
- `tests/test_runtime_coordinator.py`
- `tests/test_heartbeat_service.py`
- `tests/test_promotion_workflow.py`

Current problem:
- `load_runtime_state(workspace)` hardcodes `workspace / "state"`
- `nanobot status` calls that reader directly
- there is no code-visible way to say “for eeepc, the live authority is `/var/lib/eeepc-agent/self-evolving-agent/state`”

## Target Behavior

After this slice:
- runtime state can be read from an explicit source selection boundary
- operator-facing output states which authority it used
- `workspace_state` remains the default
- a configured `host_control_plane` source can read from a host-style state root such as `/var/lib/eeepc-agent/self-evolving-agent/state`
- no code path silently claims workspace-state truth when the host authority is something else

---

### Task 1: Add a focused runtime-state source model

**Objective:** Introduce a small typed/configurable concept for where runtime state is being read from.

**Files:**
- Modify: `nanobot/runtime/state.py`
- Test: `tests/test_commands.py`

**Step 1: Write failing test**

Add a test that calls a new helper with a host-control-plane path and expects the returned metadata to identify:
- source kind: `host_control_plane`
- authority path: exact host state path

Suggested test shape:

```python
def test_load_runtime_state_from_host_control_plane_root(tmp_path):
    state_root = tmp_path / "host-state"
    (state_root / "reports").mkdir(parents=True)
    (state_root / "reports" / "evolution-1.json").write_text(
        json.dumps({"goal_id": "goal-1", "result_status": "PASS"}),
        encoding="utf-8",
    )

    runtime = load_runtime_state_from_root(
        state_root,
        source_kind="host_control_plane",
    )

    assert runtime["runtime_state_source"] == "host_control_plane"
    assert runtime["runtime_state_root"] == str(state_root)
```

**Step 2: Run test to verify failure**

Run:
`python3 -m pytest tests/test_commands.py -k host_control_plane_root -v`

Expected: FAIL — helper does not exist yet.

**Step 3: Write minimal implementation**

In `nanobot/runtime/state.py`:
- extract current logic into a root-based helper such as `load_runtime_state_from_root(state_root: Path, source_kind: str)`
- keep the current workspace behavior as a thin wrapper:
  - `load_runtime_state(workspace)` -> `load_runtime_state_from_root(workspace / "state", source_kind="workspace_state")`
- add returned metadata keys:
  - `runtime_state_source`
  - `runtime_state_root`

Do not change coordinator write behavior in this slice.

**Step 4: Run test to verify pass**

Run:
`python3 -m pytest tests/test_commands.py -k host_control_plane_root -v`

Expected: PASS

**Step 5: Commit**

```bash
git add nanobot/runtime/state.py tests/test_commands.py
git commit -m "feat: add explicit runtime state source metadata"
```

---

### Task 2: Add a small host-control-plane reader path

**Objective:** Make the state loader able to read a host-style state root directly, without requiring `workspace/state` nesting.

**Files:**
- Modify: `nanobot/runtime/state.py`
- Test: `tests/test_commands.py`

**Step 1: Write failing test**

Add a test that builds this layout directly under a root:
- `reports/evolution-20260415T230020Z.json`
- `outbox/cycle_sync_....json` or `outbox/latest.json`
- `goals/registry.json` or `goals/active.json`

Then assert the loader can extract:
- report path
- goal ID
- approval/gate-related info when available

Suggested test shape:

```python
def test_load_runtime_state_reads_host_control_plane_layout(tmp_path):
    state_root = tmp_path / "host-state"
    reports = state_root / "reports"
    reports.mkdir(parents=True)
    report_path = reports / "evolution-20260415T230020Z.json"
    report_path.write_text(
        json.dumps(
            {
                "goal": {"goal_id": "goal-44"},
                "process_reflection": {"status": "PASS"},
                "capability_gate": {"approval": {"ok": True, "reason": "valid"}},
                "follow_through": {"artifact_paths": ["prompts/diagnostics.md"]},
            }
        ),
        encoding="utf-8",
    )

    runtime = load_runtime_state_from_root(state_root, source_kind="host_control_plane")

    assert runtime["report_path"] == str(report_path)
    assert runtime["active_goal"] == "goal-44"
```

**Step 2: Run test to verify failure**

Run:
`python3 -m pytest tests/test_commands.py -k host_control_plane_layout -v`

Expected: FAIL — current reader only understands the workspace-state schema.

**Step 3: Write minimal implementation**

In `nanobot/runtime/state.py`:
- add small schema-normalization logic for host-control-plane report shapes
- support extracting fields from either:
  - current workspace-state report keys (`goal_id`, `result_status`, `approval_gate`, etc.)
  - host-control-plane report keys (`goal.goal_id`, `process_reflection.status`, `capability_gate.approval`, `follow_through.artifact_paths`)
- add one new user-facing field:
  - `runtime_status`
- add one optional field:
  - `artifact_paths`

Keep parsing narrow and evidence-based. Do not attempt a full cross-schema abstraction.

**Step 4: Run test to verify pass**

Run:
`python3 -m pytest tests/test_commands.py -k host_control_plane_layout -v`

Expected: PASS

**Step 5: Commit**

```bash
git add nanobot/runtime/state.py tests/test_commands.py
git commit -m "feat: read host control-plane runtime state layout"
```

---

### Task 3: Thread authority selection into the CLI status command

**Objective:** Make `nanobot status` truthfully report which runtime-state authority it is using.

**Files:**
- Modify: `nanobot/cli/commands.py`
- Modify: `nanobot/runtime/state.py`
- Test: `tests/test_commands.py`

**Step 1: Write failing test**

Add a CLI test that monkeypatches a runtime-state source selection and expects the output to include:
- authority kind
- authority root
- runtime status

Suggested assertion examples:

```python
assert "Runtime state source: host_control_plane" in result.stdout
assert f"Runtime state root: {state_root}" in result.stdout
assert "Runtime status: PASS" in result.stdout
```

**Step 2: Run test to verify failure**

Run:
`python3 -m pytest tests/test_commands.py -k "status and runtime state source" -v`

Expected: FAIL — CLI does not expose source metadata yet.

**Step 3: Write minimal implementation**

In `nanobot/cli/commands.py`:
- extend `status()` with one bounded optional parameter for runtime-state root selection, or
- read a small config/env-based override for runtime-state authority

Minimal acceptable implementation for this slice:
- `nanobot status --runtime-state-root <path> --runtime-state-source <workspace_state|host_control_plane>`

Requirements:
- preserve existing default behavior when flags are omitted
- when flags are present, use `load_runtime_state_from_root(...)`
- render new lines in `format_runtime_state(...)`:
  - `Runtime state source`
  - `Runtime state root`
  - `Runtime status`
  - optionally `Artifacts`

**Step 4: Run test to verify pass**

Run:
`python3 -m pytest tests/test_commands.py -k "status and runtime state source" -v`

Expected: PASS

**Step 5: Commit**

```bash
git add nanobot/cli/commands.py nanobot/runtime/state.py tests/test_commands.py
git commit -m "feat: expose runtime state authority in status command"
```

---

### Task 4: Add one focused regression test for existing workspace behavior

**Objective:** Prove the new authority boundary does not break the current workspace-state slice.

**Files:**
- Modify: `tests/test_commands.py`
- Optionally modify: `tests/test_runtime_coordinator.py`

**Step 1: Write failing or tightening test**

Extend the existing `test_status_reports_runtime_surface` to assert:
- `Runtime state source: workspace_state`
- `Runtime state root: <workspace>/state`
- existing runtime fields still render

**Step 2: Run test**

Run:
`python3 -m pytest tests/test_commands.py::test_status_reports_runtime_surface -v`

Expected: either FAIL due to changed output, or PASS after updating expected assertions.

**Step 3: Adjust implementation only if needed**

Ensure backward compatibility:
- existing workspace-state reports still load
- promotion fields still render
- approval gate hints still render

**Step 4: Run test to verify pass**

Run:
`python3 -m pytest tests/test_commands.py::test_status_reports_runtime_surface -v`

Expected: PASS

**Step 5: Commit**

```bash
git add tests/test_commands.py nanobot/runtime/state.py nanobot/cli/commands.py
git commit -m "test: preserve workspace runtime status behavior"
```

---

### Task 5: Add one documentation proof note for the new CLI path

**Objective:** Record how to query live eeepc truth through the new authority boundary.

**Files:**
- Modify: `docs/EEEPC_SELF_EVOLVING_HOST_PROOF_2026-04-15.md`
  or
- Create: `docs/EEEPC_RUNTIME_STATE_AUTHORITY_USAGE.md`

**Step 1: Write the doc update**

Add:
- exact CLI invocation
- when to use `workspace_state`
- when to use `host_control_plane`
- what fields in the output prove shared authority

Example command target:

```bash
nanobot status \
  --runtime-state-source host_control_plane \
  --runtime-state-root /var/lib/eeepc-agent/self-evolving-agent/state
```

**Step 2: Verify doc references match the implemented flags**

Run:
`python3 -m pytest tests/test_commands.py -k status -v`

Expected: PASS

**Step 3: Commit**

```bash
git add docs/...
git commit -m "docs: record runtime state authority usage"
```

---

## Final Verification

Run the focused suite:

```bash
python3 -m pytest \
  tests/test_commands.py \
  tests/test_runtime_coordinator.py \
  tests/test_heartbeat_service.py \
  tests/test_promotion_workflow.py \
  -v
```

Expected:
- existing workspace-state tests still pass
- new authority-boundary tests pass
- status command truthfully reports source and root

## Live Host Verification After Implementation

On a machine that can access live eeepc truth, verify one status call against the host control-plane state root:

```bash
nanobot status \
  --runtime-state-source host_control_plane \
  --runtime-state-root /var/lib/eeepc-agent/self-evolving-agent/state
```

A valid proof should show:
- `Runtime state source: host_control_plane`
- `Runtime state root: /var/lib/eeepc-agent/self-evolving-agent/state`
- a fresh report path
- a runtime status such as `PASS` or `BLOCK`
- approval state from the same authority
- the selected goal ID from the same authority
- any artifact/evidence path from the same authority

## Notes

- Do not change the write path in `nanobot/runtime/coordinator.py` in this slice.
- Do not attempt promotion-path convergence in this slice.
- Do not auto-renew approvals.
- The success condition is truthful shared authority reporting, not full runtime unification.
