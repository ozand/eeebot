# Subagent Bridge — spec

_Status: current. Last updated: 2026-06-25._

## Purpose

The subagent bridge is the LLM-execution arm of the self-evolving runtime. The
coordinator (a lightweight bookkeeper that does not write code) queues bounded
subagent requests under the state root; the bridge picks the oldest queued
request, builds a concrete prompt from the source artifact, and runs the
mandatory local executor model to actually implement the change. Each cycle is
isolated on its own git branch off `origin/main`, gated by an import-smoke check
of only the changed files, and integrated into `main` only when that gate
passes — so a broken or unverified cycle never reaches `main`.

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
  `_run_smoke_tests`, which performs syntax + import checks on ONLY the changed
  `.py` files. It SHALL NOT run the full pytest suite as the gate.
- R11. If no `.py` files changed, the smoke gate SHALL pass (skip). Transient
  errors (timeouts, etc.) SHALL NOT hard-fail the gate.

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
  subagent ran rather than only a blocked stub.

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
- Given a subagent committed a changed `.py` file on `selfevo/cycle-<id>`
- When the import-smoke check of the changed file passes
- Then the bridge merges the cycle HEAD into `main`, pushes `origin/main`, and
  deletes the cycle branch.

### Scenario: failing smoke gate keeps main clean
- Given a subagent commit whose changed `.py` file fails syntax/import
- When the smoke gate fails (after repair attempts are exhausted)
- Then the commits remain on the cycle branch, `main` is not modified, a learning
  artifact is written, and the cycle branch is retained for inspection.

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
  `_cleanup_cycle_branch`). `scripts/eeepc_self_evolving_subagent_bridge.py`
  is a thin wrapper that calls `nanobot.runtime.bridge.cli_main`.
- Related specs: `docs/specs/self-evolving-runtime/spec.md`,
  `docs/specs/host-runtime/spec.md`, `promotion-and-release`, `model-routing`.
