# Design: FTS5-backed memory search pointer

## Components

1. `nanobot.runtime.existence_index`
   - Add a `memory` corpus builder over all safe `memory/**/*.md` documents except `memory/index.md`.
   - Reuse `_upsert_document`, `_deactivate_missing`, `content`, `documents`, and `docs_fts`.
   - Expose a memory-specific structured search that verifies active `documents` and matching content-addressed `content` rows before returning any hit.
   - Do not reuse duplicate-suspect heuristics for memory facts; FTS/BM25 rank is the result.

2. `nanobot.agent.tools.memory_search.MemorySearchTool`
   - Input: query plus bounded limit.
   - Dependency injection: executor workspace and resolved runtime state root from `SubagentManager`.
   - Output: bounded JSON with `status`, `reason`, `results`, `returned`, `available`, and `limit`.
   - Safe source paths only; snippets are bounded and whitespace-normalized.

3. `nanobot.agent.subagent.SubagentManager`
   - Register the tool in the same visible sequence as `EXECUTOR_TOOL_NAMES`.
   - Preserve the exact declaration-versus-registration assertion.
   - The registered tool result is the decision boundary: `unavailable` carries an explicit `decision=blocked` instruction; `complete` zero results says the zero is genuine; `partial` says more verified rows exist.

4. `nanobot.agent.memory.MemoryStore`
   - Preserve resident extraction and accounting.
   - Remove recency-selected remainder text from the returned prompt.
   - Append a fixed inert-data pointer naming the tool, safe fetch path, and complete/unavailable/partial behavior.
   - Continue accounting for source remainder as searchable rather than prompt-kept/dropped.

## Availability and retirement

A memory reindex is all-or-unavailable per pass. A missing memory directory is a complete empty corpus only when the workspace itself is valid and `memory/` is genuinely absent/empty. Any discovered Markdown source that is symlinked, escapes the workspace, is oversized, undecodable, or unreadable marks the pass unavailable. On an unavailable pass, retirement for kind `memory` is skipped so a transient read error cannot erase previously valid evidence. The search tool returns `unavailable`; it never serves stale rows from a failed pass.

A successful pass re-derives every included path and retires active `memory` rows not seen. A removed file therefore cannot remain an active FTS hit.

## Search contract

FTS terms are extracted and escaped by the existing query helpers. The query is limited to `kind='memory'`. Fetch `limit + 1` verified rows: return at most `limit`; the extra row makes `partial` non-vacuous. Every candidate is validated against an active `documents` row, its hash, and the matching `content` text. Any mismatch makes the whole call unavailable because a partially trustworthy evidence set must not be presented as complete.

Snippets are at most 320 characters per result and total results are capped at 8. Path traversal and absolute paths are rejected both during indexing and before tool serialization.

## Failure-state tests

Write `unavailable`-is-not-empty first. Tests cover missing/corrupt/unindexed databases without manufacturing a live event, dangling FTS/document/content evidence, complete zero, partial hard-limit, removal retirement, unreadable/symlink/path safety, tool JSON, registered parity, and resident byte equivalence. A non-vacuity test uses an isolated copy or monkeypatch and never `git stash`.

## Host measurement

Copy the host memory Markdown corpus read-only into a local temporary directory (or stream it into a local disposable builder) and build a local disposable database. Never pass the live state directory to `reindex`, never write over SSH, never trigger a cycle/unit. Record file count/source bytes, elapsed milliseconds, SQLite main/WAL/SHM bytes after checkpoint/close, and several query latencies. Do not read `/etc/*.env`.
