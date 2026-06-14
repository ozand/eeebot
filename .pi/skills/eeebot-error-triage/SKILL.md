---
name: eeebot-error-triage
description: Use when an error, regression, systemd timer hang, or git permission failure occurs on the Eee PC self-evolving agent runtime. Search the local lessons registry first, apply known fixes, or record new postmortems.
---

# EeeBot Error Triage & Lessons Skill

Use this skill when a self-evolving run fails, a systemd service is stuck, or git commits/pushes return permission errors.

## Directory & Context Hierarchy

1. **Active Repository Knowledge Base**: `lessons/errors.yaml` and human-readable Markdown cards under `lessons/errors/`.
2. **Current System State**: Check systemd unit logs, active queue states, and permission setups on the target host.

## Operational Playbook

### 1. Identify System Anomalies

Look out for these symptoms on the `eeepc` host:
- **Promotion block**: Check if coordinator status is `blocked_not_ready` (often caused by missing `SOURCE_COMMIT` file).
- **Git permissions lock**: Error containing `insufficient permission for adding an object`.
- **Systemd relative timer drift**: Timer stuck with `Trigger: n/a`.
- **Queue diagnostic leak**: Dashboard showing `CRIT` queue pressure but zero stale requests cleaned up.

### 2. Query the Error Registry

Check `lessons/errors.yaml` to match symptoms:
- `ERR-2026-06-14-001` -> Deployment missing `SOURCE_COMMIT`
- `ERR-2026-06-14-002` -> Git Database permission/ownership conflict
- `ERR-2026-06-14-003` -> Timer Stagnation via relative intervals
- `ERR-2026-06-14-004` -> Unpruned results dir/mtime mismatch

### 3. Record New Errors

If the failure does not match any existing entries, document it:
1. Generate an ID: `ERR-YYYY-MM-DD-XXX`.
2. Append a structured entry to `lessons/errors.yaml`.
3. Create a Markdown card in `lessons/errors/<ID>.md` defining:
   - **Symptom**: Precise error output or visual behavior.
   - **Root Cause**: Low-level origin (network, filesystem, permission, code logic).
   - **Fix Applied**: Executed commands or patches.
   - **Prevention**: Guardrails, automated scripts, or checks.

### 4. Cleanup and Verification

Always verify system state changes using:
- `git -c safe.directory=<path> -C <path> status` for Git tree cleanliness.
- `python3 scripts/eeebot_dashboard.py --tui` (or `curl http://100.102.243.92:8080` / `ssh eeepc ...`) to verify metrics health status is OK or WARN, not CRIT.
