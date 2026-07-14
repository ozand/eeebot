"""FTS5 existence index: semantic near-duplicate detection for cycle dedup (#750).

The bridge's pre-spawn dedup gate (``nanobot/runtime/bridge.py``) is title-text
matching: a git-log grep of the proposed title (``_task_already_done`` /
``_task_already_done_for_path``) and a bounded-recency scan of recent failures
(``_recent_failure_match``). Both require the words in the NEW proposal's
title to literally overlap the words in a PAST commit subject or result
title. Semantic near-duplicates slip through: ``track_memory.py`` shipped,
then ``monitor_memory.py`` shipped as a separate "success" the same night,
because "monitor RAM and memory usage" shares no *exact* subject-line words
with a commit titled "add track_memory.py to log memory over time".

This module builds a local, qmd-inspired **existence index** over the
self-evolving repo's artifacts using nothing but the stdlib ``sqlite3``
module's built-in FTS5 extension (no new dependency; the eeepc host is
stdlib-only) so the dedup gate can ask "is there already something like
this?" instead of "does this exact wording already exist?".

Storage
-------
SQLite database at ``<state_dir>/existence_index/index.sqlite``, WAL mode,
a busy timeout so a concurrent reindex/read never deadlocks the loop:

- ``content(hash TEXT PRIMARY KEY, text TEXT)`` — content-addressed text
  (sha256 of the text itself), so identical text (e.g. two scripts sharing a
  boilerplate docstring line) is stored once.
- ``documents(kind TEXT, path TEXT, hash TEXT, active INTEGER DEFAULT 1,
  UNIQUE(kind, path))`` — ``kind`` is one of ``script``, ``ledger_title``,
  ``hypothesis``. ``active=0`` marks a soft-deleted document (e.g. a script
  file that no longer exists) without losing history.
- ``docs_fts`` — an FTS5 virtual table ``(kind UNINDEXED, path, text,
  tokenize='porter unicode61')`` mirroring the ACTIVE documents only. Kept in
  sync explicitly on every upsert/deactivate (delete-then-insert on change) —
  no triggers, deliberately simple at this scale.

Indexed corpus (built by :func:`reindex`)
------------------------------------------
- **scripts**: every ``*.py`` directly under ``<selfevo_repo>/scripts/`` and
  ``<selfevo_repo>/surfaces/`` (non-recursive, best-effort — a missing
  directory is simply skipped). Text = the filename with underscores turned
  into spaces, plus the first line of the module docstring (``ast``,
  best-effort; falls back to the first ``#`` comment line if the file
  doesn't parse). ``path`` = the path relative to ``selfevo_repo``.
- **ledger_titles**: titles of past subagent attempts, read from
  ``<state_dir>/subagents/results/*.json`` (bounded to the 500
  most-recently-modified files — this is the durable, title-bearing record;
  ``cycles.jsonl`` itself never carries ``task_title``, only cycle/phase
  bookkeeping, so the results directory is the actual source of past
  proposal titles). ``path`` = the request id.
- **hypotheses**: titles from ``<state_dir>/hypotheses/backlog.json``
  (``entries[].task_title``) and ``<state_dir>/research/hypotheses.json``
  (``[].candidates[].title``). Both optional; each fails open independently.

Matching (:func:`find_similar` / :func:`find_duplicate_script`)
-----------------------------------------------------------------
FTS5/BM25 only narrows the candidate set (OR of the query's 3+-char words,
each double-quoted and escaped). The actual duplicate-suspect decision is
made in Python, over the FTS candidates, using a plain word-overlap rule:
a hit counts as duplicate-suspect iff at least 2 of its 4+-character content
words (from ``path`` + ``text``, after stripping a small generic/stopword
list) also appear among the proposal's own 4+-character content words
(title words + the target path's ``_``-split, ``.py``-stripped basename
tokens). This is deliberately simple — no embeddings, no LLM — and is tuned
so that {"monitor RAM and memory usage" / target ``monitor_memory.py``} DOES
flag an existing ``track_memory.py`` ("track memory usage over time" — 2
shared words: ``memory``, ``usage``), while {"generate a markdown
changelog"} does NOT (zero shared words with ``track_memory.py``).

Only ``kind == 'script'`` hits are treated as duplicate-suspect by the
dedup-gate helper — ``ledger_title``/``hypothesis`` hits are informational
only (future proposer-context use, not wired into the gate in #750).

Kill switch
-----------
``SELFEVO_EXISTENCE_INDEX_ENABLED`` — mirrors the project convention (e.g.
``SELFEVO_DETERMINISTIC_PLANNER_ENABLED``, #739): default ON (absent, ``"1"``,
or garbage all mean enabled); ``"0"`` disables the gate helper entirely
(:func:`find_duplicate_script` returns ``None`` immediately, no DB touched).

Fail-open
---------
Every public function here is best-effort: a missing/corrupt DB is dropped
and rebuilt from scratch; a missing source directory/file is skipped; any
unexpected exception during reindex or matching must never propagate into
the bridge — the caller (:mod:`nanobot.runtime.bridge`) treats any exception
from this module as "no match, proceed."
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any

_INDEX_SUBDIR = "existence_index"
_INDEX_FILENAME = "index.sqlite"

_SCRIPT_SUBDIRS = ("scripts", "surfaces")
_MAX_LEDGER_RESULTS = 500

ENABLED_ENV = "SELFEVO_EXISTENCE_INDEX_ENABLED"

# Small generic/stopword list stripped from BOTH sides of the overlap check
# so common verbs/nouns in task titles ("create", "script", "add", ...)
# don't manufacture false-positive overlaps. Deliberately short — this is a
# precision aid, not a full stopword list.
_GENERIC_WORDS = frozenset({
    "create", "creates", "creating", "write", "writes", "writing",
    "implement", "implements", "implementing", "script", "scripts",
    "the", "and", "for", "that", "this", "with", "from", "into",
    "file", "files", "task", "tasks", "using", "add", "adds", "adding",
    "make", "makes", "making", "new", "python", "module",
})

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS content ("
    " hash TEXT PRIMARY KEY,"
    " text TEXT"
    ")",
    "CREATE TABLE IF NOT EXISTS documents ("
    " kind TEXT NOT NULL,"
    " path TEXT NOT NULL,"
    " hash TEXT,"
    " active INTEGER DEFAULT 1,"
    " UNIQUE(kind, path)"
    ")",
    "CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5("
    " kind UNINDEXED, path, text, tokenize='porter unicode61'"
    ")",
)


def existence_index_enabled() -> bool:
    """Return whether the existence-index dedup gate may run (#750 kill switch).

    Mirrors the style of ``SELFEVO_DETERMINISTIC_PLANNER_ENABLED`` (#739): a
    single small env-backed helper. Any value other than the literal ``"0"``
    (absent, ``"1"``, or garbage) preserves the gate as enabled.
    """
    return os.environ.get(ENABLED_ENV, "1").strip() != "0"


def _index_path(state_dir: Path) -> Path:
    return Path(state_dir) / _INDEX_SUBDIR / _INDEX_FILENAME


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path), timeout=5.0)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=5000")
    return con


def _open_db(state_dir: Path) -> sqlite3.Connection:
    """Open (creating/rebuilding as needed) the existence-index DB.

    Crash-safe: if the file exists but is corrupt (schema creation or a basic
    integrity check fails), it is dropped and recreated from scratch — the
    corpus is fully rebuilt on the next :func:`reindex` call, which is cheap
    at this scale.
    """
    db_path = _index_path(state_dir)
    try:
        con = _connect(db_path)
        for stmt in _SCHEMA:
            con.execute(stmt)
        con.execute("SELECT count(*) FROM documents")
        con.commit()
        return con
    except Exception:
        try:
            con.close()  # type: ignore[possibly-undefined]
        except Exception:
            pass
        for suffix in ("", "-wal", "-shm"):
            with_suppress = db_path.parent / (db_path.name + suffix)
            try:
                with_suppress.unlink(missing_ok=True)
            except Exception:
                pass
        con = _connect(db_path)
        for stmt in _SCHEMA:
            con.execute(stmt)
        con.commit()
        return con


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _fts_replace(con: sqlite3.Connection, kind: str, path: str, text: str) -> None:
    con.execute("DELETE FROM docs_fts WHERE kind = ? AND path = ?", (kind, path))
    con.execute(
        "INSERT INTO docs_fts(kind, path, text) VALUES (?, ?, ?)", (kind, path, text),
    )


def _fts_remove(con: sqlite3.Connection, kind: str, path: str) -> None:
    con.execute("DELETE FROM docs_fts WHERE kind = ? AND path = ?", (kind, path))


def _upsert_document(con: sqlite3.Connection, kind: str, path: str, text: str) -> bool:
    """Insert/update one document. Returns True if content actually changed
    (i.e. FTS/content work was done), False if the hash was unchanged
    (cheap no-op — the incremental-reindex fast path)."""
    new_hash = _hash_text(text)
    row = con.execute(
        "SELECT hash, active FROM documents WHERE kind = ? AND path = ?", (kind, path),
    ).fetchone()
    if row is not None and row[0] == new_hash and row[1] == 1:
        return False  # unchanged and already active — nothing to do

    con.execute(
        "INSERT OR IGNORE INTO content(hash, text) VALUES (?, ?)", (new_hash, text),
    )
    con.execute(
        "INSERT INTO documents(kind, path, hash, active) VALUES (?, ?, ?, 1) "
        "ON CONFLICT(kind, path) DO UPDATE SET hash = excluded.hash, active = 1",
        (kind, path, new_hash),
    )
    _fts_replace(con, kind, path, text)
    return True


def _deactivate_missing(con: sqlite3.Connection, kind: str, seen_paths: set[str]) -> int:
    """Mark active documents of ``kind`` not in ``seen_paths`` as inactive
    and drop them from the FTS index. Returns the count deactivated."""
    rows = con.execute(
        "SELECT path FROM documents WHERE kind = ? AND active = 1", (kind,),
    ).fetchall()
    deactivated = 0
    for (path,) in rows:
        if path in seen_paths:
            continue
        con.execute(
            "UPDATE documents SET active = 0 WHERE kind = ? AND path = ?", (kind, path),
        )
        _fts_remove(con, kind, path)
        deactivated += 1
    return deactivated


# ─── corpus builders ────────────────────────────────────────────────────────


def _script_display_text(py_path: Path) -> str:
    """Return "basename-with-spaces first-docstring-line" for a script file."""
    name = py_path.stem.replace("_", " ").replace("-", " ").strip()
    docline = ""
    try:
        source = py_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        source = ""
    if source:
        try:
            tree = ast.parse(source)
            doc = ast.get_docstring(tree)
            if doc:
                docline = doc.strip().splitlines()[0].strip()
        except Exception:
            docline = ""
        if not docline:
            for line in source.splitlines()[:20]:
                stripped = line.strip()
                if stripped.startswith("#") and not stripped.startswith("#!"):
                    docline = stripped.lstrip("#").strip()
                    break
    return f"{name} {docline}".strip()


def _reindex_scripts(con: sqlite3.Connection, selfevo_repo: Path) -> dict[str, int]:
    counts = {"scripts_indexed": 0, "scripts_unchanged": 0, "scripts_deactivated": 0}
    seen: set[str] = set()
    for subdir in _SCRIPT_SUBDIRS:
        d = selfevo_repo / subdir
        if not d.is_dir():
            continue
        try:
            py_files = sorted(d.glob("*.py"))
        except Exception:
            continue
        for py_path in py_files:
            try:
                rel = str(py_path.relative_to(selfevo_repo))
            except Exception:
                rel = str(py_path)
            seen.add(rel)
            try:
                text = _script_display_text(py_path)
            except Exception:
                continue
            if not text:
                continue
            try:
                changed = _upsert_document(con, "script", rel, text)
            except Exception:
                continue
            if changed:
                counts["scripts_indexed"] += 1
            else:
                counts["scripts_unchanged"] += 1
    try:
        counts["scripts_deactivated"] = _deactivate_missing(con, "script", seen)
    except Exception:
        pass
    return counts


def _reindex_ledger_titles(con: sqlite3.Connection, state_dir: Path) -> dict[str, int]:
    counts = {"ledger_titles_indexed": 0, "ledger_titles_unchanged": 0}
    results_dir = state_dir / "subagents" / "results"
    if not results_dir.is_dir():
        return counts
    try:
        entries = [p for p in results_dir.glob("*.json") if p.is_file()]
    except Exception:
        return counts
    try:
        entries.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    except Exception:
        pass
    for entry in entries[:_MAX_LEDGER_RESULTS]:
        try:
            data = json.loads(entry.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        title = str(data.get("backlog_title") or data.get("task_title") or "").strip()
        if not title:
            continue
        request_id = str(data.get("request_id") or entry.stem)
        try:
            changed = _upsert_document(con, "ledger_title", request_id, title)
        except Exception:
            continue
        if changed:
            counts["ledger_titles_indexed"] += 1
        else:
            counts["ledger_titles_unchanged"] += 1
    return counts


def _reindex_hypotheses(con: sqlite3.Connection, state_dir: Path) -> dict[str, int]:
    counts = {"hypotheses_indexed": 0, "hypotheses_unchanged": 0}

    backlog_path = state_dir / "hypotheses" / "backlog.json"
    if backlog_path.is_file():
        try:
            data = json.loads(backlog_path.read_text(encoding="utf-8"))
        except Exception:
            data = None
        entries = (data or {}).get("entries") if isinstance(data, dict) else None
        for idx, entry in enumerate(entries or []):
            if not isinstance(entry, dict):
                continue
            title = str(entry.get("task_title") or "").strip()
            if not title:
                continue
            doc_path = f"hyp-backlog-{entry.get('task_id') or idx}"
            try:
                changed = _upsert_document(con, "hypothesis", doc_path, title)
            except Exception:
                continue
            if changed:
                counts["hypotheses_indexed"] += 1
            else:
                counts["hypotheses_unchanged"] += 1

    research_path = state_dir / "research" / "hypotheses.json"
    if research_path.is_file():
        try:
            data = json.loads(research_path.read_text(encoding="utf-8"))
        except Exception:
            data = None
        if isinstance(data, list):
            for hyp_idx, hyp_entry in enumerate(data[:50]):
                if not isinstance(hyp_entry, dict):
                    continue
                cycle_id = hyp_entry.get("cycle_id") or hyp_idx
                candidates = hyp_entry.get("candidates") or []
                for cand_idx, cand in enumerate(candidates):
                    if not isinstance(cand, dict):
                        continue
                    title = str(cand.get("title") or "").strip()
                    if not title:
                        continue
                    doc_path = f"hyp-research-{cycle_id}-{cand_idx}"
                    try:
                        changed = _upsert_document(con, "hypothesis", doc_path, title)
                    except Exception:
                        continue
                    if changed:
                        counts["hypotheses_indexed"] += 1
                    else:
                        counts["hypotheses_unchanged"] += 1

    return counts


def reindex(state_dir: Path, selfevo_repo: Path) -> dict[str, Any]:
    """Incrementally (re)build the existence index. Idempotent and crash-safe.

    Returns a counts dict (all keys always present, 0 if nothing to do) for
    logging: ``scripts_indexed``, ``scripts_unchanged``,
    ``scripts_deactivated``, ``ledger_titles_indexed``,
    ``ledger_titles_unchanged``, ``hypotheses_indexed``,
    ``hypotheses_unchanged``. On any unexpected failure, returns a dict with
    an ``"error"`` key instead of raising — callers must fail open.
    """
    state_dir = Path(state_dir)
    selfevo_repo = Path(selfevo_repo)
    counts: dict[str, Any] = {
        "scripts_indexed": 0,
        "scripts_unchanged": 0,
        "scripts_deactivated": 0,
        "ledger_titles_indexed": 0,
        "ledger_titles_unchanged": 0,
        "hypotheses_indexed": 0,
        "hypotheses_unchanged": 0,
    }
    try:
        con = _open_db(state_dir)
    except Exception as exc:
        return {"error": str(exc)}
    try:
        try:
            counts.update(_reindex_scripts(con, selfevo_repo))
        except Exception:
            pass
        try:
            counts.update(_reindex_ledger_titles(con, state_dir))
        except Exception:
            pass
        try:
            counts.update(_reindex_hypotheses(con, state_dir))
        except Exception:
            pass
        con.commit()
    except Exception as exc:
        counts["error"] = str(exc)
    finally:
        try:
            con.close()
        except Exception:
            pass
    return counts


# ─── matching ───────────────────────────────────────────────────────────────

_WORD_RE = re.compile(r"[A-Za-z]{3,}")
_CONTENT_WORD_RE = re.compile(r"[A-Za-z]{4,}")


def _query_words(title: str) -> list[str]:
    return sorted({w.lower() for w in _WORD_RE.findall(title or "")})


def _target_tokens(target_path: str | None) -> list[str]:
    if not target_path:
        return []
    base = Path(target_path).stem  # strips one suffix, e.g. .py
    parts = re.split(r"[_\-./]+", base)
    return [p.lower() for p in parts if len(p) >= 3]


def _escape_fts_term(term: str) -> str:
    return term.replace('"', '""')


def _build_match_query(words: list[str]) -> str:
    terms = [f'"{_escape_fts_term(w)}"' for w in words if w]
    return " OR ".join(terms)


def _content_words(*texts: str) -> set[str]:
    words: set[str] = set()
    for text in texts:
        for w in _CONTENT_WORD_RE.findall(text or ""):
            lw = w.lower()
            if lw not in _GENERIC_WORDS:
                words.add(lw)
    return words


def find_similar(
    state_dir: Path, title: str, target_path: str | None = None, limit: int = 5,
) -> list[dict]:
    """Return up to ``limit`` existing documents that look related to
    ``title``/``target_path``, best match first.

    Each result: ``{"kind", "path", "text", "score", "duplicate_suspect"}``.
    ``score`` is the BM25 score negated so higher-is-better (sqlite's raw
    ``bm25()`` is lower-is-better/negative). ``duplicate_suspect`` is True
    only for ``kind == 'script'`` hits that share at least 2 content words
    (4+ chars, generic words stripped) with the proposal — see the module
    docstring for the worked positive/negative example. A hit whose ``path``
    is exactly the proposal's own ``target_path`` is never flagged here —
    that "same file" case is already the job of the narrower, git-scoped
    ``_task_already_done_for_path`` check in ``bridge.py`` (#736); this
    module's job is catching a DIFFERENT existing artifact that duplicates
    the same intent.

    Fail-open: any exception returns ``[]``.
    """
    state_dir = Path(state_dir)
    query_words = _query_words(title) + _target_tokens(target_path)
    query_words = sorted(set(query_words))
    if not query_words:
        return []
    match_query = _build_match_query(query_words)
    if not match_query:
        return []

    proposal_words = _content_words(title or "") | {
        t for t in _target_tokens(target_path) if len(t) >= 4
    }

    try:
        con = _open_db(state_dir)
    except Exception:
        return []

    results: list[dict] = []
    try:
        rows = con.execute(
            "SELECT kind, path, text, bm25(docs_fts) AS score "
            "FROM docs_fts WHERE docs_fts MATCH ? ORDER BY bm25(docs_fts) LIMIT ?",
            (match_query, max(1, limit)),
        ).fetchall()
        for kind, path, text, score in rows:
            duplicate_suspect = False
            if kind == "script" and path != target_path:
                hit_words = _content_words(path or "", text or "")
                overlap = proposal_words & hit_words
                duplicate_suspect = len(overlap) >= 2
            results.append({
                "kind": kind,
                "path": path,
                "text": text,
                "score": -float(score),
                "duplicate_suspect": duplicate_suspect,
            })
    except Exception:
        return []
    finally:
        try:
            con.close()
        except Exception:
            pass
    return results


def find_duplicate_script(
    state_dir: Path, selfevo_repo: Path, title: str, target_path: str | None = None,
) -> str | None:
    """Convenience wrapper for the bridge's pre-spawn dedup gate.

    Honors :func:`existence_index_enabled`, incrementally reindexes, then
    looks for a duplicate-suspect ``script`` hit for ``title``/``target_path``.
    Returns the matched script's repo-relative path, or ``None`` if disabled,
    no match, or any error occurred (fail-open — this must never raise or
    block a cycle it failed to evaluate).
    """
    if not title:
        return None
    if not existence_index_enabled():
        return None
    try:
        reindex(Path(state_dir), Path(selfevo_repo))
        hits = find_similar(Path(state_dir), title, target_path=target_path, limit=5)
        for hit in hits:
            if hit.get("kind") == "script" and hit.get("duplicate_suspect"):
                return hit.get("path")
        return None
    except Exception:
        return None
