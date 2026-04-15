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

Trigger the health service:

```bash
systemctl start eeepc-self-evolving-agent-health.service
journalctl -u eeepc-self-evolving-agent-health.service -n 20 --no-pager
```

Expected:
- a fresh `PASS` or `BLOCK` line
- a fresh report path

### Step 8 — re-run `nanobot status` against the same authority root

This proves the verification release can read the same live proof fields coherently.

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

## Operational Rule

Prefer this order:
1. test locally
2. package
3. unpack side-by-side
4. verify read-only against live truth
5. only then activate if activation is actually needed

This keeps the eeepc host stable while still letting Nanobot ship and prove new slices incrementally.
