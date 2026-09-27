# eeepc Deploy, Verify, and Rollback Runbook

Last updated: 2026-04-16 UTC

## Purpose

This runbook defines the canonical safe workflow for:
- building a Nanobot release from the local repo
- transferring it to `eeepc`
- unpacking it side-by-side as a verification release
- verifying it without switching the active runtime immediately
- optionally promoting it to the active pinned runtime
- rolling back safely if needed

The workflow intentionally prefers proof before activation.

## Canonical Paths

Local repo:
- `/home/ozand/herkoot/Projects/nanobot`

eeepc pinned runtime base:
- `/home/opencode/.nanobot-eeepc/runtime/pinned`

Active pinned runtime symlink:
- `/home/opencode/.nanobot-eeepc/runtime/pinned/current`

Live eeepc self-evolving authority root:
- `/var/lib/eeepc-agent/self-evolving-agent/state`

Verification runtime usage pattern:
- `PYTHONPATH=/home/opencode/.nanobot-eeepc/runtime/pinned/<release-id>`

## Release Workflow

### Step 1 — ensure local repo is clean

```bash
git -C /home/ozand/herkoot/Projects/nanobot status --short
git -C /home/ozand/herkoot/Projects/nanobot rev-parse --short HEAD
```

Expected:
- working tree clean
- commit id known

### Step 2 — run focused verification locally

Minimum expected checks before packaging:

```bash
python3 -m pytest tests/test_commands.py tests/test_runtime_coordinator.py -v
```

Add more focused tests if the slice touched additional surfaces.

### Step 3 — build release archive

```bash
git -C /home/ozand/herkoot/Projects/nanobot archive --format=tar.gz -o /tmp/nanobot-<commit>.tar.gz HEAD
sha256sum /tmp/nanobot-<commit>.tar.gz
```

### Step 4 — copy archive to eeepc

```bash
scp -F /home/ozand/.ssh/config \
  -i /home/ozand/.ssh/id_ed25519_eeepc \
  -o IdentitiesOnly=yes \
  /tmp/nanobot-<commit>.tar.gz \
  eeepc:/tmp/nanobot-<commit>.tar.gz
```

### Step 5 — unpack into side-by-side verification release

Suggested release id shape:
- `YYYYMMDD-HHMM-<commit>`

Example:

```bash
sudo mkdir -p /home/opencode/.nanobot-eeepc/runtime/pinned/20260416-0312-cffb77d
sudo tar -xzf /tmp/nanobot-cffb77d.tar.gz \
  -C /home/opencode/.nanobot-eeepc/runtime/pinned/20260416-0312-cffb77d
sudo chown -R opencode:opencode /home/opencode/.nanobot-eeepc/runtime/pinned/20260416-0312-cffb77d
```

Verification release rule:
- do not switch `current` yet
- verify first using `PYTHONPATH`

## Verification Workflow

### Non-sudo readiness boundary

If the current operator context can SSH to `eeepc` but cannot run `sudo`, cannot access `/home/opencode`, or cannot read protected authority indexes, this runbook can only establish rollout readiness and partial live proof.

In that mode, record the exact blockers and do not claim host-emitter parity. The known fail-closed signals are:
- `sudo -n true` returns `a password is required`
- direct SSH as `opencode` or `root` is denied
- `/home/opencode/.venvs/nanobot/bin/nanobot` is inaccessible
- `/var/lib/eeepc-agent/self-evolving-agent/state/outbox/report.index.json` or `goals/registry.json` are unreadable

A non-sudo operator may still preserve limited live proof from the newest readable `reports/evolution-*.json`, but activation and authoritative parity verification remain privileged steps.

### Step 6 — run read-only verification against live host truth

Use the verification release without switching the active runtime:

```bash
sudo env PYTHONPATH=/home/opencode/.nanobot-eeepc/runtime/pinned/<release-id> \
  /home/opencode/.venvs/nanobot/bin/nanobot status \
  --runtime-state-source host_control_plane \
  --runtime-state-root /var/lib/eeepc-agent/self-evolving-agent/state
```

