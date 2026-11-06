# Development workflow

This is the focused operational reference for eeebot development. Read it before following task-tracking, change, repository workflow, verification, LiteLLM, or memory instructions. It complements `AGENTS.md`, [`CONSTITUTION.md`](../../CONSTITUTION.md), [`AGENT_ROUTING.md`](../../AGENT_ROUTING.md), and [`REPO_GITHUB_WORKFLOW_RULES.md`](../../REPO_GITHUB_WORKFLOW_RULES.md); it grants no additional authority.

## Task tracking (GitHub Issues + labels)

GitHub Issues are the single coordination layer for our work. All interaction goes through the `gh` CLI. Labels are the metadata store — we do not depend on a Project v2 board (its fields need extra token scopes and GraphQL; at our scale labels are enough).

- **Every substantial task is a GitHub Issue.** Status lives **only** in the `status:*` labels — never duplicated in the Issue body (no drift): `status:discovery` → `status:in-progress` → `status:test` → `status:roll-out`; no `status:*` label = backlog; issue closed = done.
- **Issue metadata contract (existing labels only):** one `type:*` (`runtime|dashboard|process`), `area:*` as applicable (`deploy|eeepc|control-plane|self-improvement|…`), optional `priority:*` and `wsjf:*`, and a `story_id` in the body linking to the canonical spec/change (resolved to `docs/specs/*` or `docs/changes/*`) when one exists.
- **Claiming (our scale = minimal):** set yourself as `assignee` as a *visible signal* and post a claim comment. Assignee is not a lock — do **not** take over an Issue already claimed by another agent; escalate via a comment.
- **Dependencies** are expressed via native sub-issues / `blocked by`, not text task lists. Only pick a **ready** (unblocked) Issue.
- **Done** = PR/commit reference + acceptance criteria met + issue closed with a closing comment + affected `docs/specs/*` updated.
- **Label schema changes are a deliberate act** (human or explicitly tasked agent) — day-to-day work only assigns labels that already exist.
- Markdown task lists are allowed **only** as scratch inside a branch or PR body — never as a persistent backlog.

## Change workflow (specs + changes)

- **Trivial change** (bugfix, doc fix, small scoped edit): just a branch + PR.
- **Substantial change** (new/changed capability): create a `docs/changes/<id>/` folder with `proposal.md` (+ `design.md` if non-obvious). The PR implements it. On merge, **archive** it to `docs/changes/archive/<id>/` and update the affected `docs/specs/<capability>/spec.md`. See [`docs/changes/README.md`](../changes/README.md).
- The change folder is durable *design* documentation tied to a PR — not a status tracker (status is the Issue). The two do not duplicate.
- **Decommission** (removing a writer, unit or module): list its duties as **superseded** (name what does it now), **dropped** (say why nobody needs it) or **orphaned** (a reader still expects the output). In the **same PR**, grep every path it wrote across `ozand/eeebot`, `ozand/eeebot-ops-dashboard`, the instance repo and the host units (`host/eeepc/systemd/`, `/etc/systemd/system`); turn each orphaned read into a labelled `unavailable`/`retired` naming the issue, never an empty default, and record the writer as `orphan:#<issue>` in `nanobot/runtime/state_paths.py`. No test catches this: the reader contract (#1173) lets "empty because absent" pass, so a retired path renders healthy emptiness forever (#1219, #1222, #1224 → ops-dashboard #205). Before reporting, this must show only labelled reads: `rg -n "<state/dir it wrote>" . ../eeebot-ops-dashboard ../eeebot-self-evolving`

## Working tree and branch safety

Read [`REPO_GITHUB_WORKFLOW_RULES.md`](../../REPO_GITHUB_WORKFLOW_RULES.md) before repository/Git workflow operations; its mandatory preflight, isolation, sync, and stop conditions apply. Additional project rules:

- Do not work directly on `main`. One task = one branch (`feat/*`, `fix/*`, `docs/*`, `chore/*`); prefer `git worktree` for parallel/risky work.
- Run `git status --short --branch` + `git fetch --all --prune` before edits.
- Classify dirty files as task-related vs unrelated; never mix unrelated edits into one branch/PR. Re-check `git diff <base>...HEAD` before opening a PR.
- **One open PR per issue.** Rework of an open PR — a stale base, review findings, a branch that picked up unrelated edits — goes to the **same** branch: `git rebase origin/main`, fix, `git push --force-with-lease`. Open a replacement branch only when the old one cannot be repaired, and close the predecessor PR in the same step with a comment naming the replacement. Suffixes such as `-v2`, `-clean`, `-final`, `-reconciled` are the symptom this rule exists for (#1244). Before reporting, this must print `[]`: `gh pr list --state open --json number,headRefName --jq '[.[] | {n: (.headRefName | capture("(?<n>[0-9]{3,4})").n // "none"), pr: .number}] | group_by(.n) | map(select(length > 1))'`
- Assume concurrent rename edits may land nearby; keep edits minimal and task-local; do not reintroduce legacy naming in new code unless compatibility requires it.

## Commands and CI

```bash
pip install .[dev]                              # install with dev deps
python -m pytest tests/ -v                      # full Python test suite
python -m pytest tests/<file>.py -k <pattern> -v # focused test
ruff check <path>                               # lint (configured in pyproject.toml)
```

CI runs the Python test suite plus a `ruff --select F811` redefined-name check, on Python `3.11`/`3.12`/`3.13`. No system packages are installed first — the Matrix channel (and its `libolm-dev` build dependency) was removed 2026-07-05 per #602.

## LiteLLM configuration

All LiteLLM credentials/routing for the eeepc runtime live in **`/etc/eeepc-agent/litellm.env` only**. Never set `LITELLM_API_KEY` / `LITELLM_BASE_URL` / `LITELLM_MODEL` elsewhere. Models require a `cl/`, `an/`, or `un/` gateway prefix. See README "LiteLLM configuration".

## Memory

`memory/MEMORY.md` (index of current project state) and `memory/HISTORY.md` (append a one-line `[YYYY-MM-DD HH:MM] <what you did>` entry per change) track development state. Read `MEMORY.md` before picking up self-evolving work.
