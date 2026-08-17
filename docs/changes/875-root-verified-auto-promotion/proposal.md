# Change: root-verified auto-promotion of runtime-slice candidates

- **change-id:** 875-root-verified-auto-promotion
- **issue:** #875
- **capability:** self-evolving-runtime (#812 runtime-slice tier + #822 causal
  micro-benchmark + #780 held-out pack), eeepc host provisioning
- **role / workstream:** trust root of the whole RSI (recursive self-
  improvement) plan — correctness over speed

## Problem: the naive design is fatal

#812 added a bounded "runtime-slice" tier — an operator-approved allowlist of
`nanobot/runtime/*.py` modules the loop may PROPOSE changes to. Those changes
never auto-integrate: a green cycle lands as a durable candidate record under
`state/promotions/promotion-runtime-*.json` for an operator to review by hand.

That manual-review step does not scale, and the obvious next step — "auto-
promote once the numbers look good" — is fatal to build naively, because of a
fact discovered during a live-host security audit:

> On the eeepc host, the bridge/harness AND the instance's subagent run as the
> **same uid** (`eeepc-agent`). `state/` is instance-writable — this is why
> #789 *detects* sidecar tampering rather than *preventing* it (there is no
> filesystem boundary between "harness-authored" and "instance-authored"
> inside `state/`).

If an auto-promotion loader trusted an instance-written index (say, a
candidate record's own `changed_files`/`microbench` fields) to decide what
code to load into the harness process, the instance could forge index +
file to get **arbitrary code execution in harness context, before any gate
ever ran** — a total collapse of the #603 immutable-core invariant. No
in-band signature scheme closes this: since harness and instance share a
uid, the instance can read any verification key the harness could read too.

## The solution: root is the only real boundary

The only privilege boundary that actually exists on this host is **root** —
deploys run via `sudo`, and the release tree / `/etc/eeepc-agent` are
root-touched. The design puts root in the loop as an automated, but
completely independent, verifier:

```
instance (eeepc-agent uid)          root (systemd timer, no User=)              eeepc-agent uid (again)
─────────────────────────           ───────────────────────────────            ────────────────────────
runtime-slice cycle goes green  →   eeepc_promotion_verifier.py:                nanobot.runtime.promoted_overlay:
writes a PROPOSAL record             1. reads the PROPOSAL as untrusted          on bridge/coordinator start,
  state/promotions/                     input — only its (module_path,           reads PROMOTED_TREE/manifest.json
  promotion-runtime-*.json               head_sha) pair is trusted, and           (root-owned, agent can read but
  (its own microbench numbers          that only because head_sha is a           never write) and loads any
  are IGNORED downstream)               content-addressed git SHA               `status: "active"` module over
                                      2. re-materializes candidate bytes         the installed copy — after
                                         itself: `git show <head_sha>:           checking sha256 integrity and
                                         <module_path>` against the              re-checking deny-set/slice
                                         INSTANCE repo (content-addressed,       membership independently.
                                         not a trust decision)
                                      3. RE-MEASURES itself: #822's
                                         microbench (baseline = the
                                         ACTUAL INSTALLED product tree,
                                         never instance git history) +
                                         #780's held-out pack — the
                                         instance's own numbers are
                                         never read for this decision
                                      4. only if independently satisfied,
                                         soaks 3 clean passes, then
                                         writes verified bytes into
                                         PROMOTED_TREE (root-owned 0755,
                                         files 0644) + updates
                                         manifest.json
                                      5. watches every ACTIVE promotion
                                         every pass after that: held-out
                                         regression, a new integrity
                                         ledger event, or an operator
                                         `status: "vetoed"` edit rolls
                                         it back (removes the tree file
                                         + manifest entry, ledgers it)
```

Filesystem permission **is** the trust boundary — there is no signing key to
protect because there is no signing key. `PROMOTED_TREE` (default
`/var/lib/eeepc-promoted`) is root-owned, 0755; its files are 0644. The
eeepc-agent uid can read it (needed to load the module) but structurally
cannot write it (no sudo, no setuid path). This is exactly the same kind of
boundary the rest of the deploy pipeline already relies on (release tree,
`/etc/eeepc-agent`) — #875 just extends it to cover one more artifact class.

## Data flow / components

| Component | Runs as | Trusts | Writes to |
|---|---|---|---|
| bridge/coordinator (`#812` classifier) | eeepc-agent | nothing new — same #812 gate | `state/promotions/promotion-runtime-*.json` (PROPOSAL only) |
| `host/eeepc/libexec/eeepc_promotion_verifier.py` (**new**, C2) | **root** (systemd, no `User=`) | only `(module_path, head_sha)` from the proposal — a content-addressed pointer, not a claim | `PROMOTED_TREE/manifest.json`, `PROMOTED_TREE/verifier_state.json`, `PROMOTED_TREE/{flattened_module}.py`, a `phase: "promotion"` ledger event |
| `nanobot.runtime.promoted_overlay` (**new**, C3) | eeepc-agent | only what root wrote into `PROMOTED_TREE`, after its own boundary + deny + sha256 checks | nothing (read-only loader) |
| `nanobot.runtime.runtime_deny` (**new**, C1) | shared by all three above | n/a (pure stdlib logic, no external trust) | nothing |

### C1 — `nanobot/runtime/runtime_deny.py`

Pure-stdlib extraction of the #812 deny-set (`_RUNTIME_DENY_ALWAYS_FILES`,
`_RUNTIME_DENY_TOKENS`, `_is_runtime_deny`) and a new pure
`runtime_slice_paths(env_value)` (same parsing as bridge's old
`_runtime_slice_paths()`, but taking the raw string as an argument instead of
reading `os.environ` itself). `bridge.py` re-exports the same names
unchanged — existing tests (`tests/test_runtime_slice.py`) still pass
untouched. The root verifier and the agent loader import this module
directly, so "what counts as safety-shell" and "what counts as in-slice" is
defined exactly once and enforced identically in all three trust contexts.

