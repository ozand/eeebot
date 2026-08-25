---
name: memory
description: Two-layer memory system with grep-based recall.
always: true
---

# Memory

## Structure

Interactive workspaces use `memory/MEMORY.md` for long-term facts; it is loaded into context, while `memory/HISTORY.md` remains an append-only event log searched on demand.

Self-evolving workspaces use `memory/index.md` as the catalog. Read individual `memory/facts/*.md` files on demand, and curate the index manually; do not assume a full MEMORY.md is loaded.

Both layouts keep `memory/HISTORY.md` append-only. Search it with grep-style tools or in-memory filters. Each entry starts with [YYYY-MM-DD HH:MM].

## Search Past Events

Choose the search method based on file size:

- Small `memory/HISTORY.md`: use `read_file`, then search in-memory
- Large or long-lived `memory/HISTORY.md`: use the `exec` tool for targeted search

Examples:
- **Linux/macOS:** `grep -i "keyword" memory/HISTORY.md`
- **Windows:** `findstr /i "keyword" memory\HISTORY.md`
- **Cross-platform Python:** `python -c "from pathlib import Path; text = Path('memory/HISTORY.md').read_text(encoding='utf-8'); print('\n'.join([l for l in text.splitlines() if 'keyword' in l.lower()][-20:]))"`

Prefer targeted command-line search for large history files.

## When to Update Memory

For interactive workspaces, write important facts to `memory/MEMORY.md` using `edit_file` or `write_file`.
For self-evolving workspaces, create or update a fact under `memory/facts/` and add its entry to `memory/index.md`.

## Consolidation

Interactive sessions may automatically summarize old conversations into HISTORY.md and extract facts into MEMORY.md. The self-evolving instance does not rely on that automation: the executor curates its index and facts explicitly.
