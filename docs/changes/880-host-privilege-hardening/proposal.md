# #880 — Host privilege hardening: enforce the boundary the trust kernel assumes

## The finding (live-host audit, 2026-08-17)

The verification kernel (#603 immutable core, #789 non-forgeable fitness sidecars,
#813/#819/#822 benchmark trust) is designed as if the instance were adversarial
and could not run privileged code. On the real eeepc that assumption did not hold:

- **The subagent-bridge lane ran as ROOT** — `eeepc-self-evolving-subagent-bridge.service`
  had no `User=` (confirmed `_UID=0`), `ProtectSystem=no`, `NoNewPrivileges=no`, and
  no privilege drop anywhere in `bridge.py`.
- **The release tree was writable by the runtime uid** — `/opt/eeepc-agent/.../current/`
  was `eeepc-agent:eeepc-agent` (664); the coordinator/reflection lanes ran as
  `eeepc-agent` with `ReadWritePaths=/opt/eeepc-agent`.
- The bridge lane executes instance-authored code every cycle (smoke =
  `py_compile`+`pytest` on the instance repo; microbench/heldout subprocesses strip
  env but did not drop uid).

Consequence: instance-authored code ran at root, with write access to the harness's
own code and fitness sidecars, every cycle — defeating the entire kernel before any
gate verdict. Not exploited (the instance intelligence is a controlled gateway LLM),
but #874's RSI autonomy plan treats the loop adversarially, so the boundary had to
become real before any auto-integration of instance-authored runtime code (#875).

## The fix

The only privilege boundary on this host is root (deploy runs via sudo). Rather than
add a signing key (useless when harness and instance share a uid), enforce the
boundary with the process sandbox:

1. **No lane runs as root.** The bridge lane now runs `User=eeepc-agent` like the
   coordinator. (`approval-keeper`, referenced in the bridge `After=`, does not exist
   on the host — the empty `User=` it reported was a non-existent-unit default.)
2. **The release tree is read-only to the runtime uid**, enforced by
   `ProtectSystem=strict` + `ReadWritePaths=/var/lib/eeepc-agent` on every app lane
   (bridge, agent kick, health/coordinator driver, strong-reflection). `/opt` is now
   `Read-only file system` inside the service sandbox regardless of on-disk ownership —
   this is what actually enforces #603. `PYTHONDONTWRITEBYTECODE=1` stops `.pyc`
   writes into the now-read-only tree.

The runtime writes only its own state and instance repo, both under
`/var/lib/eeepc-agent` — verified: config (`/opt/.../.nanobot/config.json`) is static
and read-only; the bridge lock is `state/bridge.lock` (under `/var/lib`); no runtime
`/opt` writes exist beyond `.pyc`.

## Rollout & verification (2026-08-17)

- Backed up every live unit (`*.bak-20260817-880`).
- `chown -R eeepc-agent:eeepc-agent /var/lib/eeepc-agent/self-evolving-agent` to clear
  ~9,500 root-owned artifacts left by the root-era bridge (so the non-root bridge can
  rewrite them).
- Applied the hardened units, `daemon-reload`.
- Verified: bridge real cycle `Result=success` as `_UID=999`, no permission/EROFS
  errors; health (coordinator driver) cycle success under strict; a full dry
  `app.main` canary exited 0 under strict with no `/opt` write; `/opt` confirmed
  `Read-only file system` inside the sandbox; both timers active (loop alive).

## Known-orthogonal note

`eeepc-strong-reflection.service` fails on missing `LITELLM_API_KEY` — a pre-existing
config gap (its unit loads only optional `-` EnvironmentFiles and never wires
`litellm.env`), independent of this change (the failure predates strict; env loading
was untouched). Tracked separately if the reflection lane is to be revived.

## Unblocks

#875 (auto-promotion) can now rely on a real boundary: a root (or separate-uid)
verifier can import a root-owned release snapshot and re-measure with a
privilege-dropped child, writing a root-owned promoted tree the agent reads read-only.
