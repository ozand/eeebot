# 599 — Move the subagent bridge into the package

story_id: 599

## Why

The executor bridge existed as two independently editable implementations
with active drift:

- `scripts/eeepc_self_evolving_subagent_bridge.py` — 1,484 LOC, the canonical
  code, deployed by **file copy** to `/usr/local/libexec/`
  (`deploy_release.sh`), and imported six things from `nanobot.*` while
  living outside the package (no import guarantees, dodges the venv).
- `host/eeepc/libexec/eeepc-self-evolving-subagent-bridge.py` — a 314-LOC
  stale stub (no `stop_guards` import, different default model) that
  `install.sh` installed on a *fresh* install. A fresh install without an
  immediate follow-up deploy shipped the wrong bridge (1,224 diff lines
  between the two files).

This change eliminates the second implementation and puts the canonical one
where the rest of the runtime lives: `nanobot/runtime/`.

## What moves

- `scripts/eeepc_self_evolving_subagent_bridge.py` (1,484 LOC) → moved
  verbatim (behavior-preserving) to `nanobot/runtime/bridge.py` (1,498 LOC
  after the module docstring/`cli_main()` addition). No logic was split out —
  the file stays under the ~1,500 LOC threshold that would have triggered a
  `bridge_git.py` split.
- `scripts/eeepc_self_evolving_subagent_bridge.py` becomes a 10-line wrapper:
  `from nanobot.runtime.bridge import cli_main; raise SystemExit(cli_main())`.
  `main()` stayed `async def` (unchanged); a new synchronous `cli_main()` was
  added purely so the wrapper (and the systemd `ExecStart`, unchanged) can
  call one thing without inlining `asyncio.run(...)`.
- `host/eeepc/libexec/eeepc-self-evolving-subagent-bridge.py` (the stale
  314-LOC stub) is **deleted**.
- `host/eeepc/scripts/install.sh`'s `install_libexec()` now also installs the
  wrapper from `scripts/` to `/usr/local/libexec/eeepc-self-evolving-subagent-bridge.py`,
  so a fresh install and a later `deploy_release.sh` run always agree (deploy
  already overwrote the stub with the `scripts/` version — see
  `lessons/errors/ERR-2026-06-28-001` — this closes the gap for installs that
  never get a follow-up deploy).

## What stays unchanged (deliberately — this PR is behavior-preserving)

- The systemd unit `eeepc-self-evolving-subagent-bridge.service` still execs
  `/usr/local/libexec/eeepc-self-evolving-subagent-bridge.py` — unchanged.
- `deploy_release.sh` still copies `scripts/eeepc_self_evolving_subagent_bridge.py`
  (now the wrapper) to that libexec path — unchanged.
- The separate `pinned/current` `PYTHONPATH` symlink the bridge runs under is
  untouched.
- Retiring the file-copy deploy mechanism entirely in favour of a
  console-script/`python -m nanobot.runtime.bridge` entrypoint is **out of
  scope** here — tracked as issue #601.

## Dedupe decisions (git ops / done-detection reuse)

The bridge's own git-commit orchestration (multiple path-specific
`git add <path> && git commit -m "<specific message>"` calls for MEMORY.md
moves, lesson recording, and archiver commits, plus repeated
`git push origin main`) was **kept as-is**, not routed through
`nanobot/runtime/github_ops.commit_and_push_self_evolution`. That helper
commits with `git add -u` (all tracked changes) under one message and pushes
`HEAD:<current-branch>` — semantically different from the bridge's
targeted, per-purpose commits to a hardcoded `origin main`. Replacing them
would risk silently commit-bundling unrelated tracked changes. Per the task
brief ("only where semantics are demonstrably identical"), this was left
alone and is noted here instead of guessed at.

The bridge already called `nanobot.runtime.stop_guards.revision_outcome` for
its smoke-gate repair-loop bookkeeping (wired in #557, prior to this move) —
no further dedupe was needed there. The bridge's own `_task_already_done()`
(git-log keyword scan for "was this backlog item already implemented")
is a distinct concern from `stop_guards`' stall/no-progress detection and was
left untouched.

## Blueprint bugs (`blueprint/bridge-feedback-loop-fix/`) — status: already fixed

The blueprint documented three structural bugs. All three were **already
fixed** in the code prior to this PR (present in
`scripts/eeepc_self_evolving_subagent_bridge.py` at the time of the move):

1. **Bug 1** — `_get_previous_attempts()` matching only on the generic
   `summary` string (so `## Previous attempts` was never injected). Already
   fixed: it now primarily matches by reading `source_artifact` →
   `next_bounded_candidate.title` from the result file, falling back to
   `summary` keyword matching, then `cycle_id`.
2. **Bug 2** — `commits_pushed` computed as `origin/main..HEAD` *after*
   execution (reported 0 when the subagent pushed itself). Already fixed via
   `_capture_pre_spawn_sha()` / `_count_commits_since()`, which counts commits
   relative to a SHA captured before spawn.
3. **Bug 3** — no durable `backlog_title` anchor in result JSON. Already
   fixed: `_write_bridge_completed_result()` writes `backlog_title`, and
   `_migrate_backlog_title_in_results()` backfills historical result files.

This PR adds regression tests locking in that fixed behavior in the new
location (`tests/test_commits_pushed.py`, `tests/test_lessons_feedback_loop.py`
— both updated to read `nanobot/runtime/bridge.py` instead of the old script
path; no new bug fixes were required).

The Priority-18 scorer-weight-loading item from the same blueprint
(`nanobot/runtime/scorer.py` frozen-weights override) is **not** part of this
PR — it is unrelated to the bridge-into-package move and belongs to a
separate task.

## Risks

- Any external tooling on the eeepc host that greps for
  `scripts/eeepc_self_evolving_subagent_bridge.py` by content (not just path)
  will now see a 10-line wrapper instead of the full implementation — expected,
  documented in this proposal and in the module docstrings on both sides.
- `install.sh` fresh-install path changed (now installs from `scripts/`
  instead of the deleted `host/eeepc/libexec/` stub) — this is the intended
  fix for the drift bug, not a regression risk, but fresh installs were not
  live-tested as part of this PR (the standing workflow verifies via
  `deploy_release.sh` against the running host, not `install.sh`).

## Acceptance

- One bridge implementation (`nanobot/runtime/bridge.py`), covered by the
  existing AST-extraction unit tests (now pointed at the new path) plus the
  blueprint regression tests.
- `scripts/eeepc_self_evolving_subagent_bridge.py` is a thin wrapper; smoke
  test (`PYTHONPATH=. python scripts/eeepc_self_evolving_subagent_bridge.py`
  import-only) passes.
- Full test suite green.
- `blueprint/bridge-feedback-loop-fix/` deleted (content folded in above).
- Deploy to eeepc + live verification of one full bridge cycle is a
  **follow-up** step per the standing eeebot fix workflow (merge needs
  explicit operator OK, then `deploy_release.sh`, then live verify) — not
  done as part of authoring this PR.
