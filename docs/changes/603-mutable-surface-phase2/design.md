# 603 + 643 phase 2 — design: mutable surface, protect-list, enforced rollback

## Scope note

This inherits `docs/changes/643-subagent-tool-harness/design.md` verbatim for
anything not called out below: the truncation module, the pluggable
`Operations` interface, "errors as tool-result content", the single
`before_tool_call` veto seam, the turn-loop shape, and the phase-1
`read`/`grep`/`ls` contracts and workspace confinement
(`WorkspaceOperations.resolve`) are unchanged. This document specifies only
what phase 2 adds: `edit`/`write`, the `mutable_surface` bound, the
protect-list, and the rollback gap-fill #603 requires before mutation ships.

## Edit/write tool contracts (deltas from 643 design.md)

- **`edit`**: `{path, edits: [{oldText, newText}]}` — exactly as specced in
  643 design.md's "Edit tool" section (exact/unique/non-overlapping
  `oldText` per edit, batch-validated against the file as read at apply
  time, validation failures are tool-result text not exceptions, unified
  diff produced as an audit byproduct). No changes.
- **`write`**: `{path, content}` — creates a new file, or overwrites an
  existing one (full-file replace). 643 design.md left this implicit; this
  is the delta. The audit diff (empty-old for a genuinely new file, else
  old-vs-new) makes an accidental overwrite reviewable either way, so a
  separate "create-only" mode isn't needed.
- Both go through `before_tool_call` exactly like `read`/`grep`/`ls`
  (`_PATH_ARG_BY_TOOL` gains `edit`/`write`); the only new veto conditions
  are the protect-list and mutable-surface checks below — path confinement
  (R19) is unchanged and runs first.
- `edit`/`write` schemas are only added to a request's tool list when its
  profile enables mutation (below) — a read-only `tool_harness` request
  never sees them, so a model can't attempt them at all. Defense in depth
  on top of the veto hook.

## Profile: `tool_harness_mutate`

Phase 1's `tool_harness` profile stays byte-identical — no behavior change
for it or any other existing profile. Mutation is a new, separate profile
value, `tool_harness_mutate`, dispatched in
`subagent_materializer.materialize_subagent_requests` alongside the
existing `tool_harness` branch. Only it gets `edit`/`write` schemas and is
subject to everything below.

### Mutable-surface declaration

```json
"mutable_surface": ["nanobot/skills/", "docs/changes/"]
```

- POSIX-style, workspace-root-relative prefixes, no leading `/`, no `..`
  segments (rejected at declaration time).
- Matching is **path-segment-aware**, not raw `startswith`: `"docs/specs/"`
  must not match `"docs/specsx/file.py"`. Use
  `PurePosixPath(resolved_rel).is_relative_to(PurePosixPath(prefix.rstrip("/")))`
  per declared prefix — a naive string-prefix check is exactly the near-miss
  bug the test plan below targets.
- Prefixes are matched against the tool call's **resolved**,
  workspace-relative path (post `Path.resolve()`, post symlink-following),
  never the raw string the model passed.

### Missing-declaration semantics (decision)

**A `tool_harness_mutate` request with an empty/missing `mutable_surface` is
rejected up front — the harness loop never starts, no LLM call is made.**
`subagent_materializer`'s mutate-profile path returns a terminal failure
immediately (`failure_reason="tool_harness_missing_mutable_surface"`), same
shape as today's blocked-stub results.

Rejected alternative: start the loop, veto every edit/write call. That would
let the model burn its full iteration/tool-call budget on calls that can
never succeed, look identical to a stuck run in the journal, and offer no
safety benefit — the request is definitionally malformed (mutation profile,
no bound declared), the same category as a missing `source_artifact`.
Fail-closed-and-fast is simpler (one check, not a steady-state veto pattern)
and safer (zero LLM calls, zero budget spent, zero mutation attempted).

## Protect-list

