# The verification chain and where it terminates

Every verifier needs a verifier. The chain either goes on forever or stops on
something unverified. This document says where this system's chain stops, so
that trust in any check here is informed rather than assumed.

It exists because of #1826, and it is the record ADR-023 points to when the
question is asked of the judges rather than of the loop.

Machine-checked by `tests/test_verification_chain.py`: the table below must
match the verifiers actually wired in, and every "loop-writable" claim is
recomputed from `MUTATION_POLICY` rather than trusted as prose.

## The loop's mutation surface, which every claim below is measured against

Authoritative source: `nanobot/runtime/mutation_policy.py`.

| | |
|---|---|
| Commit prefixes | `surfaces/`, `scripts/`, `memory/`, `lessons/`, `docs/`, `tests/`, `skills/` |
| Commit exact paths | `AGENTS.md` |
| Forbidden | `state/`, `ops/`, and the release-owned ontology files |

Two consequences worth stating before the table, because both are easy to miss:

- **`tests/` is on the surface.** The loop writes the tests that judge it.
- **`docs/` is on the surface, so this document is too.** The record of where
  the chain terminates is itself inside the region the chain fails to verify.
  That is not a reason to keep it elsewhere — it is one more instance of the
  same fact, and the test beside it is the part that does not live in prose.

## The verifiers

`blocks` means: can this mechanism, on its own, stop work from integrating?

| key | mechanism | scheduled by | verifies | reads | inputs loop-writable | blocks integration |
|---|---|---|---|---|---|---|
| `gate` | `nanobot/runtime/gate.py` | the cycle itself | mutation surface, skill hygiene, and the smoke/affected pytest run | `tests/`, the diff, `AGENTS.md` | **yes** | **yes** |
| `ci` | `.github/workflows/ci.yml` | GitHub, on a pull request | the full pytest suite | `tests/` | **yes** | operator PRs only, not the loop's own cycle |
| `enhancement_gate` | `nanobot/runtime/enhancement_gate.py` | the proposer | refuses enhancement-shaped proposals aimed at scripts nothing runs | usage evidence under `state/` | no | proposal only, never integration |
| `existence_index` | `nanobot/runtime/existence_index.py` | the proposer | semantic near-duplicate detection before a cycle is spawned | past titles and results under `state/` | no | spawn only, never integration |
| `validator_harness` | `nanobot/runtime/validator_harness.py` | `eeebot-validator-harness.timer` | runs the validator-class scripts the loop built, and consumes their findings | `scripts/` — the validators themselves | **yes** | no |
| `reflector` | `nanobot/runtime/reflector.py` | `eeebot-reflector.timer` | reflects over completed cycles and recommends | the cycle ledger under `state/` | no | no |
| `curator` | `nanobot/runtime/knowledge_curator.py` | `eeebot-knowledge-curator.timer` | curates lessons into the bounded knowledge base | `lessons/` | **yes** | no |
| `strategist` | `nanobot/runtime/strategist.py` | `eeebot-strategist.timer` | advises direction over a span | ledger, charter and funnel inputs under `state/` | no | no |
| `action_index` | `nanobot/runtime/action_index.py` | `eeebot-action-index.timer` | builds the durable per-cycle action index before prompt rotation | the cycle ledger under `state/` | no | no |
| `skill_evals` | `nanobot/runtime/skill_eval_harness.py` | `eeebot-skill-evals.timer` | held-out evaluation of skills | `skills/` | **yes** | no |
| `promotion_verifier` | `host/eeepc/systemd/eeepc-promotion-verifier.service` | `eeepc-promotion-verifier.timer` | ownership and mode of the promoted tree on the host | the host filesystem | no | no — it verifies the host, not a cycle |
| `systemd_drift_check` | `host/eeepc/systemd/eeebot-systemd-drift-check.service` | `eeebot-systemd-drift-check.timer` | that the units on the host match the units in the release | the host filesystem and the release tree | no | no |
| `daily_verdict` | `host/eeepc/systemd/eeebot-daily-verdict.service` | `eeebot-daily-verdict.timer` | judges daily loop movement vs appearance vs stalled | the cycle ledger under `state/` | no | no |
| `local_ci` | `host/eeepc/systemd/eeebot-local-ci.service` | `eeebot-local-ci.timer` | the suite, on the host, on a schedule | `tests/` | **yes** | no |