### C2 — `host/eeepc/libexec/eeepc_promotion_verifier.py`

Standalone script, no `User=` in its systemd unit (runs as root). One
`verify_pass()`:

1. Scans `STATE_DIR/promotions/promotion-runtime-*.json` — untrusted
   proposals.
2. Eligibility (ignores the instance's own microbench dict entirely):
   `changed_files` must contain **exactly one** file matching
   `nanobot/runtime/*.py`, that file must not be `_is_runtime_deny`, and it
   must be in the operator's `SELFEVO_RUNTIME_SLICE` (passed to the verifier
   via the SAME instance env file the bridge reads, so it can never drift).
3. Re-materializes candidate bytes via `git show <head_sha>:<module_path>`
   against `INSTANCE_REPO` (argv list, no shell).
4. Re-measures via `nanobot.runtime.heldout.microbench.run_measurement`
   (baseline vs. candidate against the installed product tree — the #822
   sandbag-proof design) and requires
   `improvement_pct >= benchmark_evidence._MICROBENCH_MIN_IMPROVEMENT_PCT`,
   then runs `nanobot.runtime.heldout.run_heldout` and requires
   `regressions == []` and no `status == "fail"` result. Either failing marks
   the candidate `rejected` with a reason and it is never retried (a
   deterministic verdict re-run would only reproduce itself).
5. An eligible candidate enters `soaking`; each subsequent pass re-checks
   held-out cleanliness plus "no new `phase: integrity` ledger event since
   its watermark". After 3 clean subsequent passes it promotes.
6. Promotion writes the re-measured bytes to
   `PROMOTED_TREE/{module_path.replace('/', '__')}` (atomic tmp+rename,
   0644) and a `manifest.json` entry (schema `promoted-manifest-v1`:
   `{module_path: {sha256, candidate_id, head_sha, improvement_pct,
   verified_utc, status: "active"}}`), plus a `phase: "promotion"` /
   `reason: "root_verified_promoted"` ledger event — the ledger file is
   chowned back to `eeepc-agent:eeepc-agent` afterward (best-effort) so the
   bridge keeps being able to append to it.
7. Every active manifest entry is watched on every subsequent pass: a
   held-out regression, a new integrity event since promotion, or an
   explicit operator `status: "vetoed"` edit (sudo-editing the root-owned
   manifest — this IS the veto mechanism, no separate CLI needed) rolls it
   back — removes the tree file + manifest entry, ledgers
   `reason: "root_verified_rolled_back"`.

Every candidate and every active-manifest-entry step runs inside its own
`try/except` — one bad record can never abort the rest of a pass, and the
script is safe to run unattended every 15 minutes forever.

### C3 — `nanobot/runtime/promoted_overlay.py`

