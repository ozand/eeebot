# Trust Kernel Inventory

This document is the code-checked map of eeebot's anti-falsification and
integrity boundaries. It describes mechanisms, not a second policy source:
executable code and its tests remain authoritative.

## Kernel boundary and new-module rule

The kernel manifest is `nanobot/runtime/runtime_deny.py`:
`_RUNTIME_DENY_ALWAYS_FILES`, `_RUNTIME_DENY_TOKENS`, and the held-out directory
prefix deny future or operator-selected runtime-slice modules from mutating or
loading the trust boundary. The deny set is enforced by the bridge classifier,
the root promotion verifier, and the promoted overlay loader.

Every new anti-falsification mechanism **must ship as its own module**, normally
under about 300 LOC and stdlib-only where practical, and must be added to the
deny-set. Extraction of existing embedded machinery is opportunistic when its
host file is already being changed. The gate extraction is separately tracked;
do not use this rule to widen that scope.

## Deny-set manifest

The explicit immutable files currently listed in
`runtime_deny._RUNTIME_DENY_ALWAYS_FILES` are:

```text
nanobot/runtime/bridge.py
nanobot/runtime/promotion.py
nanobot/runtime/coordinator.py
nanobot/runtime/scorecard.py
nanobot/runtime/benchmark_evidence.py
nanobot/runtime/usage_evidence.py
nanobot/runtime/promoted_overlay.py
nanobot/runtime/runtime_deny.py
nanobot/runtime/evolution_tree.py
nanobot/runtime/hypothesis_verdict.py
nanobot/runtime/tech_tree.py
nanobot/runtime/skill_fitness.py
nanobot/runtime/skill_eval_harness.py
nanobot/runtime/model_registry.py
nanobot/runtime/knowledge_curator.py
nanobot/runtime/context_compaction.py
nanobot/runtime/goal_gap_futility.py
nanobot/runtime/skill_candidate_mining.py
nanobot/runtime/strategist.py
```

The basename token deny family is:

```text
gate, precheck, promotion, approval, safety, security, stop_guard,
bench, scorecard, overlay, deny, usage, evidence, heldout
```

The structural denied subtree is `nanobot/runtime/heldout/`.
The sync test is `tests/test_trust_kernel_manifest.py`; it parses this fenced
manifest and compares it to the code constant, preserving an exact-set check.

## Attack-class map

