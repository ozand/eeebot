# eeebot Internal Rename Migration Plan

Goal:
Rename internal `nanobot` package/runtime naming to `eeebot` safely, without breaking:
- the eeepc 32-bit host runtime
- existing dashboard collectors
- current systemd units/scripts
- existing state roots and compatibility surfaces

This should be treated as a staged compatibility migration, not a cosmetic search/replace.

## Why not rename everything now

Current live compatibility depends on many existing names:
- Python package/import path: `nanobot`
- CLI entrypoints and venv paths
- local dashboard package/services/scripts
- existing docs/runbooks and state file references
- eeepc wrapper/service conventions

A bulk rename would create high risk with low immediate operational value.

## Recommended migration phases

### Phase 0 — Public identity only (done)
- GitHub repo renamed to `eeebot`
- README rewritten around `eeebot`
- dashboard repo renamed to `eeebot-ops-dashboard`
- keep internal code/runtime names unchanged for compatibility

### Phase 1 — Compatibility alias layer
Add a no-risk compatibility layer before any hard rename:
- keep `nanobot` as canonical import path temporarily
- add optional `eeebot` wrapper entrypoint/package alias
- document that `eeebot` is project identity while `nanobot` remains runtime compatibility name

Possible bounded tasks:
- add `eeebot` CLI shim that calls current `nanobot` CLI
- add documentation for supported names
- avoid moving package directories yet

### Phase 2 — Internal path inventory
Before renaming code, inventory all places that depend on `nanobot` naming:
- imports
- package names
- pyproject scripts
- systemd units
- shell wrappers
- dashboard config defaults
- GitHub Actions / tests
- eeepc deployment paths
- docs/runbooks

Output should be a machine-readable inventory and a grouped migration matrix.

### Phase 3 — Dual-name runtime support
Introduce dual support temporarily:
- `nanobot` import path still works
- `eeebot` import path also works
- CLI accepts both commands where practical
- dashboard can collect from either naming convention

This phase should include tests proving both names work.

### Phase 4 — Bounded live host trial
Do a side-by-side trial on eeepc without cutting over the active service:
- verification release only
- explicit rollback path
- confirm 32-bit compatibility and wrapper behavior

### Phase 5 — Final cutover
Only after all of the above:
- update canonical service names if desired
- update package/import layout if still worth it
- keep compatibility aliases for one deprecation window

## Non-goals for the rename
- do not move the live authority root during the same migration
- do not rename every historical document artifact
- do not mix a broad upstream merge into the rename
- do not change packaging, providers, and dashboard runtime all at once

## Recommended first implementation slice
The safest next actual code step is:
1. add `eeebot` CLI shim / alias
2. document supported naming
3. keep package/runtime internals unchanged

That gives the new identity operational visibility without risking the current system.
