# Change: cumulative structural-summary compaction

- **change-id:** 1776-structural-compaction
- **issue:** #1776 (+ #1930 reasoning-payload follow-up)
- **capability:** docs/specs/context-compaction/spec.md
- **role / workstream:** developer / self-evolving runtime

## Problem

Before this change, `nanobot/runtime/context_compaction.py` protected a
fixed *count* of recent messages, exempted assistant turns from compaction
entirely, and dropped old tool results as bare head/tail excerpts with no
account of what was lost. That meant:

- a single oversized recent tool result could never be compacted (a fixed
  message count has no notion of size);
- an assistant turn's own narration (decisions, progress, files touched)
  was silently destroyed once its carrying message aged out, with nothing
  recording what had been there;
- repeated compaction in one cycle re-scanned already-dropped content or
  produced an ever-growing pile of excerpts with no cumulative account;
- a thinking model's `reasoning_content` (present on 590/637 `llm_calls`
  prompts on the host) was invisible to the token estimate and never
  shrunk, so compaction could report "nothing to do" while the real
  history stayed over budget.

## Intended change

- Replace the fixed recent-message count with a token-span cut
  (`_compactable_indices`): walk back from the newest message accumulating
  estimated tokens; everything before the point that would exceed
  `keep_tokens` is a compaction candidate, assistant turns included.
- Replace bare excerpting with a deterministic, evidence-only structural
  summary (`_structural_summary`): explicit decisions, progress narration,
  and file paths observed in tool calls/results, extracted mechanically
  (no model call). One carrier message per cycle holds the live summary;
  older carriers are retired to a short placeholder once a new one installs
  successfully. The summary is cumulative — each round folds the previous
  round's carrier text back in, bounded by `MAX_SUMMARY_CHARS` so it cannot
  grow without bound across a long cycle.
- A candidate only becomes this round's carrier if doing so is a net win
  (its refreshed summary + excerpt is smaller than its own original text);
  every worth-compacting candidate is tried, not only the first, so a small
  first candidate that can't hold the summary doesn't cause it to be
  discarded when a later, larger candidate could carry it.
- Compaction state (which message is the current carrier vs. already
  compacted) is tracked on internal flags the code sets itself
  (`_compaction_touched` / `_compaction_carrier`), never inferred from a
  message's own text — a tool result can legitimately contain the literal
  marker strings.
- `reasoning_content` (the only reasoning field the host's provider, qwen
  via LiteLLM, populates) is counted toward the token estimate and cleared
  from every turn compaction touches, exactly as `content` is. Anthropic's
  extended-thinking forms (`thinking_blocks`, including
  `redacted_thinking`) are out of scope for shrinking in this change: a
  message carrying `thinking_blocks` is left completely untouched by
  compaction (still counted toward the token estimate, never itself
  shrunk) rather than risk breaking the signed-replay contract those
  providers require.

## Acceptance

- [x] A single oversized recent tool result is compactable (token-span cut,
      not message count).
- [x] A tool call and its result are never separated by compaction.
- [x] Every compaction decision (fire, decline, and why) is journalled.
- [x] Repeated compaction in one cycle is cumulative and stays bounded
      (`MAX_SUMMARY_CHARS`), with exactly one live carrier at all times.
- [x] `reasoning_content` is counted and cleared; `thinking_blocks` are
      counted but never shrunk (fail-safe).
- [x] Compaction state is never inferred from message text.
- [x] Fail-open: any exception returns the original history unchanged.

## Out of scope

- Full support for Anthropic's `thinking_blocks`/`redacted_thinking` forms
  (preserving them across a shrink while keeping the tool-call replay
  contract intact) — tracked as a follow-up issue; the host's current
  provider does not populate these fields (0/637 prompts observed), so the
  fail-safe (leave untouched) is the correct behavior for now.
- A model-call-based (non-deterministic) summarizer.
