# Context compaction — spec

_Status: current. Last updated: 2026-09-27 (#1776 + #1930:
`docs/changes/1776-structural-compaction/`)._

## What this covers

`nanobot/runtime/context_compaction.py` reduces the token size of the
subagent loop's running `messages` history once it exceeds a configurable
fraction of the model's context window. Called once per provider response
from `nanobot/agent/subagent.py`. Stdlib-only, fail-open: any exception
returns the original history unchanged (and journals the failure).

## Trigger

`compact_messages` fires when the estimated token count — the real
previous-response `prompt_tokens` plus this iteration's estimated appended
delta when available, otherwise a full re-estimate of the whole history —
reaches `THRESHOLD * (WINDOW_TOKENS - RESERVE_TOKENS)`. Every evaluation is
journalled to `{state_root}/compaction/journal.jsonl` with a `reason`
(`below_threshold`, `nothing_older_than_cut`, `already_compact`,
`compacted`, or `error:<ExceptionType>`), including declines — a decline is
never silent.

## Protected span

`system` and the initial `user` task are always protected. Beyond that, the
cut is a **token span**, not a fixed message count: walking back from the
newest message, everything within `KEEP_TOKENS` estimated tokens of the end
is protected; everything older is a compaction candidate, assistant turns
included. A token span (not a count) means a single oversized recent
message is still compactable — a fixed count never could be.

A tool call and its result are never separated by this: only a candidate's
`content` (and `reasoning_content`/`thinking_blocks`, see below) is ever
replaced; `tool_calls`/`tool_call_id` and message order/count are untouched.

## What gets replaced, and with what

Among this round's candidates, a message is only touched if doing so is
worth it (`_worth_compacting`): its combined text (see below) is long
enough that a bounded excerpt would actually shrink it, and it has not
already been compacted this way.

- **Structural summary carrier.** Evidence-only, deterministic (no model
  call): explicit decisions (`decision:` / `we will `), progress narration,
  and file paths observed in tool-call arguments/tool results, extracted
  from this round's newly-dropped span. Every worth-compacting candidate is
  tried in order; the first whose refreshed summary + its own excerpt is
  actually smaller than its original text becomes this round's carrier. If
  none qualify, no carrier installs this round (every candidate just gets a
  plain excerpt, below) — a small first candidate never causes the summary
  to be discarded when a later, larger candidate could hold it.
- **Cumulative.** The prior round's carrier text (if any) is embedded
  verbatim into the new one, bounded so it cannot grow without bound across
  many compactions in one cycle (`MAX_SUMMARY_CHARS`, default 6,000). The
  summary's own header line always survives truncation — both "is this
  message the installed carrier" and "is this an earlier carrier" depend on
  it starting with that header.
- **Plain excerpt.** Every other worth-compacting candidate (and the
  carrier candidate, when no summary fits) gets a bounded head/tail excerpt
  with a `[…compacted…]` marker.
- Once a new carrier installs successfully, any earlier carrier still in
  history is retired to a short placeholder — there is at most one live
  carrier at any time, regardless of how many compactions have run in the
  cycle.

## Compaction state is a flag, never inferred from text

Which message is the current live carrier, and which messages have already
been touched by compaction, is tracked on two internal keys the code sets
on the message dict itself: `_compaction_touched` and
`_compaction_carrier`. Neither key is ever read from a message's own text —
an ordinary tool result (e.g. a `cat` of this very module) can legitimately
contain the literal strings `"[Compaction summary"` or `"[…compacted…]"`,
and text-matching would wrongly treat such a result as already compacted.

Both keys are internal-only: `litellm_provider.py`'s `_ALLOWED_MSG_KEYS` /
`_ANTHROPIC_EXTRA_KEYS` allowlist strips any key not on it before a request
is sent, so neither reaches a provider.

## Reasoning payloads

A thinking model's response can carry a reasoning payload much larger than
its visible `content`. This runtime supports exactly one such field:

- **`reasoning_content`** (the host's provider, qwen via LiteLLM, is the
  only one observed to populate it — 590/637 `llm_calls` prompts on
  2026-09-27) is counted toward the token estimate and cleared from every
  turn compaction touches, exactly as `content` is.
- **`thinking_blocks`** (Anthropic's extended-thinking form, including
  `redacted_thinking` blocks whose payload lives under `data`) is **not**
  supported for shrinking. A message carrying `thinking_blocks` is never a
  compaction candidate — left completely untouched, but still counted
  toward the token estimate as incompressible. Rationale: Anthropic
  requires a signed thinking block accompanying a `tool_calls` turn to be
  replayed unmodified on the next request, and this module has no logic
  that preserves that constraint while shrinking. Full support is a
  tracked follow-up; the host's current provider does not populate this
  field.

## Non-goals

- No summarization model call — latency/queue cost, and heuristic
  extraction must not infer unstated intent.
- No support for shrinking Anthropic `thinking_blocks` (see above).