A hardcoded tuple in `nanobot/runtime/tool_harness.py` — code, not config:

```python
_PROTECTED_PREFIXES = (
    "nanobot/runtime/tool_harness.py",
    "nanobot/runtime/stop_guards.py",
    "nanobot/runtime/bridge.py",
    "nanobot/runtime/subagent_materializer.py",
    "docs/specs/",
    ".github/workflows/",
    "pyproject.toml",
    "CONSTITUTION.md",
    "AGENTS.md",
    "SECURITY.md",
    ".git/",
)
```

Rationale: the harness's own veto seam and budget authority (`tool_harness.py`,
`stop_guards.py`) must not rewrite the rules bounding them; the bridge and
materializer are the dispatch/promotion authority (mutation must not change
how it is itself invoked or gated); `docs/specs/` is current product truth
(CONSTITUTION.md principle 3) — a subagent changing "what is true now" out
from under human review defeats the model; CI/`pyproject.toml` are the
test/lint/packaging contract the gate and review rely on;
`CONSTITUTION.md`/`AGENTS.md`/`SECURITY.md` are the guardrails this design
serves; `.git/` blocks history/config tampering.

Also reused (imported, not re-typed, so the two enforcement points can't
drift): `bridge.py::_BLOCKED_FILE_PATTERNS` (`.env`, `secret`, `credential`,
`token`, `private_key`, `id_rsa`, `.npmrc`, lockfiles).

### Precedence and evaluation order

`before_tool_call` for `edit`/`write`, in order, short-circuit on first veto:

1. Tool-call budget (existing, R22/R23 — unchanged).
2. Workspace-root confinement (existing, R19 — unchanged).
3. **Protect-list** (new): resolved path matches `_PROTECTED_PREFIXES` or
   `_BLOCKED_FILE_PATTERNS` → veto unconditionally, without consulting
   `mutable_surface` — a request cannot self-declare past the fixed harness.
4. **Mutable-surface membership** (new, `edit`/`write` only): resolved path
   must descend from a declared `mutable_surface` prefix → veto if not.
   `read`/`grep`/`ls` skip this — phase 2 doesn't narrow read visibility.

Net rule: protect-list beats mutable-surface beats workspace-root.

## Concurrency / consistency

- **Coordinator vs. model edits**: the coordinator does not edit code
  (`ALLOW_CODE_EDITS=false`, R9) and doesn't touch the subagent's checkout
  during a run — nothing to race against. Stated for completeness.
- **Concurrent external modification of a file mid-edit**: already covered
  by the inherited `edit` contract (validates `oldText` against the file as
  read at apply time) — a change landing between two tool calls in the same
  run is caught as a normal validation failure.
- **Two `tool_harness_mutate` requests against the same workspace at once**:
  not newly introduced — the bridge already processes one request at a time
  against the single shared checkout (idempotent `handled_<id>.txt`
  markers, R3). Phase 2 adds a branch switch (below) inside that same
  single-request invocation; no new concurrency surface.

## Enforced rollback — grounded in actual current behavior

**Verified by reading `nanobot/runtime/bridge.py`** (not the aspirational
R8-R9/R12-R15 in `docs/specs/subagent-bridge/spec.md`):

- No cycle-branch isolation exists. No `_setup_cycle_branch`,
  `_integrate_cycle_to_main`, or `_cleanup_cycle_branch` function is in the
  codebase. The subagent commits directly against whatever branch the
  shared `eeebot-self-evolving` checkout is on (in practice `main`).
- The bridge auto-pushes those commits to `origin/main` **before** the
  smoke gate runs.
- `_run_smoke_tests` runs the full `pytest tests/` suite, not an
  import-only check of changed files (contra R10).
- On smoke failure, up to `REVISION_CAP_DEFAULT` (3) repair subagents run,
  each auto-pushed to `origin/main` immediately on commit — before that
  turn's own re-check.