`install_promoted_overlay(promoted_tree=None) -> list[str]`. Built from
**stdlib + `runtime_deny` only** — no other `nanobot.runtime` import, so
installing the overlay never depends on anything the overlay could itself
replace.

- Resolves `PROMOTED_TREE` (arg > env > `/var/lib/eeepc-promoted` default).
  Missing tree or manifest → quiet no-op `[]`.
- **Boundary self-check (the critical line):** both the tree directory and
  `manifest.json` must be `st_uid == 0` (root-owned) and neither
  group- nor other-writable, or **everything** is refused. POSIX-only by
  construction; on any non-POSIX platform (`os.name != 'posix'`, e.g. a
  developer's Windows machine) this refuses to load anything at all rather
  than pretend the check passed — the feature is host-only.
- Per manifest entry with `status == "active"`: re-derives the canonical
  slice-shaped path via `runtime_deny.runtime_slice_paths` (this doubles as
  the deny-set check, the slice-shape check, and a traversal-normalization
  check, all in one call — a malformed or deny-set key never round-trips
  back to itself and is refused), verifies `sha256(tree_file) ==
  manifest["sha256"]`, then loads it via
  `importlib.util.spec_from_file_location` under the module's real dotted
  name, registers it in `sys.modules`, and `setattr`s it onto the (normally
  imported) parent package so any subsequent `from nanobot.runtime import X`
  picks up the promoted version.
- Fail-open **per module** (one bad entry never blocks the others — the
  installed copy of that module is simply kept); fail-**closed** overall
  (any unexpected top-level exception returns `[]`, i.e. degrade to the
  fully-installed, already-reviewed product tree).

**Wiring.** `bridge.py` calls `install_promoted_overlay()` at the very top
of its import block — immediately before `from nanobot.runtime import
llm_proposer` (the first `nanobot.runtime.*` import in that file) — so a
promoted module shadows the installed one from the first import site, not
just for call sites deeper in the file. `coordinator.py` calls the same
function too, but **inside `run_self_evolving_cycle()`'s body**, not at
module level, for a load-bearing reason documented below.

**Why coordinator.py's call site is NOT at module top-level (a deliberate
deviation).** `nanobot/runtime/__init__.py` unconditionally imports
`coordinator.py` for **any** `nanobot.runtime.*` import — including the
`heldout/microbench.py` sandboxed subprocess's own
`from nanobot.runtime import existence_index as ei` (used by every
microbench spec script, see `MICROBENCHES`). If `install_promoted_overlay()`
were called at coordinator's module scope, it would fire **inside that
sandboxed measurement subprocess too**, and since the subprocess's stripped
env does not set `PROMOTED_TREE`, the loader would fall back to the real,
production `/var/lib/eeepc-promoted` default — letting the ACTUAL root-owned
overlay leak into what must be a pure, harness-controlled baseline/candidate
comparison over a throwaway tree copy (defeating the #822 isolation
guarantee, and in the worst case measuring the SAME already-promoted bytes
for both baseline and candidate). Gating the call to only fire when a cycle
actually **runs** (inside the function body, not on mere import) keeps the
microbench sandbox subprocess overlay-free with zero special-casing needed
in `microbench.py` itself. `bridge.py` does not have this problem —
nothing in the package-init chain imports `bridge.py`, so its module-level
call site only ever fires when something explicitly imports the bridge.

### C4 — `scorecard.py` visibility

`control_plane.runtime_promotions` (added inside
`_control_plane_snapshot()`, so both the success and fail-open branches of
`compute_scorecard` get it): `{"active": <n from manifest.json>, "soaking":
<n>, "rejected": <n>}` (soaking/rejected from `verifier_state.json`,
candidate ids joined by status). Cheap, read-only, fail-open to
`{"active": 0, "soaking": 0, "rejected": 0}` on any missing/unreadable file —
same posture as the rest of `control_plane`.

### C5 — provisioning

- `host/eeepc/systemd/eeepc-promotion-verifier.service` (no `User=` → root)
  + `.timer` (`OnBootSec=5m`, `OnUnitActiveSec=15m`, matching the bridge's
  own cadence — the existing `install_units`/`install_libexec` glob loops in
  `install.sh` pick both files up automatically, no loop changes needed).
- `install.sh`: `/var/lib/eeepc-promoted` is now created explicitly, AFTER
  (and outside) the recursive `eeepc-agent` chown, owned `root:eeepc-agent`,
  mode `0755` — and `eeepc-promotion-verifier.timer` was added to the
  `enable_timers` list.
