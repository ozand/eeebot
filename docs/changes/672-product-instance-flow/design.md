# 672 — Design: downstream core-sync + upstream harvest

Two flows between the product (`ozand/eeebot`) and one instance
(`eeebot-self-evolving`). Flow 1 moves product truth down into the stale
instance checkout. Flow 2 moves vetted instance improvements up into the
product. They are designed together because Flow 1's procedure should
absorb whatever Flow 2 has already shipped, so the re-seed does not
throw away work the instance would otherwise re-derive.

## FLOW 1 — Downstream core-sync (product → instance)

### The problem, precisely

The instance's **working checkout** (not the deployed engine — see
`proposal.md`) carries a `nanobot/` + `tests/` tree that forked before the
simplification program and never caught up. Concretely, per the #672
measurement:

- `coordinator.py`: 4,509 lines (instance, pre-split) vs. 1,034 (product).
- Retired channels still present (dingtalk, discord, email, feishu, matrix,
  mochat, qq, slack, wecom, whatsapp) and un-trimmed providers (deepseek,
  zhipu, dashscope, …).
- `hermes_pi_qwen` / `ayga.tech` `pi_dev` hardcodes still resident (removed
  from product in #637/#641).
- Pre-#619 `from eeebot.*` imports (product unified on `nanobot.*` only,
  enforced by `tests/test_import_hygiene.py`).
- Missing product-only subsystems entirely: `tool_harness`, `stop_guards`,
  `bridge.py`, `archive`, `scorer`, `probes`.

Because the instance's own smoke gate (`nanobot/runtime/bridge.py`) runs the
instance's **own** test suite against its **own** `nanobot/` + `tests/`,
product fixes and hardening (like #666/#668/#669/#670/#678 itself) do not
reach that gate automatically — each one needs manual porting, or the
instance gate keeps validating cycles against a superseded implementation.

### Approach: re-seed, not merge

The #672 measurement found Bucket B (instance-originated innovation *inside*
core `nanobot/`) **empty** — every differing or instance-only core file
traces to a specific product removal/refactor commit, not to instance
invention. That means there is no core content to reconcile via merge or
cherry-pick; a three-way merge would only fight the simplification history
for no benefit. The correct operation is a **re-seed**: replace the
instance's `nanobot/` + `tests/` wholesale with product HEAD's, and leave
everything else untouched.

**Replaced (wholesale, from product HEAD):**
- `nanobot/` (the entire package)
- `tests/` (the entire product test suite)
- `pyproject.toml` dependency/tooling sections that gate what `tests/`
  and `nanobot/` need (version-controlled, not runtime state)

**Preserved (instance-only, untouched by the re-seed):**
- `scripts/` — the instance's additive automation layer, including the
  two enhancements already harvested upstream via #673
  (`cleanup_subagent_queue.py`, `cycle_logger.py` — see Flow 2's coupling
  note below on why the *harvested* versions should replace the instance's
  pre-harvest local copies as part of the same pass, not as a surprise
  regression)
- `surfaces/` — bounded mutation surface for subagent-authored artifacts
- `state/` (untracked, runtime data — never in scope for a code re-seed)
- goal artifacts (`state/goals/`, seeded by `deploy_release.sh` from
  `host/eeepc/etc/goal_text.json` — orthogonal to this migration)
