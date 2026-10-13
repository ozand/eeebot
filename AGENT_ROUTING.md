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
| Browser or web research | Use only for an explicit research need. Prefer read-only inspection. Do not submit forms, change accounts, publish content, or perform external actions without explicit authorization. |
| Host/runtime operations, systemd, production data, deployment, or rollback | Require a separate, explicit task scope naming the target environment, permitted action, stop condition, and rollback authority. Repository code authority alone does not authorize host activity. |

Choose the least-privileged route that can complete the assigned work. If the data classification or route is unclear, sanitize or keep it local; do not guess that disclosure is safe.

## Issue, branch, and review boundaries

- GitHub Issues and their existing status labels are the sole task/status record. Do not create parallel task trackers or duplicate status in this document, PR prose, or memory.
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
