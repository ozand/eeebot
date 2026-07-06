# Subagent Bridge — spec

_Status: current. Last updated: 2026-07-06 (#666: R11a auto-commit safety net
for uncommitted subagent work added; #653: R8-R15 cycle-branch isolation
implemented in code; R10/R11 corrected to describe the full-pytest gate that
was already running)._

## Purpose

The subagent bridge is the LLM-execution arm of the self-evolving runtime. The
coordinator (a lightweight bookkeeper that does not write code) queues bounded
subagent requests under the state root; the bridge picks the oldest queued
request, builds a concrete prompt from the source artifact, and runs the
mandatory local executor model to actually implement the change. Each cycle is
isolated on its own git branch off `origin/main`, gated by the full `pytest
tests/` suite, and integrated into `main` only when that gate passes — so a
broken or unverified cycle never reaches `main`.

> This is **product** runtime behavior. Explanatory detail and host operations
> are in `docs/SYSTEM_OPERATION_REFERENCE.md` §6–§7 (`EEEPC_AGENT_RUNTIME_INSTRUCTIONS.md`
> was folded there and removed 2026-07-05, #613; recoverable from git history).
> Where docs and the running
> code disagree, `nanobot/runtime/bridge.py` is authoritative (moved from
> `scripts/eeepc_self_evolving_subagent_bridge.py` in #599; that path is now a
> thin wrapper kept for the unchanged systemd/deploy contract).

## Requirements

### Request selection
- R1. The bridge SHALL select the oldest queued/pending request under
  `state/subagents/requests/` whose status is `queued` or `pending` and that has
  not already been handled by a real executor run.
- R2. The bridge SHALL treat coordinator-produced blocked stubs as NOT handled
  (`_is_real_result`): a result with `result_status`/`status` = `blocked`,
  `terminal_reason` = `local_executor_unavailable`, `materialized_from` =
  `queued_request_terminalizer`, or `blocker.reason` = `local_executor_unavailable`
  SHALL NOT mark its request as handled, leaving it eligible for the bridge.
- R3. The bridge SHALL record its own completion with a `handled_<request_id>.txt`
  marker and SHALL skip a request whose marker already exists (idempotent — no
  duplicate execution of the same request).

### Executor model
- R4. The bridge SHALL run the bounded subagent on the mandatory local executor
  model `un/qwen3.6-27b-mtp` (logical alias `gpt-5.3-codex`), configured
  through `SUBAGENT_BRIDGE_MODEL` / `config.tools.subagent.model`, calling the
  LiteLLM proxy directly. The executor model SHALL NOT be swapped for a
  remote/coordinator model.
  - History: through #637 the model was routed through an external `pi`
    binary profile (provider name `local_pi_cli`, historical alias
    `hermes_pi_qwen`) with `--no-tools`, which is functionally a single
    LiteLLM call. #641 removed that external-binary profile entirely — the
    runtime now has exactly one built-in executor path (this bridge /
    `queued_request_terminalizer`), with no dependency on `/usr/local/bin/pi`
    or any subprocess shell-out. Historical state artifacts that still carry
    the old provider names are never rewritten (migration spec R7).
- R5. `NANOBOT_SUBAGENT_EXECUTOR_COMMAND` SHALL NOT be set in `agent.service`.
  If set, the coordinator's in-process materializer runs a deterministic,
  no-LLM `bounded_subagent_executor` and writes a `completed` result before the
  bridge can claim the request — defeating real LLM execution.

### Executor autonomy contract

The local executor's system/developer instructions (formerly kept in two
standalone `docs/HERMES_AUTONOMY_*.md` files, folded here and removed
2026-07-05, #637) encode a short completion-discipline contract so a bounded
executor run does not stop early or hand off instead of acting:

- Every progress/status reply names the current time (from a tool), what is
  being done now, and — if work was delegated — what was delegated.
- The executor does not end a turn on a summary or handoff sentence
  (`"next I will"`, `"if you want"`, etc.) while an open bounded issue with no
  blocker remains; it moves to the next open issue in the same run instead.
- Every claimed action must have actually been performed in the same
  response/session — no reporting hypothetical future work as done.
- On a failed bounded attempt, the executor repairs or rolls back to a green
  baseline rather than leaving half-broken state while claiming progress.
- GitHub Issues remain the source of task truth; lifecycle state and
  rollout/proof links are updated on the issue when work advances.

### Prompt construction
- R6. `build_task` SHALL inline the content of the request's `source_artifact`
  (the materialized-improvement JSON) directly into the subagent prompt
  (truncated to ~4000 chars), so the subagent has concrete data and does not
  hunt the workspace for context.
- R7. When the cycle is isolated on a branch, the prompt SHALL include a
  mandatory branch-discipline addendum instructing the subagent to commit on the
  current branch and to NOT run `git checkout`/`switch`/`branch` or `git push`.

### Cycle isolation
- R8. Before spawning, the bridge SHALL isolate the cycle on a fresh branch
  `selfevo/cycle-<id>` created with `git checkout -B <branch> origin/main`
  (clean base + rollback) in the working repo `eeebot-self-evolving`.
- R9. If branch setup fails, the bridge SHALL fall back to the current branch and
  continue — branch-setup failure SHALL NOT break the loop.

### Smoke gate
- R10. After the subagent commits, the bridge SHALL gate the cycle with
  `_run_smoke_tests`, which runs the FULL `pytest tests/` suite via the runtime's
  own interpreter (`sys.executable -m pytest -x -q --tb=native`, 300s timeout)
  inside the isolated checkout. This is intentionally not an import-only check
  of changed files — an updated 2026-07 revision of this requirement; see
  "History" below. (#668: the bare system `python3` lacks runtime dependencies
  and `--tb=short`/60s produced spurious INTERNALERROR/timeout failures on the
  eeepc host; `sys.executable` + `--tb=native` + 300s reflect the environment
  and runtime the gate must actually exercise.)
- R11. If no commits landed on the cycle branch, the smoke gate SHALL be
  skipped entirely (nothing to test) and the cycle branch SHALL be discarded
  without touching `main`. Transient errors (timeouts, missing `pytest`, no
  `tests/` directory) SHALL NOT hard-fail the gate — `_run_smoke_tests` returns
  a pass with an explanatory message in those cases.
  - History: an earlier draft of this requirement (through #653) described an
    import-only syntax check of only the changed `.py` files. That was never
    implemented; `_run_smoke_tests` has always run the full suite. #653
    corrected the requirement text to match the running code (CLAUDE.md
    "executable truth wins") rather than implementing the cheaper import-only
    check, since the full suite is a strictly stronger gate.
- R11a. If no commits landed on the cycle branch but the working tree is dirty
  (`git status --porcelain` non-empty), the bridge SHALL commit those changes
  itself (`_auto_commit_uncommitted_work`) before applying R11 — excluding any
  file matching the same `_BLOCKED_FILE_PATTERNS` used by
  `_validate_mutation_surfaces` (logged, never staged) — then recount commits
  and proceed through the normal smoke gate / R12-R15 flow unchanged. Found
  live during #656 verification (2026-07-06): a subagent implemented real
  changes via `edit_file` but ended its turn without running `git commit`;
  because `cycle_commit_count` stayed `0`, the gate was skipped and the
  `finally`-block restore-to-main discarded the work outright, so every
  following cycle re-did (and re-lost) the same task. This is a bridge-level
  safety net, not a prompt-only fix — R7's branch-discipline prompt also
  reinforces "commit is the final step" so the gap is rarer, but the bridge
  no longer relies on the subagent remembering to commit (#666).

### Integration to main
- R12. The bridge SHALL integrate the cycle branch into `main` (merge `--no-ff`
  the subagent's HEAD onto a fresh `origin/main`, then push) ONLY when the smoke
  gate passes.
- R13. When the smoke gate fails, the bridge SHALL leave the commits on the cycle
  branch, SHALL keep `main` clean (no merge/push), and SHALL record a learning
  artifact; the cycle branch SHALL be kept for inspection.
- R14. A merge conflict or push failure during integration SHALL abort cleanly,
  keep the cycle branch, and SHALL NOT corrupt `main`.
- R15. The bridge SHALL delete the cycle branch only after it has been integrated
  into `main`; on non-integration it SHALL return to `main` and keep the branch.

### Result evidence
- R16. After each run the bridge SHALL write a real `bridge_llm_execution` result
  to `state/subagents/results/` (with `commits_pushed`, `files_changed`,
  `backlog_title`, `result_status`) so the coordinator can observe that a real
  subagent ran rather than only a blocked stub. The result SHALL also carry a
  `rollback` record — `{"integrated": bool, "cycle_branch": str,
  "main_sha_before": str, "main_sha_after": str, "reason": str | None,
  "auto_committed": bool}` — so integration/non-integration is git-verifiable
  from the artifact alone: `main_sha_before == main_sha_after` whenever
  `integrated` is `false`. `auto_committed` is `true` when R11a fired for this
  cycle (#666). `commits_pushed` counts only commits that reached
  `origin/main` (i.e. it is `0` whenever `integrated` is `false`, even if the
  subagent committed on the cycle branch) — this is the one semantic change
  from the pre-#653 field, which counted any subagent commit regardless of
  whether it survived the gate.

### Tool harness — phase 1 (read-only tools, #643)
- R17. `nanobot.runtime.subagent_materializer.materialize_subagent_requests`
  SHALL run the in-process phase-1 tool harness
  (`nanobot.runtime.tool_harness`) only when a request's `profile` field is
  exactly `tool_harness`; every other profile SHALL take the pre-existing
  path (configured external executor, or the blocked-stub
  `queued_request_terminalizer` path) completely unaffected.
- R18. The phase-1 tool set SHALL be exactly `read`, `grep`, `ls` —
  read-only, no mutation, no command execution. `edit`/`write` (phase 2) and
  command execution (phase 3) remain gated per
  `docs/changes/643-subagent-tool-harness/design.md`.
- R19. Every tool call SHALL resolve its path argument
  (`Path.resolve()`, following symlinks) and verify the resolved path is a
  descendant of the workspace root *before* any I/O. An escape (`..`,
  absolute path outside root, symlink pointing outside) SHALL be vetoed —
  the model SHALL see a tool-result string explaining the veto, and the loop
  SHALL continue; the harness SHALL NOT crash or raise on an escape attempt.
- R20. `read` and `grep` output SHALL be truncated by one shared,
  deterministic head-tail truncation function (2000 lines / 50KB defaults)
  whose `{truncated, total_lines, total_bytes}` metadata is surfaced to the
  model in the tool result, never silently dropped.
- R21. No tool call SHALL raise an exception into the turn loop. Bad paths,
  invalid regexes, missing files, and vetoes SHALL all become normal
  tool-result text the model sees on its next turn.
- R22. Exactly one veto hook (`before_tool_call`) SHALL sit between "model
  requested a tool call" and "tool call executes", checking (a) the harness's
  own tool-call budget and (b) path confinement. Tools themselves SHALL stay
  policy-free.
- R23. The harness loop SHALL NOT invent a second budget/stop-reason system:
  it SHALL record one of the stop reasons already enumerated by
  `nanobot.runtime.stop_guards` (`gate_clean` when the model stops calling
  tools on its own, `max_iterations`, or `budget_tool_calls`) using the caps
  `SubagentToolConfig.harness_max_iterations` (default 8) and
  `harness_max_tool_calls` (default 24). Exception: an LLM-call failure is not
  a cycle-stall concern `stop_guards` models, so it is the one harness-local
  stop reason, `llm_error` (`nanobot.runtime.tool_harness.STOP_REASON_LLM_ERROR`)
  — set when `chat_with_retry` exhausts retries and returns
  `finish_reason="error"` instead of raising. The loop SHALL break
  immediately in that case (`run_tool_harness_request` returns `ok=False`,
  and `subagent_materializer._run_tool_harness` maps it to
  `failure_reason="tool_harness_llm_error"`, distinct from
  `tool_harness_incomplete`) rather than reporting the failed run as
  `gate_clean`/`completed` (found live during #643 phase-1 verification: an
  `un/qwen` model-group outage was silently recorded as a completed run).
- R24. Every tool call (request, allow/veto decision, result byte size,
  truncation flag) SHALL be appended to a per-request JSONL sidecar at
  `state/subagents/tool_calls/<request_id>.jsonl`. The result JSON SHALL
  carry only the bounded summary fields `tool_calls_count`,
  `tool_call_journal` (path to the sidecar), and `stop_reason` — full detail
  lives in the sidecar, not inlined into the result artifact.
- R25. The harness workspace root SHALL be the cycle's existing isolated
  checkout (`state_root.parent / "eeebot-self-evolving"`, the same
  convention `nanobot/runtime/bridge.py` already uses), overridable
  per-request via a `workspace_root` field for tests. The harness SHALL NOT
  touch `_setup_cycle_branch`, the smoke gate, or `_integrate_cycle_to_main`
  — those remain the bridge's exclusive authority.

> **Journald timestamp gotcha (#620):** under systemd, stdout/stderr are a pipe
> to the journal, and Python fully-buffers a piped stream by default. During a
> 2026-07-04 token-rotation incident, a stale `auto-push` print line was
> journaled minutes after the event it described, which sent the investigation
> down a wrong path. The bridge now calls
> `sys.stdout.reconfigure(line_buffering=True)` / same for stderr at process
> start (`cli_main`), so journal timestamps are trustworthy going forward.

## Scenarios

### Scenario: blocked stub does not suppress a real run
- Given a queued request whose only result is a coordinator blocked stub
  (`terminal_reason = local_executor_unavailable`)
- When the bridge selects a pending request
- Then `_is_real_result` rejects the stub and the bridge picks up that request
  for real LLM execution.

### Scenario: passing smoke gate integrates to main
- Given a subagent committed on `selfevo/cycle-<id>`
- When the full `pytest tests/` gate passes
- Then the bridge merges the cycle HEAD into `main`, pushes `origin/main`, and
  deletes the cycle branch.

### Scenario: failing smoke gate keeps main clean
- Given a subagent commit that breaks the test suite
- When the smoke gate fails (after repair attempts are exhausted)
- Then the commits remain on the cycle branch, `main` is not modified (verified
  by `origin/main`'s SHA before/after the cycle), a `result_status="blocked"`
  artifact records `rollback.integrated=false`, and the cycle branch is
  retained for inspection.

### Scenario: uncommitted subagent work is auto-committed before the gate
- Given a subagent edited files on `selfevo/cycle-<id>` via `edit_file`/`write_file`
  but ended its turn without running `git commit` (dirty tree, `cycle_commit_count == 0`)
- When the bridge checks for new commits after the subagent run
- Then `_auto_commit_uncommitted_work` commits the dirty changes (excluding any
  `_BLOCKED_FILE_PATTERNS` match) as `selfevo: auto-commit uncommitted subagent
  work — <title>`, the commit count is re-derived, and the normal smoke
  gate/integration flow (R10-R15) proceeds exactly as if the subagent had
  committed itself.

### Scenario: idempotent re-run
- Given a request already has a `handled_<id>.txt` marker
- When the bridge runs again
- Then it prints `already_handled` and does not re-spawn the subagent.

## References

- Reference docs: `docs/SYSTEM_OPERATION_REFERENCE.md` §6 (subagent bridge) and
  §7 (models/topology); `EEEPC_AGENT_RUNTIME_INSTRUCTIONS.md`
  ("Subagent bridge — architecture and troubleshooting") was folded there and
  removed 2026-07-05 (#613; recoverable from git history). The executor
  autonomy contract above was folded from `docs/HERMES_AUTONOMY_CHECKLIST.md`
  and `docs/HERMES_AUTONOMY_INSTRUCTION_SNIPPET.md`, removed 2026-07-05
  (#637; recoverable from git history).
- Code (authoritative): `nanobot/runtime/bridge.py`
  (`main`, `find_pending_request`, `_is_real_result`, `build_task`,
  `_setup_cycle_branch`, `_run_smoke_tests`, `_integrate_cycle_to_main`,
  `_cleanup_cycle_branch`, `_auto_commit_uncommitted_work`).
  `scripts/eeepc_self_evolving_subagent_bridge.py`
  is a thin wrapper that calls `nanobot.runtime.bridge.cli_main`.
- Related specs: `docs/specs/self-evolving-runtime/spec.md`,
  `docs/specs/host-runtime/spec.md`, `promotion-and-release`, `model-routing`.
