# Change: FTS5-backed memory search pointer

- **change-id:** 1481-memory-search-pointer
- **story_id:** https://github.com/ozand/eeebot/issues/1481
- **capabilities:** `docs/specs/subagent-bridge/spec.md`, `docs/specs/self-evolving-runtime/spec.md`

## Problem

The loop prompt keeps five labelled resident memory rules, then fills the remaining memory budget with whole `memory/index.md` lines in reverse chronological order. On the measured host corpus this embeds 3,573 characters while permanently dropping 4,736 characters from 29 entries. Selection is recency under a character cap, not relevance, and an omitted entry is indistinguishable from an unsearched corpus.

## Intended change

Keep the resident block byte-equivalent. Replace only its remainder with a compact inert-data pointer to a deliberately registered `search_memory` executor tool. Reuse the existing SQLite FTS5 database and its content-addressed `content` plus active `documents` evidence contract. Add `memory` as the fourth corpus kind; do not create a second database.

`search_memory` returns JSON with one of three explicit states:

- `complete`: the corpus was indexed and searched; zero results is a real zero;
- `unavailable`: the index/corpus/evidence/search could not be trusted;
- `partial`: valid results are returned, but the configured result limit truncated more verified matches.

The tool returns bounded snippets directly and a safe repo-relative Markdown path. A caller may use the existing `read_file` tool for a full fetch. The first version indexes all regular, non-symlink `memory/**/*.md` files except generated `memory/index.md`; this includes resident discipline files, facts, history and archive material without using the index catalogue as an availability gate. Stored paths are repo-relative POSIX paths and must remain under `memory/`.

A tool call that returns `unavailable` is a visible fail-closed boundary for memory-dependent reasoning: the tool result instructs the executor to report `outcome: blocked` with the supplied reason instead of treating the response as zero hits. This is enforced in the tool description/prompt contract and covered through the real registered tool path. It does not block unrelated decisions that did not request memory.

## Why snippets plus paths

Bounded snippets make the first retrieval actionable in one tool call and keep result size predictable. Returning the path preserves provenance and lets the executor fetch more context with the already-registered `read_file`. A paths-only result was rejected because it spends a second tool iteration before the model can judge relevance; full-document results were rejected because a single large file would recreate prompt pressure.

## `partial` boundary

A result is `partial` only when more verified FTS matches exist than the hard result limit. An unreadable/escaping/invalid source during reindex makes the memory corpus unavailable for that pass and skips retirement; it does not produce a misleading partial corpus. Missing active-document or matching content evidence during search is `unavailable`, following PR #1480.

## Acceptance

- Five resident labels and the #1472 overflow sweep remain unchanged.
- No memory remainder entries are embedded in the initial prompt; the pointer names `search_memory` and all three statuses.
- The existing database has a kind-aware, incremental `memory` corpus and retires removed files without stale active FTS rows.
- Search distinguishes genuine zero, unavailable evidence/index/corpus, and limit-truncated partial results.
- Results are bounded and contain only safe `memory/*.md` paths plus snippets.
- `unavailable` is visibly blocked/deferred by the executor contract, never described as empty memory.
- Tool declaration, runtime registration, bridge prompt declaration and H2 parity tests agree.
- Host measurement is read-only and reports source bytes/files, disposable build time, database size and query latency; no live index is built.

## Out of scope

Resident-rule edits, skills catalogue, retirement brake, lesson-corpus redesign, embeddings, cap increase, memory-file rewrites, live cycles, deployment, host state mutation, and executor confinement changes.

## Host sizing measurement (read-only)

Measured as `eeepc-agent` against the current 57 regular non-symlink Markdown files under the instance `memory/` tree, excluding `index.md`, while building only a disposable `/tmp` SQLite database:

```text
source files: 57
source bytes: 329,413
largest file: 95,660 bytes
cold disposable build: 763.276 ms
closed/checkpointed database: 868,352 bytes
query samples: 2.876–22.142 ms (limit 9)
```

The live existence index, memory files, runtime state, services and cycles were not modified. No `/etc/*.env` file was read. The result is a direct host measurement on the constrained machine, not an extrapolation from desktop hardware.