Expected proof fields:
- `Runtime state source: host_control_plane`
- `Runtime state root: /var/lib/eeepc-agent/self-evolving-agent/state`
- live status
- active goal
- approval state
- report source
- outbox source
- artifact paths when present

### Step 7 — if needed, run a supervised PASS proof

Write a short-lived apply gate:

```bash
python3 -c "import json,time,pathlib; p=pathlib.Path('/var/lib/eeepc-agent/self-evolving-agent/state/approvals/apply.ok'); p.parent.mkdir(parents=True, exist_ok=True); p.write_text(json.dumps({'expires_at_epoch': int(time.time())+3600}, indent=2))"
```

Trigger the subagent bridge (the former coordinator/health service was
decommissioned in #900/#910 — it never made an LLM call in production; the
bridge is the live driver of the self-evolving loop):

```bash
systemctl start eeepc-self-evolving-subagent-bridge.service
journalctl -u eeepc-self-evolving-subagent-bridge.service -n 20 --no-pager
```

Expected:
- a fresh `PASS` or `BLOCK` line
- a fresh report path

### Step 8 — re-run `nanobot status` against the same authority root

This proves the verification release can read the same live proof fields coherently.

### Step 8b — precondition for any release at or after #1300 (strict executor prompt)

Since eeebot #1300 the executor's system prompt is built strict: only instance
`AGENTS.md` sections marked `<!-- prompt-fit: droppable -->` may be dropped
under the 24,000-char cap, and a prompt that cannot hold every unmarked
section fails the cycle with exit status 4 (`EXIT_SYSTEM_PROMPT_OVERFLOW`).
`deploy_release.sh` classifies 4 as a genuine activation failure and rolls the
release back, naming the cause in its output.

