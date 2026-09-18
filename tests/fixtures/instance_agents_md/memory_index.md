# Memory index

## Facts (memory/facts/)

* [Identity](facts/identity.md) - Identity fact
* [Write target: ozand/eeebot-self-evolving](facts/write-target.md) - Write target: ozand/eeebot-self-evolving fact
* [DO NOT touch](facts/do-not-touch.md) - DO NOT touch fact
* [How each session should go](facts/session-flow.md) - How each session should go fact
* [Rules](facts/rules.md) - Rules fact
* [Key paths on host](facts/key-paths.md) - Key paths on host fact
* [Release promotion metadata](facts/release-promotion-metadata.md) - SOURCE_COMMIT metadata is required for release promotion
* [Git database permissions](facts/git-database-permissions.md) - Shared repository permissions must be configured explicitly
* [Systemd timer scheduling](facts/systemd-timer-scheduling.md) - Timers should account for clock drift

## Working memory (memory/)

* [MEMORY.md](MEMORY.md) - Active working-memory notes (current priority items)
* [MEMORY_ARCHIVE.md](MEMORY_ARCHIVE.md) - Archived weekly memory summaries

## Discipline & protocol facts (memory/)

* [HISTORY.md](HISTORY.md) - append-only log; read its last N lines with offset/limit
* [Auxiliary tool-call discipline](auxiliary_tool_call_discipline.md) - Suppress secondary tool calls once target-file inspection proves redundancy
* [Targeted script inspection](targeted_script_inspection.md) - Line-range slicing and ast/symbol query practices for inspecting large scripts without blowing prompt context
* [Standalone validator registration](standalone_validator_registration.md) - How validators register into the test runner (STANDALONE_VALIDATORS / INTEGRITY_VALIDATORS) and how to inspect the runner's registration hooks quickly
* [Direct target-file edit transition](direct_edit_discipline.md) - Transition from reading the target file to editing it within the next 2-3 tool calls to avoid exhausting the budget without a single edit
* [Turn budget pacing & early edit checkpoints](turn_budget_pacing.md) - Actionable pacing thresholds for 80-iteration cycles: when to stop inspecting, when the first edit must land, and when to stage partial work early
* [Blocked-task early exit](blocked_task_early_exit.md) - Recognize blocked or unnecessary edits early and immediately emit the structured final response without trailing confirmation scans
* [Utility-script CLI extension & test pairing](utility_script_test_pairing.md) - Protocol for pairing a script CLI extension with a companion pytest suite so the change is standalone-safe and passes the gate
* [Operational lesson cross-referencing standards](operational_lesson_standards.md) - Conventions for cross-referencing AGENTS.md rules, memory cards, and skill paths in operational lessons to keep policy-to-implementation traceability complete
* [Compound task differential scoping](compound_task_scoping.md) - Inspect existing interfaces before modifying code and scope edits strictly to missing capabilities during compound enhancement tasks
* [Research phase batching patterns](research_phase_batching.md) - Concrete composite tool execution rules (batch_grep passes, chained exec, bounded reads) that keep the research phase to 3-5 calls before the first edit
* [Inline assertion testing standards](regex_verification_standards.md) - Rules for writing and running inline assertion blocks (positive, negative, boundary cases) that verify documented regex patterns and verification snippets
* [Harness execution coupling](harness_execution_coupling.md) - How modified scripts couple to harness execution verification: the pycache/output signal chain, freshness rules, and confirmable-vs-confirmed definitions
* [Pre-proposer existence gating & deduplication](pre_proposer_existence_gating.md) - Existence gating (disk/git/inventory) and deduplication (demand/target/failure-signature) heuristics that block duplicate cycle proposals before dispatch
* [Runner heartbeat telemetry & feed freshness](runner_heartbeat_telemetry.md) - How the host-metrics runner touches feed/heartbeat/history files, how staleness is detected and refreshed, and how to keep host-metrics token integration bounded

* [AGENTS.md structural assertions](facts/agents-instruction-assertions.md) - Standing instructions in AGENTS.md must be asserted in tests/test_agents_structure.py

* [Bounded text inspection](facts/bounded-text-inspection.md) - Safe text reading utilities return structured truncation metadata

