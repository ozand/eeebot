# Avoiding Repeat Failures

## Description
This document outlines the patterns, root causes, and actionable mitigation strategies for repeat failures in the `eeebot` self-evolving runtime. The target repeat failure rate is **0.3**, but historical rates have exceeded this target (e.g., reaching 1.77). By understanding these patterns, subagents and operators can avoid repeating errors in future cycles.

## Root Causes
Repeat failures are typically caused by duplicate script proposals, incorrect surface paths, git push rejections, and systemd service failures.

## Recovery Procedures
To recover from repeat failure loops, clear the duplicate suppression state, rebase local commits, or prune the failed backlog.

## Prevention Mechanisms
We prevent repeat failures by following correct archival procedures, verifying changes before committing, and targeting mutation surfaces.

---

## 1. Duplicate Script Proposals
### Symptom
A subagent proposes a task to modify, archive, or create a script, but the task is repeatedly skipped or fails with a rollback reason like `recent_duplicate_failure` or `skipped-duplicate`.

### Root Cause
1. **Failure Suppression**: The coordinator's task selector suppresses tasks that match recently-failed or rejected proposals within a 24-hour window.
2. **Unresolved Decay**: If an initial task fails (e.g., due to syntax errors, missing imports, or test failures), subsequent attempts to address the same underlying issue are blocked by the duplicate check, leading to a loop of skipped tasks.
3. **Backlog Seeding Loops**: The backlog seeder continues to propose the same task because the underlying decay or issue is not resolved.

### Mitigation Strategies
* **Correct Archival Procedure**: When archiving a script, do not delete the file. Prepend a deprecation notice to the docstring, add a warning on execution, and exit early (e.g., `sys.exit(0)` or raise `RuntimeError`).
* **Pre-Commit Verification**: Always run a syntax check and smoke test on the modified script before committing:
  ```bash
  python3 -m py_compile scripts/your_script.py
  python3 scripts/your_script.py --help
  ```
* **Clear Suppression State**: If a task is stuck in a `recent_duplicate_failure` loop, the operator must manually approve the cycle or clear the failure marker in the state directory to allow the task to run again.
* **Backlog Pruning**: Use `scripts/prune_failed_backlog.py` to remove tasks that have failed repeatedly from the active backlog.

---

## 2. Incorrect Surface Paths
### Symptom
Subagents attempt to modify files outside the allowed mutation surfaces, leading to failed validation, rejected commits, or coordinator rollbacks.

### Root Cause
1. **Hardcoded Paths**: Subagents hardcode paths referencing developer home directories (e.g., `/home/opencode`) or temporary directories.
2. **Out-of-Scope Modifications**: Subagents modify files in the `state/` directory (which is not git-tracked) or other non-git-tracked directories.
3. **Ignoring Mutation Surfaces**: Subagents fail to prioritize the 7 canonical mutation surfaces defined in the prompt:
   * `surfaces/task_selector.json`
   * `surfaces/prompt_template.md`
   * `surfaces/retry_policy.json`
   * `surfaces/tool_policy.json`
   * `surfaces/memory_policy.json`
   * `surfaces/score_weights.json`
   * `surfaces/lesson_policy.json`

### Mitigation Strategies
* **Use Relative Paths**: Avoid hardcoding absolute paths. Use paths relative to the workspace root (`/var/lib/eeepc-agent/self-evolving-agent/eeebot-self-evolving`).
* **Target Mutation Surfaces**: For clean, bounded changes, prefer editing files in `surfaces/`.
* **Verify Git Tracking**: Only modify files that are tracked by git. Do not modify `state/`, `.env` files, tokens, secrets, or systemd units.
* **Check Path Existence**: Before modifying a file, read it first. Do not assume files or directories exist.

---

## 3. Git Push Rejections (`push_rejected`)
### Symptom
The subagent or coordinator fails at the integration/push stage with a `push_rejected` outcome.

### Root Cause
1. **Concurrent Runs**: Multiple subagents or coordinator instances running concurrently and attempting to push to the remote repository.
2. **Out-of-Sync Commits**: Remote commits (e.g., from manual operator hotfixes or other subagents) are not present in the local workspace.

### Mitigation Strategies
* **Always Pull Before Working**: The coordinator and subagents must run `git pull` (or `git fetch` + `git rebase`) at the start of every cycle.
* **Rebase Local Commits**: If a push is rejected, fetch the latest commits and rebase local commits on top of the remote branch:
  ```bash
  git fetch origin
  git rebase origin/main
  ```
* **Concurrency Control**: Ensure only one subagent runs at a time by checking systemd timers and ensuring `RuntimeMaxSec` is configured to prevent overlapping runs.
* **Follow Branch Discipline**: Implement and commit on the assigned cycle branch. Do not run `git checkout` or `git push` manually; let the bridge handle integration.

---

## 4. Systemd Service Failures
### Symptom
Services fail to start or restart after user deletion, path changes, or system updates.

### Root Cause
1. **Hardcoded Developer Paths**: Systemd unit files, drop-in overrides, or environment files referencing developer home directories (e.g., `/home/opencode`).
2. **Namespace Issues**: Systemd sandboxing or namespace restrictions blocking access to required paths.

### Mitigation Strategies
* **Avoid Developer Home Directories**: Maintain all runtime resources (virtual environments, workspaces, logs) inside dedicated application directories (like `/opt/eeepc-agent` or `/var/lib/eeepc-agent`) owned by the application service account.
* **Use Relative Paths or Environment Variables**: Avoid hardcoding absolute paths where possible, or define them in a single configuration file.
* **Configure RuntimeMaxSec**: Set `RuntimeMaxSec=3300` (55 minutes) in systemd unit files to force-terminate hung subagents.

---

## 5. Actionable Best Practices for Subagents

1. **Check Recent Activity**: Before implementing a task, check the "Recent activity" section in the prompt and the codebase. If the task is already done, skip it.
2. **Run Smoke Tests**: Always verify your changes with a smoke test before committing. If `pytest` is not available, use `python3 -c "import <module>"` or run the script directly.
3. **Follow Branch Discipline**: Implement and commit on the assigned cycle branch. Do not run `git checkout` or `git push` manually; let the bridge handle integration.
4. **Record Lessons**: After resolving a failure, record a structured lesson in `lessons/errors.yaml` and create a matching Markdown card under `lessons/errors/` to prevent the failure from repeating.
