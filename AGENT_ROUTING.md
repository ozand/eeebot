# Agent Routing and Authority

This policy governs development agents working in `ozand/eeebot`. It complements `AGENTS.md`; it does not grant runtime, host, GitHub, or deployment authority.

## Three separate questions

- **Responsibility** is the bounded task a person or agent is assigned and accountable for.
- **Capability** is what its model, tools, identity, and environment can technically do. Tool availability is not permission.
- **Authority** is the explicit approval to perform an action. It comes from the operator or an existing repository process, not from a role name, model, successful tool call, issue assignment, or another agent's suggestion.

When any action exceeds the granted scope, stop before mutation and request the specific decision from the owner. Never infer merge, issue closure, host access, deployment, credential use, or architectural acceptance authority from implementation authority.

## Route work by data and task

| Work or data | Route and limits |
|---|---|
| Product source, tests, and repository documentation | Use an isolated issue branch/worktree and the project's documented local tools. Assign one writer per worktree. |
| Private, credential-bearing, or otherwise sensitive context | Keep it on an explicitly trusted local route. Do not send secrets, tokens, keys, cookies, auth state, private runtime data, or unrelated personal information to cloud agents, browser services, or issue text. |
| Routine, sanitized analysis that benefits from an external model | Send only the minimum redacted context needed. Treat its answer as advisory; verify factual claims against source, tests, and GitHub. |
| Browser-based research or implementation | Use only when explicitly assigned. Name the target repository/site and permitted actions; a browser session or logged-in identity is capability, not authority. Do not submit, publish, change accounts, or persist remote state unless that action is explicitly in scope. |
| Host/runtime operations, systemd, production data, deployment, or rollback | Require a separate, explicit task scope naming the target environment, permitted action, stop condition, and rollback authority. Repository code authority alone does not authorize host activity. |

Choose the least-privileged route that can complete the assigned work. If the data classification or route is unclear, sanitize or keep it local; do not guess that disclosure is safe.

## Stable work allocation

Use these defaults only when the task and current tool access match; verify the actual identity, repository, and environment on each handoff. The table assigns responsibilities, not blanket permissions.

| Work | Default responsibility | Boundary |
|---|---|---|
| Product implementation and Linux-targeted verification | Assigned product writer in an isolated `ozand/eeebot` worktree | One writer per worktree; do not infer host access from Linux tests or repo access. |
| Browser-based dashboard/site changes | Assigned web writer in the explicitly named dashboard/site repository | Browser or cloud workspace access does not authorize edits to another repo or publication. |
| Independent local review, Windows verification, and workspace/KB operations | Local operator or explicitly assigned local reviewer | Keep private workspace, credentials, host state, and local knowledge stores on authorized local routes. |
| Coordination | Parent/coordinator | Sequence and report work only when assigned coordination; no file edits or tests under that role. |
| Exact-SHA review | A different, read-only reviewer from the writer | Review the committed head and base; writer cannot self-review. |

Actual capability and environment vary by session. Confirm available tools, repo identity, OS/runtime target, and data sensitivity before routing; never rely on role labels alone.

## Issue, branch, and review boundaries

- GitHub Issues and their existing status labels are the sole backlog and canonical task-status record. Do not create parallel task trackers or maintain a second live status elsewhere; PRs and handoffs may state evidence and status as of a named SHA/check.
- Before mutation, verify the open governing issue, its scope, dependencies, and current claim. Work only on its isolated branch/worktree; preserve unrelated dirty state.
- Keep one writer per worktree. A coordinator may sequence work but must not edit files or run tests when assigned only coordination.
- Reviewers use a fresh read-only context and inspect the exact committed head against its stated base. A review of another SHA is not approval of the current SHA. Resolve findings on the same issue branch, then rerun required checks on the new SHA.
- Report full commit SHA, base SHA, changed paths, exact commands/results, review status, and CI run/check state. Distinguish pending, failed, cancelled, and successful checks; never call partial CI green.
- Use the repository's quoted `--body-file` procedure for GitHub bodies containing Markdown/code. Never put backticks or shell-expanding content inside double-quoted `gh --body` arguments.

## Decision and delivery gates

- Architectural, public-contract, security/trust-boundary, data-format, dependency, or deployment-target decisions require the repository ADR process before implementation. An agent may draft or implement an authorized decision but must not record operator acceptance on the operator's behalf.
- Host, systemd, rollout, and production actions remain out of scope unless explicitly authorized for the named environment. A merged repository PR does not imply host deployment.
- Do not merge, close an issue, or change an ADR status unless the governing acceptance procedure and explicit authority permit that exact action.
- Handoffs contain only the task-relevant, sanitized context, exact source/base references, evidence, blockers, and next action. Do not copy prompts, secrets, session transcripts, or unrelated workspace data.

## Data-boundary deviation: contain and continue safely

An individual data-boundary deviation does not revoke independently approved work. Stop the forbidden action and contain only the deviation; terminate the offending child when needed. Do not reuse or forward the implicated payload. Provide a sanitized receipt that separates known facts from unknowns and contains no sensitive paths or secrets. Preserve existing logs and evidence; do not perform destructive cleanup.

Continue only when the same independent task remains authorized and separable: start from a fresh context using only its approved source SHA, issue, and CI job. No new GO is needed for that already-approved work. Never repeat a prohibited scan through another executor. Escalate new access or scope, inseparable contamination, concrete credential-publication or rotation need, unauthorized mutation that compromises evidence, or guard bypass. A guard/access rejection is not unavailability; do not change executor or tool to evade the same restriction. All existing review, CI, merge, issue-closure, and deployment gates remain in force.

### Verification scenarios

1. **Private-history read:** Stop further access and do not reuse or forward the read material. Provide a sanitized known/unknown receipt, preserve logs, then continue the already-approved independent analysis in a fresh context from its approved source SHA, issue, and CI job. Existing review and CI gates still apply.
2. **Published secret:** Contain the publication and escalate the concrete incident and any credential-rotation need; do not repeat or propagate the secret. Continue independent work only if it is demonstrably separable and uses a fresh approved source context. Keep all existing delivery gates.
3. **Guard denial:** Stop the denied action and report the rejection as a guard/access boundary, not unavailability. Do not retry with another executor or tool; escalate if the task needs new access or scope. No work proceeds by bypass, and gates remain unchanged.
4. **Unknown source index:** Preserve the checkout and logs; do not guess, substitute a source, or destructively clean up. Report known facts and unknowns without sensitive paths, and leave the result unaccepted until the approved source SHA is known. Resume only from that approved source with the existing gates intact.