- `deploy_release.sh`: the libexec sync's `chmod +x` was previously scoped
  to `eeepc-self-evolving-*.py`, which would have silently left
  `eeepc_promotion_verifier.py` non-executable — broadened to every synced
  `.py`. Also now syncs systemd units on every deploy (previously only done
  once at `install.sh` time) and enables+starts the new timer.

## Trust invariants (also in C2/C3 docstrings verbatim)

- The instance authors only PROPOSALS (candidate records, git branches). It
  never authors the promoted bytes or the numbers: root re-materializes from
  a content-addressed git SHA and re-measures itself, from scratch, on
  every candidate.
- The promoted tree is root-owned; the agent-side loader REFUSES to load
  anything if the tree is not root-owned or is group/other-writable.
- Deny-set + slice membership are enforced in BOTH the root verifier and the
  agent loader, using the identical `runtime_deny` logic the bridge's own
  gate uses.
- The verification kernel (bridge/gate/scorecard/benchmark/usage/heldout/
  coordinator — the runtime deny-set) is never promotable.

## Rollback

Automatic: a held-out regression or a new integrity ledger event on an
active promotion rolls it back on the very next verifier pass (tree file +
manifest entry removed, `reason: "root_verified_rolled_back"` ledgered).
Manual/operator: `sudo`-edit the module's manifest entry to
`"status": "vetoed"` — the next pass rolls it back the same way. Because
`PROMOTED_TREE` is entirely separate from the installed release tree, a
rollback (automatic or manual) simply restores the pre-promotion behavior —
the installed `nanobot/runtime/*.py` copy was never touched.

## Rework after #880 (opus-review RED1/RED2)

#880 landed while this design was in-flight and confirmed the trust model
above needed two more fixes before it could be considered load-bearing.

### RED1 — the root verifier imported from an agent-owned release tree (root RCE)

#880 made the release tree **read-only to the runtime uid** via the process
sandbox (`ProtectSystem=strict` on every app lane) — but did not change the
release's **on-disk owner**, which `deploy_release.sh` still chowned
`eeepc-agent:eeepc-agent`. The root verifier does
`sys.path.insert(0, .../current)` and then `from nanobot.runtime import ...`
straight out of that tree, AS ROOT. A runtime-uid-owned release means the
runtime uid — the SAME uid the instance's subagent runs as — could plant or
mutate a module the verifier would then import with root privilege: a
straightforward root RCE, defeating the entire boundary this change exists
to build.

**Fix:**

1. `deploy_release.sh` now chowns the release `root:root`, not
   `eeepc-agent:eeepc-agent` (the `goal_text.json` state-seeding chown stays
   `eeepc-agent:eeepc-agent` — that's instance-writable state, not release
   code). The runtime uid only ever needed READ+EXEC here; #880 already
   proved no runtime write into `/opt` exists (`ProtectSystem=strict` +
   `PYTHONDONTWRITEBYTECODE=1` on every app lane), and world-read from the
   release tar/umask already gives every other uid read+exec.
2. The verifier independently proves that promise held, BEFORE it ever
   imports anything from the release:
   `_verify_release_ownership_or_exit()` resolves the release dir's realpath
   and stat-checks the release dir itself, its `nanobot/` subdir, and its
   `nanobot/runtime/` subdir — each must be `st_uid == 0` and neither
   group- nor other-writable (`mode & 0o022 == 0`). Any failure prints one
   reason to stderr and `sys.exit`s nonzero **without ever reaching** the
   `from nanobot...` import lines — fail closed, no partial import.
   Skippable only via `EEEPC_VERIFIER_SKIP_OWNERSHIP_CHECK=1`, documented
   as a test-only escape hatch (tests run as a non-root user against temp
   dirs / a real dev checkout that can never satisfy a real root-ownership
   check).

### RED2 — the verifier executed instance-influenced work as root (no privilege drop)

