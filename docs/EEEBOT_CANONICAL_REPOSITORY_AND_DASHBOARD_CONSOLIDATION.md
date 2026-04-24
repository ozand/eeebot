# eeebot Canonical Repository and Dashboard Consolidation

Created: 2026-04-24T01:46:43Z

## Decision

`ozand/eeebot` is the canonical repository for eeebot/nanobot product work.

The separate repository `ozand/eeebot-ops-dashboard` is not allowed to become the only durable home for operator-dashboard or ops-control code. Until that code is imported into `ozand/eeebot`, treat it as an external staging/mirror repository, not as the source of truth.

## What happened

A local dashboard/control-plane project was implemented in a separate repository:

- local path: `/home/ozand/herkoot/Projects/nanobot-ops-dashboard`
- GitHub repo: `https://github.com/ozand/eeebot-ops-dashboard`

Recent dashboard issues were created and closed there:

- `ozand/eeebot-ops-dashboard#21` — analytics API and PASS/BLOCK streaks
- `ozand/eeebot-ops-dashboard#22` — collector memory/runtime guard visibility

Only the optional-dependency task was created in the canonical main repo:

- `ozand/eeebot#135` — optional channel dependencies should not block baseline pytest

This split is confusing and creates a retention risk: a future maintainer looking only at `ozand/eeebot` may miss or lose dashboard product code.

## Current proof surfaces

Main repo:

- local path: `/home/ozand/herkoot/Projects/nanobot`
- canonical remote: `https://github.com/ozand/eeebot.git`
- branch: `main`

Dashboard staging repo:

- local path: `/home/ozand/herkoot/Projects/nanobot-ops-dashboard`
- remote: `https://github.com/ozand/eeebot-ops-dashboard.git`
- latest verified commit: `7113272 feat: expose dashboard analytics streaks and service guards`
- tracked files at audit time: 125

Immediate local safety backup created outside tracked source:

- bundle directory: `/home/ozand/herkoot/Projects/nanobot/workspace/backups/`
- bundle pattern: `eeebot-ops-dashboard-*.bundle`
- checksum pattern: `eeebot-ops-dashboard-*.bundle.sha256`

`workspace/` is ignored by git, so this is an emergency local preservation artifact, not a source-control replacement.

## Risk assessment

High-risk if left unchanged:

1. Product code drift
   - Dashboard work continues in `eeebot-ops-dashboard`, while the canonical repo lacks that code.

2. Issue-tracking split
   - Main repo issues show only a subset of actual product work.

3. Discoverability failure
   - New agents or maintainers inspecting `ozand/eeebot` may conclude dashboard work is absent or stale.

4. Retention risk
   - If the dashboard repo is deleted, made private, renamed, or forgotten, the canonical repo does not preserve the implementation.

5. Conflicting instructions
   - Older docs mention the dashboard repo as a sibling, but do not state that `ozand/eeebot` must remain the canonical owner.

## Recommended solution

Preferred path: import the dashboard into the canonical repo with history.

1. Create a main-repo issue in `ozand/eeebot` titled approximately:
   - `Consolidate ops dashboard under canonical eeebot repository`

2. Import `ozand/eeebot-ops-dashboard` into `ozand/eeebot` using `git subtree` under:
   - `ops/dashboard/`

3. Keep the existing dashboard package paths initially for compatibility:
   - Python package may still be `nanobot_ops_dashboard`
   - systemd unit names may still be `nanobot-ops-dashboard-*`
   - runtime env file may still be `/home/ozand/.config/nanobot-ops-dashboard.env`

4. After subtree import, move future dashboard issues to `ozand/eeebot`.

5. Update `ozand/eeebot-ops-dashboard` README to say it is archived/read-only or a mirror, not canonical.

6. Optionally archive the separate GitHub repo only after:
   - code exists in `ozand/eeebot`
   - docs and tests pass from the canonical repo
   - service deployment docs point at the canonical path

## Why subtree is preferred

`git subtree` is preferred over a submodule here because:

- the canonical repo becomes self-contained;
- code is not lost if the sibling repo disappears;
- history can be preserved;
- agents working only from `ozand/eeebot` can see and modify dashboard code;
- GitHub issues and code review can converge on the main repo.

A submodule is less suitable because it still depends on a second repository being available and correctly checked out.

A raw copy is acceptable only as an emergency fallback because it loses useful commit history.

## Bounded migration sequence

Use this exact sequence for the import task:

1. Verify current state:

```bash
cd /home/ozand/herkoot/Projects/nanobot
git status --short --branch
git remote -v

gh issue list --repo ozand/eeebot --state open --limit 20
```

2. Create an issue in `ozand/eeebot` before code movement.

3. Create a task branch from fresh `origin/main`.

4. Add the dashboard repository as a temporary remote:

```bash
git remote add ops-dashboard https://github.com/ozand/eeebot-ops-dashboard.git
git fetch ops-dashboard --tags
```

5. Import with history:

```bash
git subtree add --prefix=ops/dashboard ops-dashboard master
```

6. Verify no secrets/runtime DBs were imported:

```bash
git ls-files ops/dashboard | grep -E 'sqlite|\.env|secret|token|__pycache__|\.pytest_cache' && exit 1 || true
```

7. Run dashboard tests from the imported path or adapt pytest config minimally:

```bash
cd ops/dashboard
PYTHONPATH=src python3 -m pytest -q
```

8. Update canonical docs and service deployment docs to point to `ops/dashboard/`.

9. Commit and push branch to `ozand/eeebot`.

10. Close or relabel dashboard issues only after the canonical import is verified.

## System instruction change

From now on, agents must obey this repository invariant:

- default GitHub target for eeebot/nanobot product work: `ozand/eeebot`
- do not create new durable product code in `ozand/eeebot-ops-dashboard`
- if a separate repo is used for staging, immediately create a canonical tracking issue in `ozand/eeebot`
- before finalizing, ensure the canonical repo contains either the code or an explicit migration issue with proof links

## Current status

As of this note, the dashboard code has not yet been imported into `ozand/eeebot`.

The safe next implementation task is the subtree import under `ops/dashboard/`.

## 2026-04-24 consolidation implementation slice

Current canonical import branch: `chore/import-ops-dashboard`.

Implementation target:
- dashboard subtree lives in `ops/dashboard/` inside `ozand/eeebot`
- runtime package/service/env names remain compatible for this slice
- systemd unit templates point to `/home/ozand/herkoot/Projects/nanobot/ops/dashboard`
- run scripts derive their root from their own location instead of the former sibling checkout
- `ozand/eeebot-ops-dashboard` is retained only as a staging/mirror/legacy reference after import verification

Tracked GitHub issue tranche:
- #136 consolidate dashboard under canonical repo
- #138 update canonical dashboard docs after import
- #139 mark sibling dashboard repo as staging/mirror after canonical import
- #140 add import verification and artifact safety checks