Units deliberately **not** verifiers, listed so their absence above is a
decision rather than an omission: `eeebot-host-metrics`, `eeebot-host-capabilities`,
`eeebot-archive-subagent-requests`, `eeebot-narrator`, `eeepc-self-evolving-subagent-bridge`,
`eeebot-techtree-publish`.

## Where the chain terminates

**On loop-authored tests.**

Read the `blocks integration` column against `inputs loop-writable`. Exactly one
mechanism is `yes` in both: the gate. Everything that can stop an integration
decides by running pytest, and pytest's content — `tests/` — is written by the
loop being judged.

The independent judges are real and better than most systems have. Measured on
the host, 2026-09-20:

| component | model | calls that day |
|---|---|---|
| executor — the thing judged | `un/qwen3.8-27b-gguf`, local | 720 |
| proposer | `an/gemini-3.8-flash-high` | 101 |
| reflector | `an/gemini-3.8-flash-high` | 42 |
| strategist | `an/gemini-3.8-flash-high` | 1 |

Different model, different provider, separate processes under separate systemd
units. This is not "an LLM produces and the same LLM reviews". But none of them
can stop anything. They advise, propose and curate. Conflating *independent* with
*authoritative* is what hid this gap: the system has independence without
authority, and authority without independence, and neither half covers for the
other.

### Why that termination is currently accepted

Not because it is safe. Because the alternatives cost more than the measured
harm so far, and because the two failures below were caught — by a person, which
is the part that does not scale.

A test the loop cannot write would have to be authored somewhere off the surface
and kept in agreement with code the loop *does* write. The system already has one
such population — the harness suite under `tests/` in *this* repository, which the
loop does not commit to — and the honest statement is that the boundary between
"harness test" and "loop test" is a repository boundary, not a mechanism.

### Two measured failures at exactly this point

Both are cited as evidence, not as anecdotes: each is a case where the gate ran,
passed, and certified something false.

1. **The fixture that omitted the field production always sends.** A retry test
   was green only because its fixture left out `source_artifact`. The test
   verified the code against a world that does not exist; the gate could not tell
   the difference, because the fixture is the world as far as pytest is concerned.
2. **The self-built ratchet.** The loop wrote a test forbidding the removal of its
   own `AGENTS.md` additions (#1188/#1193). A constraint the loop authored, enforced
   by the mechanism that is supposed to constrain it. The gate enforced it faithfully.

Neither was found by the chain. Both were found by a person reading the diff.

## The judge/evidence split

Independence has two halves — the judge, and the evidence the judge reads — and
this system has never applied ADR-023's rule to the judges themselves.

| judge | judge independent of the loop | evidence outside the loop's surface |
|---|---|---|
| reflector | yes — separate model, separate provider, separate unit | **yes** — the cycle ledger is written by the harness under `state/` |
| strategist | yes | **yes** — ledger, charter and funnel inputs under `state/` |
| proposer | yes | mixed — reads `state/` inputs, but its candidate pool includes loop-written `lessons/` and `skills/` |
| curator | yes | **no** — `lessons/` is written by the loop |
| validator harness | it is not a model — it executes | **no** — the validators it runs are `scripts/`, written by the loop |
| skill evals | not a model | **no** — `skills/` is written by the loop |

The reflector is the strongest position in the system: independent judge, evidence
outside the writable surface. The curator is the instructive one — a genuinely
independent judge reading evidence the defendant wrote. An independent judge does
not make the verdict independent.

## The one place the termination could cheaply be moved

**Assert the gate's pytest run against fixtures the loop did not write.**

Not a new verifier and not a test the loop cannot touch — a narrower claim: for
the small set of contracts where production's shape is known and recorded
(`source_artifact` is the worked example), derive the fixture from the recorded
production shape rather than from a literal in the test file. A fixture that omits
a field production always sends then fails to build, instead of passing green.

Estimate: one module that reads the recorded shapes, one gate assertion, and the
tests for both — comparable to #1764, which did the same kind of narrowing on a
suppression rule. It closes failure 1 above and does nothing for failure 2, which
needs a scope rule rather than a fixture rule and is already bounded by
`agents_md_scope_violations`.

Not built here. #1826 declares the termination; moving it is a separate decision
with its own measurement.