| Attack / integrity risk | Closing mechanism | Code pointer | Tests / origin |
|---|---|---|---|
| Mutation-surface escape | Exact blocked paths, blocked filename two-tier rule, allowed-surface validation, runtime deny set | `nanobot/runtime/bridge.py:_BLOCKED_EXACT_PATHS`, `_is_blocked_filename`, `_validate_mutation_surfaces`, `_classify_mutation_surface`; `nanobot/runtime/runtime_deny.py:_is_runtime_deny` | `tests/test_runtime_slice.py`, `tests/test_bridge_*`; #812/#875 |
| Gate-chain rollback laundering | Skip reasons excluded from recent failure history; `out_of_band_main_detected` is included in the exclusion set | `nanobot/runtime/bridge.py:_SKIP_ROLLBACK_REASONS`, `_recent_failure_match` | bridge failure/dedup tests; #977/#982 |
| Decay reward farming | Decay eligibility requires harness-observed use, protects contracted paths, and splits decay integrations out of fitness | `nanobot/runtime/usage_evidence.py:stale_artifacts`, `_decay_protected_paths`; `nanobot/runtime/scorecard.py:_loop_section` | `tests/test_usage_evidence.py`, `tests/test_scorecard.py`; #761/#800/#809 |
| Birth-use inflation | Newly created artifacts are not confirmed by birth-only use; creation timestamp and grace window are enforced | `nanobot/runtime/validator_harness.py:_in_birth_window`, `run_validator_harness`; `nanobot/runtime/usage_evidence.py:stale_artifacts` | validator/usage tests; #800/#802 |
| Output/reference freshness forgery | Output evidence must be fresh relative to the script; references are corroborated by independent sidecars | `nanobot/runtime/usage_evidence.py:_output_signal`, `_sidecar_corroborates_use`, `confirm_serves` | `tests/test_usage_evidence.py`; #929/#1004 |
| Reference-signal forging | Harness/usage sidecar corroboration and guarded reference index prevent a named reference from becoming proof by itself | `nanobot/runtime/usage_evidence.py:_reference_index`, `_sidecar_corroborates_use` | usage evidence tests; #789/#929 |
| Benchmark forgery | Allowlisted benchmark schema, trust environment, harness-owned before/after scorecard history corroboration | `nanobot/runtime/benchmark_evidence.py:validate_benchmark`, `verify_benchmark`, `has_valid_benchmark` | `tests/test_benchmark_evidence.py`; #819/#822 |
| Fitness-sidecar tamper | Spawn-boundary hashes cover `FITNESS_SIDECARS`; foreign writes are detected and repaired/recorded by usage confirmation | `nanobot/runtime/scorecard.py:FITNESS_SIDECARS`, `fitness_sidecar_hashes`; `nanobot/runtime/bridge.py` integrity pre/post hash path | scorecard/bridge integrity tests; #789/#939 |
| Validator-sidecar forgery | Harness owns verdict records, caps output, validates markers and runs; demand trusts only harness-shaped records | `nanobot/runtime/validator_harness.py:_run_one`, `_terminal_stderr_line`, `_parse_findings_count`, `run_validator_harness`; `nanobot/runtime/demand.py:_validator_defect_items` | `tests/test_validator_harness.py`, `tests/test_demand.py`; #925/#934 |
| Validator decay misclassification | Runtime output declaration `harness_contract=decay_declared` is classified before ordinary non-zero-exit defects | `nanobot/runtime/validator_harness.py:_run_one`, `run_validator_harness`; `nanobot/runtime/demand.py:_validator_defect_items` | decay/harness demand tests; #936/#1002 |
| Harness silent failure | Whole-invocation failures and no-run/no-record conditions exit non-zero instead of appearing healthy | `nanobot/runtime/validator_harness.py:main`, `run_validator_harness` | harness exit tests; #937/#995 |
| Runtime-tier smuggling | Tier classification plus promotion candidates; runtime trust ladder and root verifier prevent auto-integration | `nanobot/runtime/bridge.py:_classify_mutation_surface`, `_record_runtime_slice_candidate`; `nanobot/runtime/runtime_deny.py:effective_runtime_slice`; `nanobot/runtime/promoted_overlay.py:_boundary_ok` | runtime slice, verifier, overlay tests; #812/#875/#876 |
| Curator worktree race | Curator writes state-only staging payloads; bridge picks them up on clean main and commits atomically | `nanobot/runtime/knowledge_curator.py:_stage_promotions`, `run_curation`, `load_staged_manifest`; `nanobot/runtime/bridge.py:_pickup_staged_promotions` | `tests/test_knowledge_curator.py`, `tests/test_curator_staging_pickup.py`; #1001/#1012/#1015 |
| Goal-gap futility farming | Tracks gap progress and suppresses gaps that repeatedly produce no improvement | `nanobot/runtime/goal_gap_futility.py:_update`, `futile_gap_ids`, `futility_snapshot`; `nanobot/runtime/demand.py:_goal_gap_items` | `tests/test_goal_gap_futility.py`; #996 |
| Action-sequence/skill-candidate evidence abuse | Deterministic recurrence mining from normalized action index, with existing-skill matching and bounded candidates | `nanobot/runtime/skill_candidate_mining.py:mine`, `_existing_skill_match`; `nanobot/runtime/demand.py:_skill_candidate_items` | `tests/test_skill_candidate_mining.py`; #1006 |
| Reflector steering contaminating fitness | Reflector writes only `state/reflector/`; recommendations enter normal demand, and no scorecard path reads reflections | `nanobot/runtime/reflector.py:run_reflector`, `_append_journal`; `nanobot/runtime/demand.py:_reflection_items`; scorecard has no reflector input | `tests/test_reflector.py`; #1007 |
| Demand served-defect priority forgery | Harness-served validator records are ordered before unserved records; instance-authored text cannot create the served signal | `nanobot/runtime/demand.py:_validator_defect_items` (served-map ordering) | demand validator-priority tests; #933/#1003 |
| Repeat-failure metric laundering | Only `recent_duplicate_failure` skips and `self_dedup` rejects enter `repeat_failure_rate`; healthy skips and steering-only records do not | `nanobot/runtime/scorecard.py` module contract and `_loop_section` | `tests/test_scorecard.py`; #977/#980/#983 |
| Context/identity steering confusion | Product and instance `AGENTS.md` are distinct; steering text is not trusted as fitness or gate evidence | `AGENTS.md`, `CONSTITUTION.md`, runtime prompt construction and harness trust boundaries | import/hygiene and harness tests; #619/#789 |

## Deliberate reader asymmetry

The product-repo `AGENTS.md` governs developers and tooling. The instance-repo
`AGENTS.md` is self-maintained working guidance for the loop. They are two
different files and must never be conflated.

- **Proposer (think):** reads the goals charter, demand, and guardrails to
  select one task. Rationale: task selection needs trusted operator goals and
  current demand, not a full executor workspace.
- **Executor (hands):** reads the goals charter, instance `AGENTS.md`, skills,
  and memory while operating tools. Rationale: execution needs workspace
  guidance, but those instance-authored texts are steering only, never proof.

Neither proposer nor harness treats instance-authored `AGENTS.md`, skills, or
other mutable text as an integrity/fitness signal.

## What the kernel does not claim

Reflector findings, curator facts, demand recommendations, and action-mining
candidates are steering inputs. They are not scorecard inputs. The kernel
prevents untrusted loop-authored material from becoming authority; it does not
claim that every steering suggestion is correct.