- If the cap is reached without a pass, `revision_outcome()` returns
  `outcome="blocked"` and the result JSON gets `result_status="blocked"`.
  **No git rollback occurs** — the failing commits are already live on
  `origin/main`. "Blocked" is a narrative field, not an enforcement action.

This is the exact gap #603 names. Shipping `edit`/`write` on top of it would
make mutation real without the accept/rollback authority #603 requires being
real. The fix below is scoped to the `tool_harness_mutate` path only — it
does not refactor the pre-existing direct-`SubagentManager` path (out of
scope, see proposal.md).

### Design: gate-before-push for `tool_harness_mutate`

For a `tool_harness_mutate` request, the wrapper around
`run_tool_harness_request`:

1. **Isolate**: `git checkout -B selfevo/cycle-<cycle_id> origin/main` in the
   shared checkout — R8's literal intent, actually implemented, scoped to
   this profile.
2. **Run the harness loop**: `edit`/`write` calls mutate files on disk in
   this checkout, confined and surface-bounded as above. The model has no
   git/commit tool in phase 2 (that's bash, phase 3) — the harness never
   commits itself.
3. **Commit** (wrapper, not model): if the tree changed, stage only paths
   that are simultaneously inside the workspace root, inside a declared
   `mutable_surface` prefix, and not protect-list-matched (defense in depth:
   `git add` is filtered independently of the tool-call vetoes). Any
   changed path failing that filter is treated as an immediate gate failure
   rather than committed. Commit message references the request id.
4. **Gate**: import-only check on the changed `.py` files (`py_compile`, no
   subprocess, no full `pytest`) — honors R10's stated intent even though
   the pre-existing `_run_smoke_tests` does not; reconciling that existing
   divergence for the non-harness path is a separate follow-up.
5. **Accept or roll back**:
   - **Pass**: merge `selfevo/cycle-<cycle_id>` into fresh `origin/main`
     (`--no-ff`), push, `git checkout main`, delete the cycle branch — R12/R15's
     stated intent, now real.
   - **Fail**: do not merge, do not push. `git checkout main` so the shared
     checkout returns to what every other code path assumes. **Keep** the
     `selfevo/cycle-<id>` ref (don't delete) — matches R13/R15 ("kept for
     inspection... deleted only after integration") and CONSTITUTION.md
     principle 8 (evidence over claims): destroying the branch would destroy
     the only record of the failed attempt. "Enforced rollback" means
     `origin/main` is provably untouched and the working tree is provably
     restored — not that the attempt's evidence is erased.
   - One gate check per request; no in-harness repair-retry loop in phase 2
     (unlike the direct-bridge path) — deferred, see Open questions.
6. Runs regardless of outcome (`try/finally`-shaped): the shared checkout
   must never be left on a cycle branch.

Net effect: for the mutation profile, `origin/main` is only ever touched by
a merge that already passed the gate — today's "push, then hope" ordering
does not exist in this path.

## Result-artifact / journal extensions

- Per-call journal (`state/subagents/tool_calls/<request_id>.jsonl`, R24,
  unchanged shape): `edit`/`write` entries add a `"diff"` field — a unified
  diff truncated through the same `truncate_text()` used for `read`/`grep`
  (smaller caps: 200 lines / 20KB), same `{truncated, total_lines,
  total_bytes}` metadata.
- Result JSON gains, only for `tool_harness_mutate`:
  - `mutation_enabled: true`
  - `mutable_surface`: echo of the declared list (audit trail)
  - `commit_sha`: cycle-branch commit sha, or `null`
  - `smoke_gate`: same shape as `stop_guards.revision_outcome()` (`gate`,
    `count`, `max`, `smoke_passed`, `capped`, `outcome`) — reused verbatim;
    `count`/`max` are `1`/`1` (no repair-retry loop in phase 2)
  - `rollback`: `{"performed": bool, "reason": "gate_failed" | null}`
- Existing `tool_calls_count`/`tool_call_journal`/`stop_reason` (R24, turn-
  loop level) are unchanged and orthogonal to the new cycle-level fields —
  the turn loop can finish `gate_clean` while the cycle-level gate still
  fails (edit succeeded; the edit was syntactically wrong).

## Test plan (adversarial)

- **Surface escape, literal path**: `edit`/`write` inside workspace-root but
  outside every declared `mutable_surface` → vetoed, tool-result names the
  reason, loop continues.
- **Surface escape via symlink**: a symlink inside a declared surface whose
  target resolves outside it (or onto a protect-list path) → veto fires on
  the resolved path, before the surface check runs.
- **Protect-list bypass via near-miss prefix**: write to
  `docs/specsx/notes.md` (surface: `docs/`) must not hit the `docs/specs/`
  protect-list entry; conversely `docs/specs/foo.md` must be vetoed even
  under a broader declared `docs/` surface — exercises the segment-aware
  matcher, not naive `startswith`.
- **Protect-list bypass via `..`-escape**: e.g.
  `nanobot/skills/../../nanobot/runtime/tool_harness.py` with
  `mutable_surface = ["nanobot/skills/"]` → caught by `Path.resolve()` +
  `relative_to` (R19) before the protect-list check needs to fire; test both
  layers independently.
- **Missing `mutable_surface`**: zero provider/LLM calls made; terminal
  failure result written immediately with the documented failure_reason.
- **Concurrent external modification**: modify a file externally between
  two `edit` calls targeting it → second call's validation fails as a
  normal tool-result, not a crash.
- **Rollback on failed gate (integration)**: fixture repo; edit produces a
  syntax error inside a valid surface; wrapper commits to
  `selfevo/cycle-<id>`; gate fails. Assert: `origin/main` SHA unchanged;
  shared checkout `HEAD` back on `main`; cycle branch still exists locally
  with the failing commit; result JSON `rollback.performed == True` and
  `smoke_gate.outcome == "blocked"`.
- **Gate passes (control case)**: same fixture, valid edit → `origin/main`
  advances via merge commit; cycle branch deleted;
  `rollback.performed == False`.

## Acceptance criteria

- All phase 1 acceptance criteria (R17-R25) hold unchanged for `tool_harness`.
- `edit`/`write` land only inside workspace-root ∩ declared
  `mutable_surface`, never on a protect-list path.
- A `tool_harness_mutate` request with no declared `mutable_surface` never
  invokes the model.
- A failed post-edit gate never advances `origin/main` and always leaves
  the shared checkout back on `main`.
- Every successful `edit`/`write` produces a journaled unified diff.
- The adversarial test plan above passes, including both escape variants
  against the protect-list.
- `docs/specs/subagent-bridge/spec.md` R8-R15 are corrected in the same PR:
  implemented to match (this design), or the spec text is fixed to describe
  verified behavior for paths this design doesn't touch (CONSTITUTION.md
  principle 4).

## Open questions

- Should the direct-`SubagentManager` bridge path get the same
  gate-before-push fix, or keep its current push-before-gate behavior
  indefinitely as a materially larger, separately-scoped refactor? Left
  open; likely its own follow-up Issue.
- Should phase 2 eventually get a bounded repair-retry loop (capped by
  `REVISION_CAP_DEFAULT`, matching the direct-bridge path)? Deliberately
  deferred in favor of one-shot-then-rollback; revisit once real
  `tool_harness_mutate` usage data exists.
- Exact import-only gate mechanism (`py_compile` vs. `ast.parse` vs.
  `compileall`) is left to implementation — all are subprocess-free; whichever
  is chosen should match R10's stated intent even though `_run_smoke_tests`
  today does not.
- Should `mutable_surface` prefixes be checked against the protect-list at
  declaration time (reject a request whose surface is entirely swallowed by
  a protect-list prefix) versus relying solely on the runtime veto? Left as
  a nice-to-have — the runtime veto already provides the safety property on
  its own.
