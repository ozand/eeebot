---
title: Context ontology — one question, one file, one owner
status: accepted
date: 2026-09-18
authors: [eeebot maintainers]
related: ["#1720", "#1721", "#1722", "#1723", "#1724", "#1725", "#1726", "#1727", "#1728", "#1729", "#1730", "ozand/eeebot-ops-dashboard#301", "#1731", "#1188", "#1193", "#1745", "#1748", "#1750", "#1751", "#1752", "#1753", "#1754", "#1756"]
tags: [prompt, architecture, ontology, runtime]
---

# Status

**Accepted, 2026-09-18 (#1756).** Decision 6 was already code as of PR #1731 and is unchanged. The rest of this record's target state is now deployed rather than aspirational.

Eleven of the original follow-ups are closed and deployed, as of release `20260918T002450Z-canonical-023a38f7`: `IDENTITY.md` trimmed to identity, `SOUL.md` and `USER.md` added at the release root, `OPERATING.md` created as the single owner of the cycle rules, `goals.md` trimmed, the file-driven loader with per-block telemetry (#1725), the prompt-ontology test harness (#1726), the `build_task` hygiene fixes (#1727), the lessons-context filter (#1728), the skills catalogue renderer, and the mutation-policy scope bound on `AGENTS.md` (PR #1731).

Three more landed since the issue that requested this flip was written:

- **#1729** (`roles/*.md` + `build_role_system_prompt`) — merged `824cac9e`, deployed in release `20260918T162023Z-canonical-824cac9e`, health gate CLEAN-EXIT. Live proof: the first post-flip ledger rows carry `system_prompt_chars` — proposer 6,550 (identity short + soul + charter + role), reflector 819 (identity short only).
- **#1745** (the `search_memory` status contract was duplicated between `OPERATING.md`'s `## Tools` section and `nanobot/agent/memory.py`) — fixed in `d3dd4aa6`: the contract now lives only in the tool's own description.
- **#1748** (outcome rows could not answer "was this a real failure") — fixed in `ada86014`.

Closed since: **#1750** (the scope gate became a ratchet, merged `de20e708`; its principle is decision 10 below). Still open under this record, named rather than folded into "done": **#1751** (rule disposition: retire the nine procedural `AGENTS.md` sections and the four zero-read skills against `OPERATING.md`), **#1752** (rewrite the instance `AGENTS.md` to repository layout in one commit — the blocker for the one remaining `xfail(strict)` in the Test Contract below), **#1753** (raise the system-prompt budget from 24,000 to 35,000 chars, derived from the 98,304-token window).

Live evidence, from a `phase: "system_prompt"` ledger row on 2026-09-18: the loop's prompt is assembled from `identity`, `soul`, `goals`, `user`, `operating`, `agents`, `skills_catalogue`, `memory`, `runtime` — the ontology's own order — with `missing: []`.

# Context

The executor's prompt is assembled from seven system blocks and a twenty-section user message, and no single document states which file answers which question or who owns it. Measured on release `01d57773` (2026-09-17, twenty recorded prompts from `lineage-cycle-details.json`):

- The role text ("You are the autonomous improvement agent…") is a string literal in `nanobot/agent/context.py:_get_identity`, not a file. The same function still carries chat-assistant guidance for tools the executor does not have (`message`, `web_fetch`, `web_search`) and says "Ask for clarification", while `IDENTITY.md` says "do not ask for clarification".
- Cycle procedure is split between literals in `nanobot/runtime/bridge.py:build_task` (Branch discipline, Your instructions, Final JSON, Mutation surfaces, ~2,300 chars) and runtime sections of the instance `AGENTS.md` (Cycle contract, Skip, Staging, Forbidden paths, Execution, Termination, ~4,500 chars).
- The instance `AGENTS.md` is 192 lines; its own `docs/agents/purpose.md` says it is "the dev-process contract … not a runtime operating directive" and `docs/agents/shortness.md` sets a hard budget of 150 lines.
- Rules are repeated: the skip rule appears 8 times across `AGENTS.md`, four skills and the user message; the mutation surface 6 times with two lists that disagreed (`AGENTS.md` allowed staging `AGENTS.md`, `mutation_policy` did not, until PR #1731); identity 4 times.
- The original nanobot loaded `AGENTS.md, SOUL.md, USER.md, TOOLS.md` (`nanobot/templates/` still ships them). PR #1192 (`4b1aaac8`, 2026-09-02) reduced `BOOTSTRAP_FILES` to `MUTATION_POLICY.read_paths == ("AGENTS.md",)`; identity was later re-attached as a bridge `system_context` tail outside the prompt cap and outside prompt-fit telemetry.

Reference designs consulted: OpenClaw (`IDENTITY.md` / `SOUL.md` / `USER.md` / `AGENTS.md` / `MEMORY.md`; persona only in workspace files; stable prefix above a cache boundary; built-in missing/truncated markers; sub-agents receive only `AGENTS.md`), Hermes (`SOUL.md` is prompt slot 1; guidance is emitted only when the matching tool is loaded; policy lives in tool descriptions), claw0 (eight explicit layers). Taxonomy from `ozand/agents_library`: eeebot today is `hardcoded + layered`; the target is `file-driven + cached`.

## The seven questions

Every prompt answers seven questions today, each currently scattered across code literals, one release file, or nothing. The target names one file per question:

| # | Question | Answered by |
|---|---|---|
| 1 | Who is the agent? | `IDENTITY.md` |
| 2 | How does it behave? | `SOUL.md` |
| 3 | Why is it doing this work? | `goals.md` |
| 4 | Who is the operator, and what do they always/never want? | `USER.md` |
| 5 | How does a cycle run, including which tools exist? | `OPERATING.md` |
| 6 | What does this repository look like? | `AGENTS.md` |
| 7 | What has it learned, and what can it look up? | `skills/*/SKILL.md`, `memory/index.md` |

## File → owner → location

| File | Owner | Location |
|---|---|---|
| `IDENTITY.md` | operator, immutable to the loop | release root |
| `SOUL.md` | operator, immutable to the loop | release root |
| `goals.md` | operator, immutable to the loop | release root |
| `USER.md` | operator, immutable to the loop | release root |
| `OPERATING.md` | operator, immutable to the loop | release root |
| `AGENTS.md` | loop, through the gate — repository layout only | instance workspace |
| `skills/*/SKILL.md` | loop, through the gate | instance workspace |
| `memory/index.md` | loop, through the gate | instance workspace |
| `## Runtime` facts, tool schemas, missing/truncated markers, `runtime_context` | generated by code, labelled technical | assembled at render time, not a file |

## Assembly order

```text
identity -> soul -> goals -> user -> operating -> instance AGENTS.md
  -> skills index -> memory (resident + pointer) -> runtime facts
  -> [cache boundary] -> the volatile user message
```

Everything up to and including runtime facts is stable within a release; the volatile user message comes last, so the stable blocks form a cache-friendly prefix.

# Decision

**Six rules, plus three decisions recorded at acceptance (7-9, added 2026-09-18 — see Status).**

## 1. One question, one file, one owner

The table above. Release root files are operator-owned and immutable to the loop. Instance files are loop-owned through the gate. Everything else is generated and labelled as technical, never as prose the operator wrote.

## 2. Code assembles, code does not author

No role, behaviour or procedure prose survives as a string literal in `context.py`, `bridge.py`, `llm_proposer.py`, or any other caller. Those modules read files and assemble blocks in the order below; they do not contain the words that make up the persona, the cycle procedure, or the operator's directives. Other roles (proposer, reflector, curator, strategist, narrator) load `roles/<role>.md` from the release root on top of the shared identity and soul, rather than repeating or improvising their own framing.

## 3. Order

Identity → soul → goals → user → operating → instance `AGENTS.md` → skills index → memory (resident + pointer) → runtime facts; then the volatile user message. Stable blocks form a cache-friendly prefix, matching the OpenClaw design consulted in Context.

## 4. One rule appears once

A fingerprint test over the assembled prompt asserts that each named rule (skip, mutation surface, branch discipline, test runner, identity, iteration budget) matches exactly one block. A rule that needs restating in two places is a sign the ontology split it wrong, not a reason to duplicate it.

## 5. Guidance only with capability

A tool is mentioned in prose only if it is in the executor's registry for that cycle; a tool's contract lives in its own description, not in surrounding prose. Prose that names `message`, `web_fetch`, or `web_search` to an executor that was never given those tools is exactly the defect this rule closes.

## 6. AGENTS.md scope, standing directives, priorities, test runner, and full charter

Six things decided together, all implemented in PR #1731:

- Instance `AGENTS.md` stays loop-editable, but only for repository layout. The harness gate bounds every staged version: `MUTATION_POLICY.agents_md_max_lines = 150`, `agents_md_runtime_headings` (the nine runtime-rule headings that now belong to `OPERATING.md`), applied to `git show HEAD:AGENTS.md` when a cycle touches the file, fail closed on deletion (a missing `AGENTS.md` at `HEAD` is a violation — the loop may shrink the file, never remove it).
- Standing operator directives use the `Always / Never / Prefer` form, with a `superseded` marking for a directive a later one has replaced, rather than silent deletion.
- Work-item priorities stay in `state/goals/goal_text.json`; they are not folded into `USER.md`'s standing directives.
- The test runner is pytest, named once, not re-derived per prompt.
- The executor receives the full charter (`goals.md`), never a summary or an excerpt chosen by code.

**This is already implemented, in PR #1731.** It reverses #1188/#1193, which closed the `AGENTS.md` write path entirely after 20 autonomous integrations in five days whose only substantive change was appending to it. The reversal is safe here because the bound moved from "no write access" to "write access scoped and checked by the harness gate, not by a test the loop itself can edit" — the runtime rules the loop used to accumulate in `AGENTS.md` now live in the release-owned `OPERATING.md`, outside its reach. **This decision supersedes ADR-003 in part**, on the single point of whether the loop may commit `AGENTS.md` at all; ADR-003's consolidation tool, operator ownership of removal, and every other clause are unaffected. See the note added under ADR-003's Status section.

## 7. Budget unit: stated in tokens, enforced in characters

The system-prompt budget is *stated in tokens*, derived from the serving model's 98,304-token context window, but *enforced in characters* everywhere in the runtime — fit arithmetic, telemetry, and the Test Contract's own budgets (`OPERATING.md` ≤ 5,000 chars, `SOUL.md` ≤ 1,800, `IDENTITY.md` ≤ 1,500, `USER.md` ≤ 4,000, the system prompt overall ≤ 24,000) — because the i386 host serving the LLM carries no tokenizer. The chars/token ratio the cap was derived from has to travel with the number, or the next person who needs to move the cap re-derives a different ratio from the same window and gets a different answer.

Measured on host `eeepc`, 2026-09-18, over 2,876 executor `llm_calls` rows: `prompt_tokens` max 79,804; `completion_tokens` capped at exactly 8,192 on every row (the client's own `max_tokens`, not a model limit — the LiteLLM route sets none); prompt + completion max 83,836 tokens against the 98,304-token window, leaving 14,468 tokens of headroom. This measurement closed #1754 and is the recorded derivation for this cap; #1753 (raising the character budget) is expected to cite it rather than re-measure.

## 8. Scope of "code does not author": the loop profile only

Rule 2 binds the loop-profile assembly path — `ContextBuilder._get_identity(loop_profile=True)` and its callers — not the interactive one. `ContextBuilder._get_identity(loop_profile=False)` deliberately keeps the original interactive nanobot template as a code literal: the "You are nanobot, a helpful AI assistant" role sentence, the Windows/POSIX `## Platform Policy` block, the five-line `## nanobot Guidelines` list (including "Ask for clarification" and the guidance about the `message`/`web_fetch`/`web_search` tools that rule 5 forbids for the loop), and the closing "Only use the 'message' tool…" sentence. This is a decision, not an unfinished migration: the interactive product has no release-root `IDENTITY.md`/`SOUL.md`/`OPERATING.md` to read — it is not the self-evolving loop, has no operator-authored files to assemble, and its persona is nanobot's own product surface, not this record's concern. `tests/test_ontology_loader.py::test_no_forbidden_loop_prose_in_context_or_subagent_source` checks the forbidden phrases are confined to the interactive branch of `_get_identity`, not absent from the file — proving the scoping, not a blanket ban.

## 9. The `SOUL.md` / `USER.md` filename collision is kept, not resolved

`nanobot/templates/SOUL.md` and `nanobot/templates/USER.md` are package-bundled workspace scaffolding for the interactive nanobot product — copied into a new interactive workspace by `sync_workspace_templates` only when the file is missing there. They carry different content and a different owner from the release-root `SOUL.md`/`USER.md` the loop profile reads (rule 1's table: operator-owned, immutable to the loop). The same two filenames now name two unrelated files depending on which profile is running, and a `grep -r SOUL.md` across the repo returns both without saying which is which.

Decision: keep the collision rather than rename either pair — renaming `nanobot/templates/*` is a stated non-goal of this record. The two profiles never load both at once, and disambiguation already happens by directory and loader path, not filename: the loop reads release-root files named directly in `ContextBuilder.BOOTSTRAP_FILES`; the interactive profile reads workspace files via `MUTATION_POLICY.read_paths`, seeded once from `nanobot/templates/` by `sync_workspace_templates`. This matches the collision as the issue that requested this record described it; nothing here contradicts that description.

## 10. A bound introduced over existing content ships with a migration commit or a ratchet clause

**A bound introduced over existing content ships with either a migration commit or a ratchet clause (#1750).** PR #1731's absolute bound (`agents_md_max_lines`, the nine forbidden headings) was applied on the day the live instance `AGENTS.md` already violated it at 192 lines with all nine headings present. Checked only against the staged version at `HEAD`, the rule then rejected *every* commit that touched the file — including one that strictly improved it — because the only commit able to pass was a single atomic rewrite to full compliance, unlikely to fit inside one bounded executor turn. The file was frozen in its non-compliant state by the rule meant to make it compliant: **a bound that rejects the repair of the state it is diagnosing is a freeze, not a guard.** #1750 turns the check into a ratchet — reads the version at the gate's base sha alongside `HEAD`; while the base version is non-compliant, a staged version is accepted iff it does not regress (line count does not increase, no forbidden heading absent from the base version is reintroduced) rather than only when it reaches full compliance outright. Once the base version is itself compliant, the absolute bound applies exactly as before. The same trap recurs on any future bound tightened over content the tightening itself does not touch; the fix each time is the same shape — a migration commit that brings existing content into compliance in the same change that introduces the bound, or (when that commit cannot be guaranteed to land atomically, as here) a ratchet against the prior state instead of the absolute target.

# Consequences

## What gets easier

One document answers "which file says X" instead of a grep across two runtimes. A rule that needs changing has exactly one place to change it (rule 4), so a fix cannot land in one copy and drift from the other five. New roles (proposer, reflector, curator, strategist, narrator) get identity and soul for free instead of reimplementing framing per role.

## What gets harder

Five release files now require operator authorship and upkeep (`IDENTITY.md`, `SOUL.md`, `USER.md`, `OPERATING.md`, plus the existing `goals.md`) where before there was one file and several code literals; drafting and reviewing `USER.md` and `OPERATING.md` is real work, not a mechanical extraction (#1721, #1722, #1723). The loader (#1725) may not deploy until three preconditions are met: `#1721` `#1722` `#1723` deployed, and the instance skills catalogue is ≤ 4,500 chars (#1730 part 2) — the per-block caps below do not fit the catalogue at its current size.

## Prompt size

Corrected against the per-block caps landing in #1725 (1,500 + 1,800 + 3,200 + 4,000 + 5,000 + 4,000 + 1,000 + 400 = 20,900 chars before the skills catalogue, which is 10,109 chars today):

- **System prompt ≤ 24,000 chars**, with `dropped: []` and every block individually under its own declared cap. Not the smaller number an earlier draft of this issue used — that arithmetic did not fit the catalogue and is not restated here.
- **User message ≈ 5,500 chars**, after `#1727`'s `build_task` defect fixes.
- **Total ≈ 30,000 chars**, down from **~37,800** measured today.
- **Precondition:** the instance skills catalogue must shrink to ≤ 4,500 chars (`#1730` part 2) *before* the loader (`#1725`) deploys. `#1730` part 2's first link — retiring the skip cluster and the unittest skill — has no dependency on `OPERATING.md` and can run ahead of the rest of this migration; `#1730` parts 1 and 3 (the instance `AGENTS.md` shrink, and other droppable-reserve moves) do wait for `OPERATING.md` to be on the host, since they remove rules that must exist there first.

Prompt-fit telemetry must cover every block, including today's `system_context` tail — the identity re-attachment from PR #1192's `BOOTSTRAP_FILES` reduction currently sits outside both the prompt cap and prompt-fit telemetry, which is itself part of the defect this record closes.

## What does not change

The gate's role as the sole enforcement point for the mutation surface. The scorecard, the ledger, and `_TARGETS`. The demand collector and its deterministic sources (ADR-020). The charter's content — only its file boundary and delivery move (rule 6: the executor still receives it in full).

# Alternatives considered

- **Keep the role in code.** The status quo. Rejected: a string literal cannot be reviewed, versioned, or bounded by the gate the way a release file can, and it is why the identity text and `IDENTITY.md` have already drifted apart (Context, `_get_identity`).
- **Operator-owned instance `AGENTS.md`, with no loop write access at all.** The #1193 position this record partially reverses. Rejected as the standing rule: it does not distinguish repository-layout edits (safe, needed as the tree grows) from runtime-rule accumulation (the actual #1188 defect). A scoped, gate-bounded write path closes the same defect without freezing a file the loop legitimately needs to keep current.
- **Inject only `AGENTS.md` into the executor, OpenClaw's own treatment of its sub-agents.** Rejected for the primary executor: OpenClaw uses this for a sub-agent that inherits the parent's identity and soul by construction and only needs the repository map on top. The eeebot executor is not spawned by an already-identified parent — it is the one process that has to be told who it is, how it behaves, why, whom it serves, and how a cycle runs, so it needs the full stack (rule 3's order), not a single file. Sub-agents spawned *by* the executor are a separate, narrower question this record does not decide.

# Test Contract

- **Fingerprint-once test** — `tests/test_prompt_ontology.py`: each named rule (skip, mutation surface, branch discipline, test runner, identity, iteration budget) is asserted to match exactly one block of the assembled prompt.
- **AGENTS.md scope test** — `tests/test_prompt_ontology.py`: a release-side mirror of the `#1731` gate bound (`mutation_policy.agents_md_scope_violations`) — a fixture text of ≤ 150 lines with none of the runtime headings passes; a fixture over the line limit, or carrying a runtime heading, fails. A second case, run directly against the live instance `AGENTS.md` (192 lines today), is `xfail(strict=True)` (`test_agents_md_scope_violations_against_real_instance_fixture`) until #1752 shrinks it — the strict marker means the test starts failing loudly, not silently passing, the day the file actually gets under the bound and the marker should come off.
- **Surface-parity test** — `tests/test_prompt_ontology.py`: the rendered "Allowed targets" / "Do NOT modify" prompt lines and the instance `AGENTS.md`'s own repository-layout section name the same paths, so the two lists PR #1731 reconciled cannot drift apart again silently.
- **No-guidance-without-capability test** — `tests/test_prompt_ontology.py`: a fixture executor registry without `message`/`web_fetch`/`web_search` asserts none of those tool names appear anywhere in the assembled system prompt.
- **OPERATING.md structure and generation parity** — `tests/test_operating_md.py`: the file exists, carries its ten headings in the assembly order, renders the mutation-surface section byte-for-byte from `MUTATION_POLICY.render_bridge_surface_block()`, and names pytest (never unittest) as the one test runner.
- **File-driven loader** — `tests/test_ontology_loader.py`: `identity → soul → goals → user → operating → AGENTS.md → skills → memory → runtime` load and assemble in that order with per-block telemetry, missing/truncated files degrade with a marker rather than raising, and no forbidden loop-profile prose (decision 8) appears outside the interactive branch of `_get_identity`.

`tests/test_prompt_ontology.py` was added by #1726 (merged `96a450a0`). Two items were `xfail(strict=True)` at that time and have since resolved: the fingerprint-once user-message half, once #1723 part (b) removed the `build_task` literals (`023a38f7`); and a `search_memory` status-contract duplication the harness found between `OPERATING.md` and `nanobot/agent/memory.py`, fixed by #1745 (`d3dd4aa6`) and now its own passing assertion, `test_search_memory_status_contract_appears_only_in_operating_tools_block`. One `xfail(strict=True)` remains by design — `test_agents_md_scope_violations_against_real_instance_fixture`, pending #1752 — and is not counted as a failure of this contract.

`tests/test_release_ontology_files.py` (#1721, #1722) also passes today and is run alongside the contract above as part of this record's own acceptance check, though it predates this ADR and does not itself cite `ADR-022`, so it is not listed as a numbered contract item: it holds `SOUL.md`, `IDENTITY.md` and `USER.md` to their per-file budgets and checks `USER.md`'s directives are dated, sourced, and in Always/Never/Prefer form. Run it with the three files above; it is expected to pass with no `xfail`.

# References

- #1720 — this record.
- #1721 — `SOUL.md` + `IDENTITY.md` trim.
- #1722 — `USER.md`.
- #1723 — `OPERATING.md` + `build_task` literal removal + surface-parity test.
- #1724 — `goals.md` trim.
- #1725 — the file-driven loader, `## Runtime`-only generated block, and telemetry.
- #1726 — the ontology test suite (`tests/test_prompt_ontology.py`).
- #1727 — `build_task` defects (independent of this record's own dependency chain).
- #1728 — `lessons_context` threshold.
- #1729 — `roles/*.md`.
- #1730 — operator priorities: instance `AGENTS.md` shrink, skills catalogue, droppable reserve. Open; #1752 is the current tracking issue for part 1 specifically.
- #1745 — `search_memory` status-contract duplication. Closed, `d3dd4aa6`.
- #1748 — outcome rows could not answer "was this a real failure". Closed, `ada86014`.
- #1750 — closed, merged `de20e708`. A bound introduced over existing content ships with a migration commit or a ratchet clause; recorded as decision 10.
- #1751 — open. Dispose the nine procedural `AGENTS.md` sections and the four zero-read skills against `OPERATING.md`.
- #1752 — open. Rewrite the instance `AGENTS.md` to repository layout in one commit; blocks the one remaining `xfail(strict)` in the Test Contract.
- #1753 — open. Raise the system-prompt budget from 24,000 to 35,000 chars; should cite decision 7's measurement rather than re-deriving it.
- #1754 — closed. The tokens-vs-characters measurement decision 7 records.
- `ozand/eeebot-ops-dashboard#301` — `agent.html` rebuilt by this ontology.
- PR #1731 — decision 6, already merged in code: `AGENTS.md` scope bound, `SOUL.md`/`USER.md`/`OPERATING.md` join the immutable-files list, `ops/` named in the rendered surface block.
- ADR-003 — superseded in part by this record (AGENTS.md commit permission); its consolidation tool and operator-removal authority stand.
- ADR-011 — the charter keeps a voice after every threshold is met; rule 6's "full charter, not a summary" continues that line.
- ADR-014, ADR-015, ADR-016 — the avatar, the channel, and the journal as instruments with their own single-owner documents; this record applies the same discipline to the executor's own prompt.
- ADR-020 — direction from reflection over a span; the demand collector this record's "does not change" section names.
- ADR-021 — capability changes only after measured use; the same evidence discipline this record's telemetry consequence extends to prompt blocks.
- `nanobot/agent/context.py`, `nanobot/runtime/bridge.py:build_task`, `nanobot/runtime/mutation_policy.py`.