Three things ran as root with no privilege drop at all: `_git_show_bytes`
(a `git` subprocess against the INSTANCE-owned repo — `git` parses
repo-local config/attributes/hooks, so running it as root against a repo
the instance controls is itself an escalation surface), the microbench
measurement (`run_measurement`, which executes the candidate's own bytes),
and the held-out pack (`run_heldout`, which copies + executes instance
artifacts in a sandboxed subprocess). Root must never EXECUTE anything the
instance influenced — it may only ever receive bytes/verdicts as inert
DATA.

**Fix:**

- A demotion helper resolves `pwd.getpwnam(EEEPC_VERIFIER_DEMOTE_USER or
  "eeepc-agent")` once and builds a `preexec_fn` that does
  `os.setgroups([gid]); os.setgid(gid); os.setuid(uid)`, in that order
  (uid dropped last, or the process loses the permission needed to change
  gid/groups first). Guarded behind `os.name == "posix"` /
  `os.geteuid() == 0` checks throughout, so the module still imports (and
  the demotion is simply skipped, with an explicit stderr log line) on a
  non-root runner — tests, dev boxes, Windows.
- `git show` against `INSTANCE_REPO` now runs via
  `_git_show_bytes_demoted()` — the same `git show` invocation, wrapped
  with the demoted `preexec_fn` when running as root.
- The microbench measurement and the held-out pack now run inside a
  **demoted CHILD process**: the verifier re-invokes its own interpreter
  (`sys.executable __file__ --child-verify --mode {full,heldout}`) with
  `preexec_fn=demote` and a minimal env (PATH, HOME pointed at a
  root-created tmpdir chowned to the demoted uid, STATE_DIR/INSTANCE_REPO/
  SELFEVO_RELEASE_DIR, `PYTHONDONTWRITEBYTECODE=1`). The child re-runs the
  same sys.path bootstrap + ownership check + imports as the parent, does
  the actual `git show` / `run_measurement` / `run_heldout` calls, and
  prints exactly ONE JSON object to stdout. The root parent parses that
  JSON as untrusted data (`_run_child_verify`) — a nonzero child exit or
  unparseable stdout is folded into `{"ok": False, ...}`, which every
  caller treats as "could not verify this pass", never as a green light.
  Hard 600s timeout per child invocation.
- **The trust decision never moved into the child.** The child reports raw
  numbers (`baseline_ms`, `candidate_ms`, `heldout_clean`); the root parent
  (`_measure_candidate`) computes `improvement_pct` itself and compares it
  against the #822 `MICROBENCH_MIN_IMPROVEMENT_PCT` floor itself, exactly as
  before this rework — the child cannot forge a promotion by lying about a
  pre-computed verdict, only by lying about (or having a compromised)
  measurement of raw numbers the parent still gates on.
- `eeepc-promotion-verifier.service` gained `NoNewPrivileges=yes` (setuid
  DROP is still permitted under `no_new_privs` — this only blocks GAINING
  privilege), `PrivateTmp=yes`, `ProtectHome=yes`, `ProtectSystem=strict`,
  `ReadWritePaths=/var/lib/eeepc-promoted /var/lib/eeepc-agent`, and
  `Environment=PYTHONDONTWRITEBYTECODE=1` — the same sandbox posture #880
  gave every other app lane.

**Accepted residual risk.** The measured candidate code necessarily
EXECUTES inside the demoted measurement child — there is no way to measure
it without running it somewhere. A sufficiently adversarial candidate could
compromise that sandboxed, unprivileged child process, but the child starts
with no root access and no write access to `PROMOTED_TREE` (root-owned,
0755, files 0644 — the child never touches it). Compromising the child can
at most forge THAT child's own reported numbers for one measurement pass;
it can never gain root, never write the promoted tree directly, and the
existing soak (3 clean subsequent passes) plus the held-out pack's
invisibility (#780) bound how much a single forged pass can accomplish.

## Known limitation (honest residual)

The post-promotion microbench re-measurement (used for the regression watch)
would ideally verify the promoted module against a baseline that reflects
"the world with this promotion already active", but `heldout/microbench.py`'s
baseline is still, per its own #822 design, the currently-**installed**
product tree — a promoted-but-not-yet-shipped module is not folded into that
baseline until a product PR actually lands it in the release. This does not
weaken the trust boundary (a wrong/stale baseline can only make the gate
stricter or looser on a subsequent candidate touching the SAME module — it
cannot forge an unrelated promotion), but it means the microbench signal for
a second, stacked promotion of the same module is measured against the
pre-first-promotion baseline until the product catches up. Flagged rather
than silently accepted; fixing it is out of scope here (it requires a
product-side change to `heldout/microbench.py`'s baseline resolution, tracked
as a follow-up, not part of #875's trust-root scope).
