# Context compaction — invariants

_Status: approved with conditions A–E (architect review, PR #2023); Codex's
remaining P1/P2 threads on that round are resolved below (namespace,
path sanitization, strict monotonicity, full-payload tests, an explicit
retention sweep, a byte-accurate volume measurement). This is a **change
proposal** (AGENTS.md's change workflow), not a `docs/specs/` capability
spec — it lives at `docs/changes/1930-compaction-invariants/proposal.md`
until #1930 lands, then archives and folds into the affected spec.
Written 2026-09-27 during
#1930 (ADR #1776 follow-up), after three rounds in which Codex found a new
way for the heuristic to lose progress: carrier accumulation, reasoning
payloads, "no candidate fits the summary", and "progress plus decision in
one turn". That pattern is a design defect, not a line-level one — the
current design guesses what evidence matters and re-derives it heuristically
every round, so anything the heuristic doesn't recognize is gone for good.
These invariants replace guessing with a structural guarantee: the
heuristic becomes an optimization (a nice-to-read summary), not the only
copy of what was removed._

## I1 — Preservation

**Definition.** Text removed from the in-context history by compaction is
never destroyed. Every character dropped from `content`, `reasoning_content`,
or `thinking_blocks` (the last only if it is ever intentionally shrunk —
see I5) is written in full to a per-cycle file before the in-context copy
is replaced. The carrier (or excerpt) references that file's path so the
executor can retrieve the original with an existing file-reading tool, plus
an index of what's inside: one line per preserved message (turn index,
role, first line ≤ 80 chars, size), stating explicitly that the full text
lives at that path.

**What gets measured.** For a given compaction call: `written_bytes` (sum
of the full pre-compaction text of every message touched this round) and
`file_path`. A round that touches N messages writes N sections (or one
file with N sections) — never fewer.

**Mechanics.**
- **Who writes it:** `compact_messages`, in the same pass that decides
  `new_content` for each `worth_indices` candidate — before the candidate's
  in-context content is replaced.
- **Where:** `{state_root}/compaction/<cycle_id>/<execution_id>/<iteration>.md`
  (next to the existing `{state_root}/compaction/journal.jsonl`, so both
  are found the same way). One file per compaction call, named by the
  same `cycle_id`/`iteration` the journal entry for that call already
  carries, plus the executing task's own `execution_id` — `cycle_id` alone
  is not unique (`SubagentManager` allows an empty cycle ID, and separate
  subagent executions can share a state root and each restart `iteration`
  from 0), so without an execution segment a second execution's write can
  overwrite or race on another's `.tmp` file, leaving an earlier carrier
  pointing at unrelated or missing evidence. A repeated cycle therefore
  never overwrites an earlier execution's files. `cycle_id` reaches here
  from the request (`bridge.py` accepts it verbatim, including `/`, `\`,
  or `..`) — it is restricted to a safe single-path-component charset
  before use, and the fully joined path is checked to resolve inside
  `{state_root}/compaction` before any write, never interpolated raw. This
  directory must be inside the executor's existing allowed read paths (no
  permission changes) and outside the instance repo checkout — the
  restore step erases uncommitted files there (#1966). If
  `{state_root}/compaction/<cycle_id>/` is outside the executor's read
  allowlist, the implementation picks a location that is, keeping the same
  per-call naming.
- **When:** synchronously, before any message's in-context content is
  mutated. If the write fails, the whole call fails open (existing
  fail-open contract) — no message is modified without an on-disk copy of
  what it's about to lose.
- **Atomicity:** write to `<path>.tmp`, `fsync`, then `os.replace` onto the
  final path — the existing on-disk file (if any, from a retried call) is
  either fully replaced or untouched, never partially written.
- **Retention/rotation:** one file per compaction call, never rotated
  independently while the cycle is active (a reader never finds a path the
  carrier references already gone). Bounded by an explicit sweep — new
  code, since no per-cycle teardown that could retire this directory
  exists today (`state_root` is a persistent, shared directory, not a
  per-cycle one that gets torn down): run once per cycle start, it scans
  `{state_root}/compaction/*/` for cycle dirs whose cycle has finished and
  whose newest file is older than 7 days, deletes them, then enforces a
  total size cap oldest-first across what remains; an active cycle's
  directory is never touched. First implementation step: measure the
  current volume from the journal's existing `compacted_details[].dropped_chars`
  (the exact character count of each dropped middle span — already
  written today, and a much closer proxy for on-disk bytes than
  `before_tokens - after_tokens`, which is a net *token* delta after
  carrier/index overhead and undercounts raw bytes roughly fourfold),
  summed per day (`ts`) and per cycle (`cycle_id`) to size the cap.
- **Privacy class:** private executor state, same class as the rest of
  `{state_root}` — contains full tool outputs and assistant reasoning,
  never published, never included in any public dashboard or gh-pages
  artifact. Formalized on ADR-036's private-only inputs list; if D2's
  private pages ever render this text, it goes through the same sanitizer
  as other call text. Canary test (dashboard repo): a marker planted inside
  a preservation file never reaches the public sink.
- **Size bounds:** the *in-context* copy is bounded without losing data;
  the file itself lives in already-git-ignored runtime state, not in
  context, so it does not compete with the token budget it exists to
  protect — bounded instead by the retention rule above.
- **Usage:** a counter (from the action index) of reads of preservation
  paths per cycle with compaction, so it's measurable whether the executor
  ever uses them. A low count doesn't argue for dropping I1 — the file is
  still a correctness guarantee — it argues the carrier's index (above)
  isn't doing its job, and becomes the next hypothesis to test.

**How it's tested.** A property test embeds a unique marker token AND
records the complete pre-compaction payload (`content`, `reasoning_content`)
of every message a random history hands to `compact_messages`. After
compaction it asserts, for every touched message, the *complete* recorded
payload — not merely the marker token — round-trips byte-for-byte out of
the preservation files: marker survival alone would still pass if a buggy
writer kept only the marker, or kept a head/tail excerpt with the middle
lost. Because a message compacted in an earlier round may no longer be in
context to re-check this round, the assertion searches every preservation
file still referenced by the current carrier's index (I1/B), not only the
file the current round wrote. See Test plan. Separately: a test through
the executor's real tool layer confirms `read_file` on the path named in
the carrier succeeds — the file being written is not enough if the
executor's tools can't open it.

## I2 — Monotonicity

**Definition.** A round is only journaled `reason="compacted"` when total
estimated tokens (`_total_tokens`) of the returned history is *strictly*
less than the total before (`after < before`) — equality is not a
compaction: it spent carrier/index overhead without buying back any
headroom, while still marking its candidates touched and excluding their
evidence from later rounds (I4), so it must not be allowed to pass as a
success. Equality takes the same decline path as "can't reduce at all":
the returned history is byte-identical to the input, and no message is
marked touched (`_compaction_touched` unset on every message that wasn't
already marked before this call). A new `reason` value in the journal
(e.g. `declined_no_reduction`) covers both cases, distinct from
`already_compact`. A declined round changes no message's content, so I1
writes nothing for it — the preservation file exists only for rounds that
actually compact.

**What gets measured.** `_total_tokens(before)` vs. `_total_tokens(after)`
for every call whose `reason` is `"compacted"` — must satisfy `after <
before`, never merely `<=`. The carrier's per-message index (I1/B) counts
toward `after` here — it's real returned-history bytes, not a free
addition outside the budget it's meant to respect.

**How it's tested.** A property test asserts, for every random-history
compaction call: if `reason == "compacted"`, `after < before` (strict); if
any other reason — including a candidate selection that would only reach
exact equality — `after == before` exactly and the set of messages with
`_compaction_touched` set is unchanged. See Test plan.

## I3 — One carrier, marked by code

**Definition.** At most one message in history ever carries
`_compaction_carrier: True`. That flag (and `_compaction_touched`) is set
and read only by `nanobot/runtime/context_compaction.py` itself — never
inferred from a message's own text (a raw tool result can legitimately
contain the literal marker text). `_CARRIER_FLAG`/`_COMPACTED_FLAG` exist
today only on #1930's own branch, from its convergence rounds — not on
`main` — and land as part of this same implementation pass, not as
separately-shipped prior work.

**What gets measured.** `sum(1 for m in messages if m.get("_compaction_carrier"))`
— must be `0` or `1` after every call, for the lifetime of a cycle.

**How it's tested.** A property test asserts this count is `<= 1` after
every compaction in a sequence of N calls over an evolving random history.
Already partially covered by
`test_carrier_growth_guard_keeps_exactly_one_live_summary_across_rounds` in
`tests/test_context_compaction.py`; the property test generalizes it to
random histories.

## I4 — Atomicity

**Definition.** A compaction round either (a) installs a carrier **and**
shrinks every worth-compacting candidate this round, or (b) changes
nothing at all — no candidate is excerpted, no message is flagged touched,
and the fresh evidence computed this round is not silently discarded
without a durable copy (I1 covers the "not discarded" half once it ships;
I4 covers the "no partial state" half on its own).

This closes 4115904095 ("Preserve fresh evidence when no carrier fits"):
today, when no worth-compacting candidate is large enough to hold the
refreshed summary, every candidate is still bare-excerpted and flagged
`_compaction_touched` — so a future round's evidence span excludes them
(I3's flag-based marking, correctly applied, has the side effect of
permanently retiring their evidence) even though nothing was actually
preserved. I4 requires that half-outcome to be impossible: either the
round fully commits (carrier + shrink), or it fully declines (identical to
I2's cancellation path, and can share its `declined_no_reduction` reason).

**What gets measured.** For a call with `worth_indices` non-empty: either
`carrier is not None` and every `i in worth_indices` has
`new_messages[i] != messages[i]` (something changed), or `carrier is None`
and every `i in worth_indices` is byte-identical to its input (nothing
changed, nothing flagged).

**How it's tested.** A property test constructs histories specifically
designed to make every worth-compacting candidate individually too small
to hold the refreshed summary (Codex's own reproducer: many
similarly-sized turns, none large enough) and asserts the round is fully
declined, not partially executed.

## I5 — Unsupported fields make the message incompressible

**Definition.** A message carrying a field this module has no verified-safe
way to shrink (currently: `thinking_blocks`, Anthropic's extended-thinking
form, including `redacted_thinking`) is never a compaction candidate,
regardless of size. It is left completely untouched — content,
`reasoning_content` (if any), `thinking_blocks`, all byte-identical — but
still counted toward the token estimate (`_message_tokens`) as
incompressible, so the trigger/protected-span accounting isn't blind to
it. `_worth_compacting`'s `thinking_blocks` guard and
`_thinking_blocks_text`'s `data`-key accounting exist today only on
#1930's own branch, from its convergence rounds — not on `main` — and
land with the rest of this pass.

**What gets measured.** For any message `m` with `m.get("thinking_blocks")`
truthy: `m` is identical (by value, all keys) before and after every
compaction call across the message's lifetime in history, and
`_message_tokens(m) > 0` whenever it carries a non-empty payload.

**How it's tested.** A property test asserts byte-identity of every
thinking-block-carrying message across N random-history compactions, and
that its contribution to `_total_tokens` is never zero when its payload is
non-empty. Partially covered today by
`test_thinking_blocks_message_is_left_completely_untouched` and
`test_redacted_thinking_payload_under_data_key_is_counted`; the property
test generalizes both to random histories and repeated rounds.

---

## Table: every P1 thread on #1930 → the invariant that closes it

| Thread (id, title) | Closes via | Why |
|---|---|---|
| 4113359607 "Bound cumulative summaries with a single carrier" | **I3** | Two-carrier accumulation is a violation of "at most one carrier"; flag-based marking + retire-on-install makes it structurally impossible. |
| 4113359613 "Carry progress from assistants that also call tools" | **I1** | Heuristic excludes tool-calling assistant progress from the summary; the full original text survives verbatim in the per-cycle file regardless of what the heuristic captures. |
| 4113427286 "Preserve progress from assistants that invoke tools" | **I1** | Same class as above (independent repro, same root cause). |
| 4113452701 "Preserve progress beyond the first 500 characters" | **I1** | Heuristic drops text past a fixed offset; full original preserved on disk regardless. |
| 4113452704 "Bound file lists when constructing the summary" | **I2** | An unbounded path-list section let the assembled summary exceed the cap and grow the history; the "cancel if not smaller" rule makes any such construction bug fail-safe instead of fail-open. |
| 4113690189 "Compact assistant reasoning payloads as well" | **I1** | `reasoning_content` is a real supported field now (counted + cleared) — clearing it is "removing text from context," so its removal is covered by the same preservation guarantee as `content`. |
| 4113690192 "Ensure the summary carrier actually shrinks" | **I2** | Carrier growth is exactly what monotonicity forbids; already enforced by `test_summary_carrier_is_not_inserted_when_it_would_increase_history`. |
| 4113699932 "Preserve fresh evidence when enforcing the summary cap" | **I1** | The summary's own internal truncation can drop fresh evidence from its rendered text; the full original messages remain durably available regardless of what the rendered summary shows. |
| 4113699933 "Extract the matched decision instead of the message prefix" | **I1** | Heuristic-extraction gap (decision text sliced wrong); full original preserved on disk. |
| 4113934219 "Keep the prior summary until a replacement is installed" | **I4** | This is exactly a partial-commit: retiring the old carrier without a successful new install. Already fixed by the `installed_carrier` gate; I4 generalizes and formalizes it. |
| 4113995954 "Try later candidates before discarding the refreshed summary" | **I1** | Even with the in-PR fix (try every candidate), any candidate still ultimately excerpted-without-carrying keeps its full original text on disk. |
| 4113995957 "Keep the matched decision instead of the message prefix" | **I1** | Duplicate of 4113699933 (re-raised, later diff anchor); same closure. |
| 4114993527 "Compact persisted reasoning payloads too" | **I1** | Same as 4113690189 for the `reasoning_content` half; the `thinking_blocks` half is I5, not I1 (never removed at all). |
| 4115692513 "Preserve thinking blocks attached to tool calls" | **I5** | Exactly what I5 defines: unsupported field → message is incompressible, never touched. |
| 4115692517 "Account for redacted thinking block payloads" | **I5** | Same invariant; the `data`-key accounting fix is I5's "still counted" half. |
| 4115719536 "Reserve space for the summary marker" | **I3** | The bug was header text being truncated away, breaking *text-based* carrier detection. Once marking is a code-set flag (I3), the rendered header's exact bytes stop being load-bearing for correctness — this bug class becomes structurally impossible, not just patched. |
| 4115719560 "Add the required change and capability documentation" | *(not applicable — see below)* | Process/documentation compliance (AGENTS.md's change workflow), not a runtime-behavior defect. Out of scope for a runtime invariant set; already fixed procedurally in the PR, unrelated to I1-I5. |
| 4115719563 "Stop trusting payload markers as compaction state" | **I3** | Exactly what I3 defines: state is a code-set flag, never inferred from text. |
| 4115904095 "Preserve fresh evidence when no carrier fits" | **I4** | Named explicitly in the invariant's own definition above — this is the invariant written specifically to close this thread. |
| 4115904099 "Preserve progress from turns that also state decisions" | **I1** | Mutually-exclusive decision/progress branching drops one or the other from the summary; full original text of the turn survives on disk regardless of which branch the heuristic took. |

**Gaps:** none found among the runtime-behavior P1 threads — every one maps
to I1-I5. The one exception (4115719560, documentation) is not a runtime
defect and is intentionally left out of this table's "closes" column
rather than force-fit to an invariant that doesn't describe it.

## Test plan (outline)

**Generator** — `_random_history(rng, n_turns)`: builds `[system, user]`
plus `n_turns` alternating assistant/tool messages, each independently
randomized across:
- content length (short / just-over-`_min_compact_len()` / large / huge),
- an assistant turn embeds a unique marker token, and independently and
  with its own probability: a `decision:`/`we will` phrase, a
  `reasoning_content` payload (random length), or `thinking_blocks`
  (random length, occasionally shaped like `redacted_thinking` with only a
  `data` key),
- a tool turn embeds a unique marker token, and independently: path-like
  text (to exercise file-list extraction) or literal marker-lookalike text
  (`"[Compaction summary"` / the `_OMIT_MARKER` string, to stress I3),
- occasionally, a pre-existing marked carrier message (flagged, with a
  `previous`-sized summary text near `MAX_SUMMARY_CHARS`) prepended, to
  stress I2/I3/I4 under repeated-compaction conditions.

Every marker token, along with the complete pre-compaction payload it's
embedded in (`content` and, when present, `reasoning_content`), is
recorded in a `planted: dict[token, full_payload]` map before the call,
for the I1 assertion.

**Assertions, after every `compact_messages` call:**
- **I1:** for every message touched this round, its complete recorded
  pre-compaction payload — not just the planted marker token — is found
  either still literally present in some message's current text
  (`_message_text`), or inside a preservation file still referenced by the
  carrier's index. The search checks every file the index names, not only
  the one this round wrote, since an earlier round's file remains the sole
  copy for messages already dropped from context.
- **I2:** per the definition above (`after < before`, strict, when
  `reason == "compacted"`; exact no-op — including exact-equality
  candidate selections — otherwise).
- **I3:** `sum(1 for m in result if m.get("_compaction_carrier")) <= 1`.
- **I4:** per the definition above (full commit or full decline, no
  partial state).
- **I5:** every message with `thinking_blocks` is byte-identical to its
  pre-call value; its `_message_tokens` contribution is non-zero.

**Sequence test:** run the generator once, then call `compact_messages` N
times in a row (N in `[5, 20, 50]`), appending fresh random turns between
calls (simulating an ongoing cycle) and re-asserting I1-I5 after every
single call, not only at the end — a property that only holds at the end
of a fixed sequence would hide an intermediate-round violation.

## Rules for implementation

One pass, merged together with #1930's own branch (I3/I5's code already
lives there, from its convergence rounds — this doesn't restart that
work, it lands it on `main` alongside I1/I2/I4). I1 requires a new
write-then-reference step in `compact_messages`, including the
execution-id namespace, cycle-id sanitization/containment check, and
index format from conditions A/B; I2 requires the strict `after < before`
check plus the matching `declined_no_reduction` reason (equality folds
into decline, no separate reason value); I2 and I4 restructure the
carrier-decision path to a single commit/decline branch instead of the
current per-candidate loop. Conditions C (retention), D (ADR-036 +
canary), and E (usage counter) ship alongside I1, not deferred. First
implementation step per C: measure current volume from the journal's
existing `compacted_details[].dropped_chars` (character counts, not the
`before_tokens`/`after_tokens` token delta) before picking the size cap.