- `memory/`, `lessons/` — durable history, never rewritten in place
  (matches migration spec R7's rule for durable artifacts)
- `docs/` — instance-local docs, if any diverge from product's; reconcile
  content conflicts case by case, this is not a wholesale replace

### Operational risks and mitigations

Risks are ordered by how directly the #672 measurement surfaced them.

**(a) Stale imports in instance `scripts/`.** Scripts written against the
old, pre-simplification core may import modules the product removed or
restructured (`state_promotion`, `state_subagents`, old monolithic
`coordinator` internals that are now split across smaller modules).
*Mitigation:* before replacing `nanobot/`, grep the instance's `scripts/`
tree for imports of `nanobot.*` symbols and diff that import surface
against product HEAD's actual public symbols; fix or remove each script
that resolves to a name product no longer exports. Do this as a dedicated
pre-migration step with its own commit, so a script fix is reviewable
separately from the re-seed itself.

**(b) Config schema drift.** The instance's live config may still set
fields product has removed — `pi_dev`-family executor settings
(`provider`, `bin_path`, per #641), or provider entries for channels/models
product trimmed. A re-seeded `nanobot/config/schema.py` may reject or
silently ignore instance config that references removed fields.
*Mitigation:* diff the instance's live config file against product HEAD's
current `schema.py` fields *before* cutover; strip or migrate any
removed-field references; confirm the instance's `NANOBOT_SUBAGENT_EXECUTOR`
and provider settings already match the built-in-executor-only path (#641)
since that removal predates this design.

**(c) State/record-shape compatibility.** Product added fields additively
to state records since the instance forked (e.g. `success_signals`,
`subagent_no_commit`). These should degrade safely when read by older
consumers because they're additive, but this is an assumption, not a
verified property for every state consumer the instance runs.
*Mitigation:* after cutover, spot-check the first post-migration cycle's
state artifacts by hand (not just "tests passed") to confirm the new
record shape round-trips through whatever the instance's own scripts read
back out of `state/`.

**(d) An untested re-seed must not reach instance `main` un-verified.** The
instance's own smoke gate is exactly the mechanism #678 hardened to stop a
bad cycle from landing — the re-seed, being the single largest code change
the instance will ever apply to itself, must go through the same discipline
it enforces on every autonomous cycle, or be treated as an explicit
out-of-band exception with a documented backup and rollback plan.
*Mitigation:* run the re-seed on a dedicated cycle branch and require the
full gate (not a partial one) to pass before it reaches `main`; if the gate
cannot practically evaluate a change this size in one pass, apply it
out-of-band under human supervision with the backup/rollback below, and
record that exception explicitly rather than silently skipping the gate.

### Migration procedure

1. **Backup.** Snapshot the instance repo's current `main` (tag or bundle)
   and its live `state/` directory, before touching anything. This is the
   rollback anchor.
2. **Pre-migration script audit.** Grep instance `scripts/` for imports of
   `nanobot.*` / `eeebot.*` symbols; cross-check each against product
   HEAD's current module layout; fix or remove scripts that break. Commit
   this fix set on the migration branch, separately from the re-seed.
3. **Config reconciliation.** Diff the instance's live config against
   product HEAD's `nanobot/config/schema.py`; remove or migrate fields the
   product schema no longer recognizes (provider/channel trims, `pi_dev`
   fields).
4. **Re-seed.** On the migration branch, delete the instance's `nanobot/`
   and `tests/` trees and copy product HEAD's in place, unmodified. Bring
   over the coupling note from Flow 2 (already-harvested `scripts/`
   improvements replace the instance's pre-harvest local copies here, not
   separately).
5. **Run the full instance suite in a clean venv** (`pip install .[dev]` +
   `python -m pytest tests/ -v`), matching the CI reality this repo already
   documents (Python 3.11/3.12/3.13, no system packages).
6. **Commit on the migration branch**, with a message that names this
   design doc and the backup reference from step 1.
7. **Verify.** Run at least one full autonomous cycle against the migration
   branch through the instance's own smoke gate (the #678-hardened path in
   `nanobot/runtime/bridge.py`), and hand-inspect the resulting state
   artifacts per risk (c) above.
8. **Cut over.** Merge the migration branch to the instance's `main` only
   after step 7 passes cleanly.
9. **Rollback path.** If step 5, 7, or a post-cutover cycle fails in a way
   that isn't a quick forward-fix, restore `main` from the step-1 backup
   and restore `state/` from the same snapshot; the deployed engine on the
   host is unaffected either way, since it always runs current product via
   `deploy_release.sh` independent of the instance's own checkout.

### Cadence after the one-time re-seed

The #678 protect-list is what makes a one-time re-seed durable rather than
a problem that just recurs: `_ALLOWED_PATH_PREFIXES = ('surfaces/',
'scripts/', 'memory/', 'lessons/', 'docs/', 'tests/')` in
`_validate_mutation_surfaces` (`nanobot/runtime/bridge.py`) already blocks
autonomous cycles from writing to `nanobot/` at all — core is not a
subagent-writable mutation surface. So a re-seed does not immediately start
re-diverging the way it would have pre-#678.

Given that, the sync cadence can be lightweight: a **periodic (or
per-deploy) core-sync** — re-running steps 4–8 of the procedure above,
without needing the one-time steps 1–3 again once the instance's scripts
and config are already reconciled — keeps the instance's `nanobot/` +
`tests/` tracking product on an ongoing basis. Once this cadence exists,
future product fixes and hardening reach the instance's own gate
automatically instead of requiring manual porting, which is the friction
this design set out to remove.

### Decision needed from the operator

This design does not decide **whether to execute the one-time re-seed now**
versus deferring it. Both options are legitimate:

- **Execute now:** removes the ongoing double-port friction immediately,
  while the #672 measurement (empty Bucket B) is fresh and the risk
  enumeration above is current. Cost: a large, one-time change to a live
  system that just achieved its first successful autonomous integration —
  risk (d) above is real even with the mitigations.
- **Defer:** the instance keeps running its current (working) fork a while
  longer. Cost: continued manual porting of any product fix the instance
  needs; the friction is real today but shrinking as core-touching dev
  pace slows and more work lands in the already-synced additive surfaces.

The operator should make this call explicitly — this document only ensures
that whenever it happens, the procedure, risks, and rollback are already
written down.

## FLOW 2 — Upstream harvest (instance → product)

### Selection signal

Not every instance commit is harvest-worthy. Two signals narrow the
candidate set to what's actually vetted:

- **Integration status.** Only changes that passed the instance's own gate
  (i.e. were integrated into the instance's `main`, not left on an
  abandoned cycle branch) are candidates — the gate is what stands in for
  review at this scale.
- **Reward/lessons signals.** The system already tracks which cycles were
  judged successful and which produced durable `lessons/` entries; a
  harvest pass should weight candidates the runtime itself flagged as
  valuable higher than incidental changes.

### Generality filter

A candidate is harvest-worthy only if it is **general** — useful to any
host running this product, not specific to the eeepc instance's local
environment. The #672 measurement gives both a positive and a negative
example from the same harvest pass:

- **General (harvested):** `cleanup_subagent_queue.py`'s `--max-queue N` /
  `--json` / metadata-file exclusion, and `cycle_logger.py`'s
  `--list`/`--dedup`/`--compact`/`--stats` — both pure script-layer
  behavior with no host-local assumptions. Promoted to product as PR #673.
- **Host-local (not harvested):** any script encoding paths or resource
  ceilings specific to the eeepc host — e.g. a hypothetical
  `cycle_resource_correlation` tuned to `/var/lib/eeepc-agent/...` paths or
  the eeepc host's specific memory/CPU ceilings, versus a generic
  `host_metrics_sampler` shape that would generalize. The test is whether
  the same code would make sense unmodified on a different host running
  the same product.

### Mechanism (v1 — manual, product-simple)

v1 is a manual periodic pass, exactly as executed for #673:

1. Diff the instance's `main` since the last harvest pass (or since
   inception, for the first pass).
2. Classify each integrated, gate-passed change against the two signals and
   the generality filter above.
3. For each general candidate: port it to product as its own PR (small,
   reviewable, with product-side tests — #673 added 15 pytest tests the
   instance's embedded `--test` harnesses didn't have, since CI only runs
   `tests/`, not ad hoc script harnesses).
4. Leave host-local candidates in the instance; no action needed — they are
   correctly *not* upstreamed.

**Cadence:** trigger a pass after roughly N integrated cycles (candidate
count exists to review) or on a fixed period (e.g. weekly), whichever comes
first in practice — this avoids both a backlog of unreviewed instance
improvements and a pass so frequent it has nothing new to look at.

v2 (only if v1's manual cadence proves too slow or too frequent to sustain
by hand) would be a bounded harvest subagent that scores candidates against
the same signals and drafts the port PRs for human review — not build until
v1 demonstrates the need, per product-simplicity.

### Harvest yield as a meta-signal

The size and quality of each harvest pass is itself a signal about whether
the self-evolving instance is producing *generalizable* value, versus value
that only ever pays off locally. A pass that yields nothing general for
several cycles in a row is not necessarily a failure of the harvest
mechanism — it may mean the instance's recent work has been host-local
maintenance rather than product-shaped improvement, which is useful
information in its own right when judging the self-evolution program.

### Coupling with Flow 1

Flow 1's re-seed step 4 must incorporate whatever Flow 2 has already
harvested and shipped to product (e.g. the #673 script enhancements),
rather than reintroducing the instance's pre-harvest local versions as
"preserved" `scripts/` content. Concretely: when the re-seed runs, the
instance's `scripts/` directory should already be at (or updated to) the
harvested versions, so the instance stops re-deriving improvements product
already has. This is a checklist item on the re-seed procedure (step 4),
not a separate flow.

## References

- Issue #672 (this design's tracking issue) and its comments (measurement:
  empty Bucket B, magnitude of drift, #673 harvest landed).
- PR #673 — first executed upstream harvest.
- `nanobot/runtime/bridge.py` `_validate_mutation_surfaces` /
  `_ALLOWED_PATH_PREFIXES` — #678 hardening this design's cadence section
  relies on.
- `docs/specs/migration/spec.md` — R7 (durable state never rewritten in
  place), informing what Flow 1 preserves.
- `docs/changes/641-remove-pi-dev-executor/proposal.md` — example of a
  product-side removal the instance's config must reconcile against
  (risk (b)).
- `docs/specs/self-evolving-runtime/spec.md` — cycle/gate contract Flow 1's
  migration procedure runs through.
