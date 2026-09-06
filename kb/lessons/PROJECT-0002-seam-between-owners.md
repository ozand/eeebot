---
id: PROJECT-0002
title: "A component's tests pass while the seam between components is broken"
category: architecture
severity: high
tags: [deployment, integration, manifests, permissions]
status: active
created: 2026-09-06
updated: 2026-09-06
error_signatures:
  - "ModuleNotFoundError: No module named"
  - "dirty_tree"
  - "Interrupted: 2 errors during collection"
  - "GeminiException"
---

# A component's tests pass while the seam between components is broken

## Symptom

Every test suite is green, every service exits zero, and production is down. Three outages
in one day, all invisible to the components involved:

- **7h of dead cycles.** Every cycle refused to start with `dirty_tree`.
- **11h of a frozen public page.** Every publish died with
  `ModuleNotFoundError: No module named 'agent_context'`.
- **Three cycles killed** by `404 {"detail":"Not Found"}` surfacing as `GeminiException`.

## Root Cause

Each failure sat between two owners, so neither side's tests covered it.

**Ownership across time.** A refactor renamed a skills directory. Git could not delete the
old copy: a subdirectory inside it was owned by `root` while the loop runs unprivileged, so
the unlink failed and an untracked leftover remained. The restore routine reported success
anyway — it verified `HEAD == main`, never that the tree was clean — so 27 consecutive
failures produced no signal at all.

**The import graph versus the shipping list.** A new module was added and imported, but not
added to the deploy manifest. The host copies exactly what the manifest lists. The
`try/except ImportError` guarding the import did not help: the fallback arm imported the
same missing module, so the exception escaped.

**A string that selects a wire protocol.** One model variable among six lacked its route
prefix. A bare gateway name keyword-matched the `gemini` provider spec, was sent as a
Google-shaped call, and the OpenAI-compatible gateway answered 404.

A rename the same day left two test files on old paths, producing
`Interrupted: 2 errors during collection` — the suite ran *nothing*, and the loop burned a
full executor turn rediscovering it and adding `--ignore=` flags.

## Resolution

Fix the seam, not only the instance:

- Restore routines verify the state they were asked to repair, not a proxy for it.
- The manifest gains the module, and the omission becomes a checkable condition.
- The route vocabulary gets one owner, and every member is asserted well-formed rather than
  the broken one being patched.

## Prevention

When a change crosses an ownership boundary, name both sides before shipping:

- **Renaming or moving files:** `rg` the literal path, not the symbol — manifests, tests,
  env files, systemd units and docs all name files by path.
- **Adding an import:** is the imported file in whatever list ships it? An import is a
  dependency declaration most deploy systems cannot see.
- **Writing as one user, reading as another:** check the *directory's* mode, not the file's.
  A file you own inside a directory you cannot write to cannot be deleted.
- **A guard that repairs state:** a repair checking the wrong post-condition reports success
  on precisely the failures it exists to catch.

Give collection errors their own alarm: a test failure still runs the other tests; a
collection error runs none.
