# eeebot Migration Status and Proof

Last updated: 2026-04-21 UTC

Goal:
Record the current state of the `nanobot` -> `eeebot` migration and distinguish between:
- completed public-identity migration
- completed compatibility layers
- intentionally deferred hard internal/runtime renames

## Completed slices

### Public identity
- GitHub repo renamed to `ozand/eeebot`
- dashboard repo renamed to `ozand/eeebot-ops-dashboard`
- both repos are public
- GitHub descriptions/homepages/topics updated
- public README/docs updated to `eeebot` identity

### Compatibility layers
- `eeebot` CLI alias added alongside `nanobot`
- `EEEBOT_*` environment variable aliases added with `NANOBOT_*` fallback
- `~/.eeebot/...` path/config aliases added with safe fallback to `~/.nanobot/...`
- dashboard service aliases added:
  - `eeebot-ops-dashboard-web.service`
  - `eeebot-ops-dashboard-collector.service`
- old `nanobot-ops-dashboard-*` service names still work

### Upstream-safe reliability ports already integrated
- session atomic write + corrupt-file recovery
- subagent session-key routing alignment
- cancelled-turn checkpoint restoration

## Verified outcomes
- main repo public identity is `eeebot`
- dashboard repo public identity is `eeebot-ops-dashboard`
- both old and new dashboard unit names install cleanly
- targeted compatibility tests pass for CLI/env/path slices

## Intentionally deferred (not safe to bulk-rename yet)
These are not forgotten; they are deferred because renaming them now would risk breaking the live eeepc runtime:
- Python package directory `nanobot/`
- broad import-path rename to `eeebot.*`
- runtime state root naming under existing `workspace/state` and eeepc authority paths
- historical control/runtime artifact names
- existing service/script/runtime file names on the live host without explicit alias shims

## Required next steps for a true internal rename
1. Dual import/package support (`nanobot` and `eeebot`) before any package directory rename.
2. Service/script alias expansion beyond dashboard if needed.
3. Explicit migration tooling for any runtime-state or control-artifact rename.
4. Side-by-side eeepc verification release before any host cutover.

## Current conclusion
The migration is already complete at the public identity + compatibility layer level.
The remaining work is a controlled internal/runtime compatibility migration and should not be confused with a cosmetic rename.
