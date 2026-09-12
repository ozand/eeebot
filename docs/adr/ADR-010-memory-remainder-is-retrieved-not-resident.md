---
title: The loop retrieves non-resident memory through one bounded FTS5 tool
status: proposed
date: 2026-09-12
authors: [eeebot maintainers]
related: ["#1481", "#1469", "#1472", "#1480"]
tags: [runtime, memory, retrieval, fts5]
---

# Status

Proposed for Issue #1481.

# Context

The strict loop prompt currently preserves five labelled resident memory rules and then fills the rest of a 4,000-character section with whole `memory/index.md` lines, newest-first under the cap. Host measurement found 3,573 remainder characters kept and 4,736 characters across 29 entries dropped on every build. This is neither relevance selection nor an actionable route to omitted facts.

The runtime already has a stdlib SQLite FTS5 database with content-addressed text, kind-aware active documents, incremental upsert/retirement, and PR #1480's requirement that an FTS row is not evidence without a matching active `documents` row and `content` row. The loop executor also has an exact declared-versus-registered tool assertion, so adding retrieval is an outward-facing tool-contract decision rather than an internal helper.

# Decision

Keep the five-label resident block byte/behavior equivalent. Replace only the non-resident remainder with a compact inert-data pointer to one explicitly registered `search_memory` tool.

Use the existing existence-index database. Add `memory` as its fourth kind and index every safe regular non-symlink Markdown file under `memory/`, excluding generated `memory/index.md`. A successful pass is incremental and retires removed memory documents. An incomplete/unsafe source pass skips retirement and is unavailable; stale rows are never served as if current.

`search_memory` returns bounded snippets plus safe repo-relative `memory/*.md` paths, allowing full follow-up through the existing `read_file`. Its public result always has one status:

- `complete`: verified search finished; zero hits is a real zero;
- `partial`: more verified matches exist than the hard result limit;
- `unavailable`: index/corpus/search or companion evidence is untrustworthy.

The registered tool result is the decision boundary for memory-dependent reasoning. `unavailable` includes a visible `decision: blocked` reason and the executor prompt/tool contract requires a blocked/deferred outcome rather than treating it as no match. Unrelated decisions that did not request memory are not globally blocked.

This is not a second database, an embedding system, a cap increase, an executor permission expansion, or a rewrite of resident rules, skills, retirement, lessons, or memory documents.

# Consequences

## What gets easier

All indexed memory documents become relevance-queryable without occupying the initial prompt. A genuine empty result is distinguishable from a missing or untrustworthy corpus. Result paths preserve provenance and allow deliberate full reads.

## What gets harder

The existence index now supports a corpus whose failure policy is fail-closed rather than dedup's historical fail-open empty-list API. Callers and tests must preserve explicit status instead of reusing `find_similar()` directly. The registered executor surface grows by one tool and must remain in exact parity with its declaration.

## What does not change

The resident five rules, prompt cap, skills catalogue, retirement brake, memory writers, existing dedup callers, and host deployment process do not change. The `All` memory corpus is not embedded in the prompt.

# Alternatives considered

- Keep the newest-first prompt projection: rejected because 29 measured entries stay unreachable and selection is not relevance.
- Use `exec` plus grep: rejected because it has no structured corpus/status/evidence contract and collapses unavailable into command interpretation.
- Return paths only: rejected because every candidate costs another tool iteration before relevance can be judged.
- Return full files: rejected because one large file recreates prompt pressure.
- Index only `memory/facts/` or only index-listed paths: rejected for v1 because it preserves current unreachable memory classes or makes the lossy catalogue an availability gate. All safe Markdown under `memory/` is a small measured corpus and paths remain visible.
- Treat unreadable individual files as `partial`: rejected because this would serve a corpus known to omit unknown evidence; bounded result truncation is the only partial state.
- Add another SQLite database: rejected because the existing schema is explicitly multi-corpus and already implements the needed evidence lifecycle.

# Test Contract

| Claim | Test | Status |
|---|---|---|
| Resident labels survive unchanged through the prompt-fit ladder | `tests/test_context_resident_labels.py` plus #1481 resident equivalence test | existing + to add |
| Remainder is replaced by an actionable pointer | `tests/test_memory_v2.py` | to add |
| Memory corpus is incremental and retires removed paths | `tests/test_existence_index.py` | to add |
| Dangling/mismatched evidence is unavailable | `tests/test_memory_search.py` | to add |
| Complete zero, unavailable, and bounded partial are distinct | `tests/test_memory_search.py` | to add |
| Paths/snippets are bounded and safe | `tests/test_memory_search.py` | to add |
| Tool declaration and registration remain identical | `tests/test_subagent_manager.py` | to update |
| Unavailable produces a visible blocked decision contract | `tests/test_memory_search.py`, `tests/test_subagent_manager.py` | to add |
| Host build/query cost is measured without live state mutation | PR #1481 measurement receipt | not yet measured |

# References

#1481, #1469, #1472, #1473, #1480, ADR-002, PR #1433.
