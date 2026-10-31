# Change: Reflector attempt correlation

- **change-id:** 2034-reflector-attempt-correlation
- **issue:** #2034
- **capability:** `docs/specs/self-evolving-runtime`
- **role / workstream:** runtime / reflector

## Problem

The reflector currently chooses a prompt by maximum sequence across participants, although sequence counters are per component. A planner transcript can therefore be analyzed as executor work.

## Intended change

Bind prompt telemetry to each executor spawn, persist the bridge's authoritative primary-or-successful-repair selection privately, and let the reflector consume only its exact prompt. Missing, ambiguous, legacy, or expired evidence is unavailable; no fallback transcript is analyzed. The sidecar expires and is pruned with its selected prompt.

## Acceptance

- [ ] Synthetic producer-to-consumer tests prove attempt identity, authoritative repair selection, and safe provenance.
- [ ] Missing, malformed, ambiguous, cross-cycle, legacy, and expired correlation never falls back to another prompt.
- [ ] Prompt writing, resolution, reading, and pruning use one resolved telemetry root and preserve bounded private retention.
- [ ] Focused tests, lint, import hygiene, and Lat documentation checks pass.

## Out of scope

No host access or deployment claim, public attempt IDs, new store/framework/dependency, generic correlation layer, or assertion about post-deployment mismatch rates.