* [CLI positional arguments with standalone flags](facts/cli-positional-optional-for-standalone-flags.md) - Define positional arguments with nargs='?' when CLI scripts support standalone flags

* [CLI stderr/JSON separation](facts/cli-stderr-json-separation.md) - CLI tools with JSON output must route logs to stderr

* [Daemon Runtime Paths](facts/daemon-runtime-paths.md) - Path isolation rules for systemd service units and workspaces

* [Doc code block placeholder normalization](facts/doc-code-block-placeholder-normalization.md) - Normalizing instructional angle-bracket placeholders prevents false-positive syntax check failures

* [Preserving line offsets in doc syntax validation](facts/doc-snippet-syntax-line-offset.md) - Replace stripped lines with blank lines to preserve markdown code block line offsets during syntax checks

* [Error markdown card git staging](facts/error-card-gitignore-handling.md) - Force-add ERR-*.md markdown cards to bypass .gitignore during lesson updates

* [Markdown structural testing](facts/markdown-structural-testing.md) - AGENTS.md structural tests assert sections, length bounds, and keywords

* [Nested TestSuite traversal](facts/nested-testsuite-traversal.md) - Recursive traversal is required when processing hierarchical test suites from unittest discovery

* [Queue Cleanup Hygiene](facts/queue-cleanup-hygiene.md) - Pruning scope parity and timestamp fallback rules for subagent queues

* [Script entrypoint scaffolding](facts/script-entrypoint-scaffolding.md) - Standard CLI entrypoint and companion test scaffolding via scripts/scaffold_script_entrypoint.py

* [Skill validation testing](facts/skill-validation-testing.md) - Skill Markdown and Frontmatter Test Suites fact

* [Subagent bridge deployment sync](facts/subagent-bridge-deployment.md) - Libexec wrapper script synchronization with canonical version

* [Subagent Timeout Alignment](facts/subagent-timeout-alignment.md) - Rules for aligning subagent timeouts below coordinator stale thresholds

* [Subprocess cwd absolute paths](facts/subprocess-cwd-absolute-paths.md) - Always pass absolute script paths when executing subprocess calls with custom working directories

* [POSIX subshell compatibility](facts/subshell-posix-compatibility.md) - Subshell commands must avoid non-POSIX builtins like disown

* [Workspace validation helpers](facts/workspace-validation-helpers.md) - AST symbol existence and task satisfaction verification helpers in scripts/workspace_validation_helpers.py

* [Pytest test runner invocation](facts/pytest-runner-invocation.md) - Repository test suites using pytest conventions must be executed via pytest rather than unittest

* [Base branch test failure isolation](facts/base-test-failure-isolation.md) - scripts/verify_and_proof.py helper isolates pre-existing base branch failures from cycle regressions

* [Feed cache preflight validation gate](facts/feed-cache-preflight-gate.md) - scripts/check_stale_feeds.py preflight_feed_cache re-evaluates staleness after executing refresh actions

* [CLI pytest suite execution timeouts](facts/cli-test-suite-timeout.md) - Subprocess-heavy test suites require explicit tool timeout overrides

* [Unrelated test failure notes](facts/unrelated-failure-notes.md) - scripts/verify_and_proof.py builds structured notes for unrelated base branch test failures

* [Host metrics heartbeat timestamp](facts/host-metrics-heartbeat-timestamp.md) - scripts/collect_host_metrics.py embeds heartbeat_touched_at in JSON payloads for in-band freshness checks

* [Validate open encoding binary mode filtering](facts/validate-open-encoding-binary-mode.md) - scripts/validate_open_encoding.py excludes binary mode open calls from encoding requirements

* [Style validator execution modes](facts/style-validator-execution-modes.md) - scripts/check_style.py provides a --fast flag to avoid AST multi-pass timeouts

* [Decay and archival preflight inspection](facts/decay_archival_policy.md) - Safety checklist requiring git commit history and do-not-touch policy inspection prior to executing script decay or archival demands

* [Markdown catalog link integrity tests](facts/markdown-catalog-link-integrity.md) - Relative path verification in markdown indexes requires stripping anchor fragments and URI schemes
