---
title: Validator harness disk-spool parse budget
status: accepted
date: 2026-09-05
authors: [eeebot maintainers]
related: ["#1320", "#1321", "#1208", "#928", "#934"]
tags: [runtime, validator-harness, demand, reliability]
---

# Status

Accepted — implemented for #1320.

# Context

`nanobot/runtime/validator_harness.py`'s `_MAX_OUTPUT_BYTES` (64 KiB) caps
captured subprocess stdout in memory, enforced during capture (#928) so a
runaway/malicious printer is bounded by this cap, not by the unit's
`MemoryMax=512M` (an OOM kill). That purpose is unchanged by this ADR.

Source inspection of all 53 allowlisted validators (`scripts/(check|validate|
audit|analyze|verify)_*.py` in the instance repo) classified them by output
growth shape: 21 emit an unconditional per-file JSON row and 6 more are
sub-domain-linear (bounded by a named subset, still growing with it) — 27/53
structurally growing relative to the fixed 64 KiB cap. Three measured live
against the host already exceed it: `validate_filename_compatibility.py`
(2.6x), `check_style.py` (2.8x), `verify_imports.py` (33x). Truncation at
exactly `_MAX_OUTPUT_BYTES` cuts valid JSON mid-document
(`json.JSONDecodeError: Unterminated string...`), which `_classify_findings`
reports as `findings_parse: "not_json"`. For a script that also exits 0 (no
other signal), this was **silent**: `findings_count` `None`, no demand, no
error, nothing recorded anywhere says the cap did it beyond the diagnostic
`stdout_truncated` flag, which nothing consumed as a defect.

27/53 growing structurally with repository size means this is a document-size
problem affecting a whole validator class, not a handful of instance scripts —
a class-wide harness fix, not N per-script edits (same reasoning #1208 step 2
already used for the `failed_count` key: one read site here vs. rewriting
every script).

# Decision

Add a **second, separate, larger** budget — `_MAX_PARSE_BYTES = 4 MiB` — spooled
to disk rather than held in memory. 4 MiB is 1/128 of the validator-harness
systemd unit's own `MemoryMax=512M`
(`host/eeepc/systemd/eeebot-validator-harness.service`), chosen as a small,
conservative fraction: comfortably bounded against the same runaway-printer
threat `_MAX_OUTPUT_BYTES` defends against, while covering every measured live
validator (`verify_imports.py`'s 2.18 MB is under half of it).

`_MAX_OUTPUT_BYTES` is **unchanged** — same value, same purpose (in-memory
evidence: `stdout_truncated`, decay-declaration detection, sandbox-denial
detection, `stderr_tail`). A new reader thread
(`_drain_stdout_spooled`) drains the child's stdout pipe into BOTH sinks
simultaneously — the existing 64 KiB in-memory head, and a disk-spooled file
capped at `_MAX_PARSE_BYTES` — continuing to read (and discard) past both caps
so an undrained pipe never blocks the child (same discipline #928 established
for the single existing cap). `_classify_findings` is called against the
**spooled** content, not the 64 KiB head, whenever the true total bytes read
did not exceed `_MAX_PARSE_BYTES` — i.e., only when the spool captured the
complete document.

When the true total DOES exceed `_MAX_PARSE_BYTES`, `_classify_findings` is not
called at all: the record gets `findings_count: None`, a distinct
`findings_parse: "exceeds_output_budget"`, and — unless a higher-priority
contract already classified the run (`requires_arguments`,
`exceeds_time_budget`, `decay_declared`, unchanged priority) —
`harness_contract: "exceeds_output_budget"`. `demand._validator_defect_items`
gets one new `elif` branch, structurally parallel to the existing
`exceeds_time_budget` branch, that turns this into visible `defect` demand. No
reader may see this state as zero findings.

Spool files live under `<state_dir>/validator_harness/` — the harness's one
writable carve-out under the systemd sandbox — with the same
uuid-suffixed-temp-name-then-delete discipline as `_atomic_write`, and are
deleted after each run regardless of outcome. `_run_one` gained one new
optional parameter (`state_dir: Path | None = None`, default preserves the
~40 existing direct test call sites unedited); the fallback for callers that
omit it is the system temp directory.

Stderr, timeout handling, sandbox-denial detection, and decay-declaration
detection are all unaffected — they continue to read the unchanged 64 KiB
`stdout`/`stderr` in-memory variables exactly as before this change.

# Consequences

Findings from the 27 structurally-growing validators — including
`verify_imports.py`, invisible since the cap was introduced (#928) — become
visible for the first time this fix deploys. This is the intended effect, but
it predicts an increase in defect-demand volume, and it interacts with #1321
(named, explicitly out of scope here): several loop-authored validators key
their suppression baselines on line number, which fails open under edits —
newly-visible findings from THIS fix will include some re-reported, previously
accepted debt from validators with that defect, not only new real problems.
#1321 tracks re-keying those baselines; this ADR does not fix them.

# Alternatives considered

**Script-side arm (rejected):** teach the three (or 27) affected scripts a
`--json --compact` / summary-counts mode that omits per-file `valid: true`
rows. Rejected for the same reason #1208 step 2 chose a harness-side fix over
a script-side rename: it touches 27 files across the instance repo, each with
its own tests and any other caller, rather than one read site in the product
repo the loop does not control; and per-file rows are meaningful output for a
human operator reading a script directly, not only harness fodder — dropping
them at the script level would be a lossy change dictated by the harness's own
constraint, not by the script's own requirements.

**Raising `_MAX_OUTPUT_BYTES` globally (rejected, explicitly a non-goal of
#1320):** would reopen the exact OOM exposure #928 was written to close for
EVERY validator (including ones with no legitimate reason to emit large
output), rather than only widening the read for a document that is bounded but
large. The two-budget split keeps the safety-relevant cap exactly where #928
put it.

**A validator-specific exception list (rejected):** would need its own trust
boundary reasoning (which scripts get the exception, and why a bad actor can't
add itself to the list) for marginal benefit over one shared, conservative
budget sized off host memory.

# Test Contract

| Claim | Test / evidence | Status |
|---|---|---|
| Output between the 64 KiB evidence cap and the 4 MiB parse budget parses completely | `tests/test_validator_harness.py::TestOutputParseBudget::test_output_between_evidence_cap_and_parse_budget_is_fully_parsed` | passing |
| Output over the parse budget is `findings_parse: "exceeds_output_budget"` / `harness_contract: "exceeds_output_budget"`, not silence | `tests/test_validator_harness.py::TestOutputParseBudget::test_output_over_parse_budget_becomes_visible_contract_demand` | passing |
| A runaway printer past the parse budget still finishes with its real exit code (no hang) | `tests/test_validator_harness.py::TestOutputParseBudget::test_runaway_output_past_the_parse_budget_never_hangs` | passing |
| `exceeds_output_budget` becomes exactly one visible `defect` item, not zero | `tests/test_demand.py::TestValidatorHarnessContractReclassified::test_exceeds_output_budget_is_visible_defect_not_silence` | passing |
| Existing evidence-cap behavior (`stdout_truncated`, decay, sandbox-denial, timeout, stderr) is unaffected | full existing `tests/test_validator_harness.py` + `tests/test_demand.py` suites | passing, no regressions |

# References

#928, #934, #936, #1208, #1320, #1321