Before deploying such a release, confirm the host's instance workspace already
carries the markers (ozand/eeebot-self-evolving#186 or later):

```bash
ssh ozand@eeepc-lan "sudo grep -c 'prompt-fit: droppable' \
  /var/lib/eeepc-agent/self-evolving-agent/eeebot-self-evolving/AGENTS.md"
# expected: 13 (or more); 0 means the deploy will roll itself back
```

Order of operations when both land together: merge the instance markers PR,
wait one bridge cycle (the bridge fetches the instance `origin/main` at cycle
start), re-run the check above, then deploy. The alternative lever —
`NANOBOT_SYSTEM_PROMPT_MAX_CHARS` in the bridge env chain — is an operator
decision recorded on #1300, not a default.

## Activation Workflow (optional)

Only do this if the verification release must become the active pinned runtime.

### Step 9 — switch the active symlink

```bash
sudo ln -sfn \
  /home/opencode/.nanobot-eeepc/runtime/pinned/<release-id> \
  /home/opencode/.nanobot-eeepc/runtime/pinned/current
```

### Step 10 — restart the active service if that slice requires service activation

If the active gateway runtime must change, restart the service that actually uses `current`.
Use the known wrapper/service path for the `opencode` runtime.

Important rule:
- do not restart blindly
- first confirm the service/unit that actually loads `PINNED_RUNTIME_SOURCE=current`

## Rollback Workflow

### Step 11 — rollback rule

If activation causes startup failure or runtime incompatibility:
- immediately switch `current` back to the last known-good pinned release
- restart the affected service
- verify health before any further diagnosis

Canonical rollback principle:
- restore host service first
- debug second

### Step 12 — preserve failed verification release for forensics

Do not immediately delete the failed verification directory.
Keep it for:
- diff inspection
- dependency diagnosis
- reproducibility notes

## Pre-Deploy Checklist

Before building a release:
- [ ] local repo clean
- [ ] exact commit chosen
- [ ] focused tests green
- [ ] host-compatibility concerns reviewed
- [ ] slice scope bounded and reversible

## Post-Unpack Verification Checklist

Before activation:
- [ ] release unpacked into new side-by-side directory
- [ ] ownership fixed to `opencode:opencode`
- [ ] verification command runs via `PYTHONPATH`
- [ ] live authority status output looks coherent
- [ ] if needed, supervised PASS proof completed

## Post-Activation Checklist

If activation occurred:
- [ ] active symlink points to expected release
- [ ] service starts successfully
- [ ] no new fatal startup errors
- [ ] live authority status still coherent
- [ ] rollback target identified and ready

## Release Window: Forecast, Check, External Review

Every behaviour release gets a 24 h window, measured from the flip time of `current`. The release issue records the window's four steps in order. (Architect decision, 2026-09-27; first applied to R1, #1993.)

### Before the flip — forecast

Publish the forecast in the release issue before activation. Each line states:

1. the change it tests;
2. its **instrument**: the exact rendered string or ledger/telemetry field, with the `file:line` of the writer at the release sha. Name the text the reader actually sees, not an internal enum name;
3. its **denominator**, for example per success cycle, per unit start or per outcome row, never "per day";
4. whether the writer is **live on the host now**;
5. the **baseline** read on the host before the flip;
6. the expected result.

Rules for the reader:
- **Rotating streams:** every count reads the live file plus the rotated archives that cover the window. That means `state/ledger/cycles.jsonl` + `cycles-<day>.jsonl.gz`, and `state/bridge/runs.jsonl` + `runs-<day>.jsonl.gz`. This applies to the baseline and to the check. Reading the live file alone undercounted by 4× on 2026-09-27.
- **The flip cycle:** exclude the cycle running at the flip, and name it by `cycle_id` or run id in the flip comment. Count only cycles first started after the flip.
- **A guard line:** at least one line must catch harm, not only the intended effect. Examples: unit failures per start from the unit journal; the wall-clock-abort share with a stop threshold.
- **Changes with no forecast line:** list any change whose trigger is unlikely within 24 h under "no forecast line", with the reason.

Before the flip, the gate precheck runs the release sha against live state: `verify_release_health.py`, with statuses printed per dimension.

**The gate does not enforce health.** `verify_release_health.py` checks imports, rendering and the structure and bounds of fields. It exits 0 whatever `overall` or any dimension status is, so a passing gate is not a health verdict.

The health stop is an **operator step**. Whoever runs the release compares the per-dimension statuses printed by the precheck and records the comparison in the release issue before activating:
- **Retired sources** (`reward`, `gate`): WARN by construction.
- **`cpu` and `queue`:** read against the load at that moment.
- **Any other dimension:** a new WARN or CRIT, compared with the live baseline, means the operator does not activate.

Automating this comparison inside `--verify-only` is a separate follow-up; until it lands, the release issue must show the comparison.

### At the flip — flip comment

Record the flip time and `SOURCE_COMMIT`, the activation self-check result, the publisher drop-ins, and the flip cycle.

### At +24 h — forecast check

Check every line with its own instrument. Before accepting a "broken" verdict, reproduce the count from the writer's literal output and confirm the reader covered the archives. A miss is one of three kinds, and the check names which:
- a code defect;
- a wrong forecast: volume, premise or instrument;
- no qualifying event in the window.

### After the check — external diary review

The architect commissions a ChatGPT review (skill `chatgpt-github-review`, one job at a time). ChatGPT reads the instance repo's public `diary/` for the window, pinned to the instance sha at the window's end. Then the architect reconciles each claim with private host data ChatGPT cannot see: the ledger with archives, `llm_calls`, `bridge/runs`. Each claim is tagged confirmed, refuted or unverifiable. The result goes to the release issue, and findings go to their owners.
- The prompt carries only public material: diary paths, instance sha, public repo code. Never goal text, host prompts or responses, or env values.
- The diary records intentions, not a result log. The reconciliation weighs host data over diary numbers.
- On 2026-09-27 the pairing worked in both directions. ChatGPT's "duplicates are caught too late" became, on host data, "duplicates cost 2 calls each; service-only cycles cost 23% of all calls". ChatGPT's validator finding reproduced (#2004). The architect's own unrotated-ledger error surfaced (#1976 correction).

## Operational Rule

Prefer this order:
1. test locally
2. package
3. unpack side-by-side
4. verify read-only against live truth
5. only then activate if activation is actually needed

This keeps the eeepc host stable while still letting Nanobot ship and prove new slices incrementally.
