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
  or the retired ``hypothesis`` (#1219; rows stay, inactive). ``active=0``
  marks a soft-deleted document (a script file that no longer exists, an
  attempt that is no longer evidence, a corpus that is no longer built)
  without losing history.
- ``docs_fts`` — an FTS5 virtual table ``(kind UNINDEXED, path, text,
  tokenize='porter unicode61')`` mirroring the ACTIVE documents only. Kept in
  sync explicitly on every upsert/deactivate (delete-then-insert on change) —
  no triggers, deliberately simple at this scale.

Indexed corpus (built by :func:`reindex`)
------------------------------------------
- **scripts**: every ``*.py`` directly under ``<selfevo_repo>/scripts/``,
  ``<selfevo_repo>/surfaces/`` and ``<selfevo_repo>/tests/`` (non-recursive,
  best-effort — a missing directory is simply skipped; ``tests/`` added in
  #757 so tests-for-X proposals can be deduped against existing test files). Text = the filename with underscores turned
  into spaces, plus the first line of the module docstring (``ast``,
  best-effort; falls back to the first ``#`` comment line if the file
  doesn't parse). ``path`` = the path relative to ``selfevo_repo``.
- **ledger_titles**: titles of past subagent attempts that PRODUCED
  something, read from ``<state_dir>/subagents/results/*.json`` plus the
  ``result-*.json`` files of ``<state_dir>/subagents/archive/`` (results
  migrate there within the hour, #1176; bounded to the 500 most-recently-
  modified files across both — this is the durable, title-bearing record;
  ``cycles.jsonl`` itself never carries ``task_title``, only cycle/phase
  bookkeeping). ``path`` = the request id. Since #1215 a title is indexed
  only when its attempt integrated (``rollback.integrated``, or the ledger's
  ``outcome: success`` for results predating the rollback record) or its
  ``target_path`` exists on ``origin/main`` — a refused proposal's title
  asserts an attempt, not an artifact, and indexing it let the first refusal
  of a subject suppress every later one. Documents that no longer qualify
  are deactivated on reindex, like deleted scripts.
- **hypotheses** — RETIRED (#1219). Titles from ``hypotheses/backlog.json``
  and the writer-less ``research/hypotheses.json`` used to be indexed here. A
  hypothesis is a statement of something not yet done, so its title is never
  evidence that an artifact exists; the corpus was never consulted by the
  gate and only displaced documents that are. Existing rows are deactivated
  by the retirement contract below; nothing is indexed under this kind.

Retirement contract (:data:`_CORPORA`, #1219)
----------------------------------------------
Each corpus builder returns the set of document paths it re-derived from a
living source in this pass, and :func:`reindex` deactivates every other
active document of that kind — for every kind ever held, including one that
no longer has a builder. Retirement is therefore a property of the index,
not of each builder: a source whose writer disappears retires with it
instead of leaving its documents active forever.

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

Matching is kind-aware since #757: the word-overlap rule above applies to
``kind == 'script'`` hits, but a proposal whose derived intent
(:func:`derive_intent`) is ``test-for(<subject>)`` (target under ``tests/``
or a test-suite-for-X title) is NEVER flagged against a ``scripts/``/
``surfaces/`` hit — a test-suite title must name the script it tests, so
that overlap is guaranteed and meaningless; writing tests for existing code
is new work. Such a proposal may only be flagged against another test
artifact (a ``tests/`` path) or a prior ``ledger_title`` attempt that is
itself test-for the same subject. (``hypothesis`` hits were "informational
only" from #750 until #1219 retired the corpus — never wired into the gate,
but present in the BM25 top-k the gate reads, where they displaced the
documents it can act on.)

Kill switch
-----------
``SELFEVO_EXISTENCE_INDEX_ENABLED`` — mirrors the project convention (e.g.
``SELFEVO_LLM_PROPOSER_ENABLED``, #707): default ON (absent, ``"1"``,
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
import subprocess
from pathlib import Path
from typing import Any

_INDEX_SUBDIR = "existence_index"
_INDEX_FILENAME = "index.sqlite"

# #757: "tests" is indexed too so a tests-for-X proposal can be deduped
# against an EXISTING test file — but tests/ hits only ever count as
# duplicate-suspect for tests-for-X proposals (kind-aware matching below),
# never for ordinary script proposals.
_SCRIPT_SUBDIRS = ("scripts", "surfaces", "tests")
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

    Mirrors the style of ``SELFEVO_LLM_PROPOSER_ENABLED`` (#707): a
    single small env-backed helper. Any value other than the literal ``"0"``
    (absent, ``"1"``, or garbage) preserves the gate as enabled.
    """
    return os.environ.get(ENABLED_ENV, "1").strip() != "0"


def _index_path(state_dir: Path) -> Path:
    return Path(state_dir) / _INDEX_SUBDIR / _INDEX_FILENAME


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path), timeout=5.0)
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=5000")
    except Exception:
        # Close before re-raising: a corrupt file fails the first PRAGMA,
        # and _open_db's rebuild path must be able to unlink the file —
        # on Windows the traceback would otherwise keep this frame (and the
        # open handle) alive, turning the unlink into a locked-file no-op
        # and the rebuild into a hard error.
        try:
            con.close()
        except Exception:
            pass
        raise
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


def _reindex_scripts(con: sqlite3.Connection, selfevo_repo: Path) -> tuple[dict[str, int], set[str]]:
    """Index the script corpus. Returns ``(counts, evidence)`` where
    ``evidence`` is the set of repo-relative paths that exist right now —
    :func:`reindex` retires every other active ``script`` document."""
    counts = {"scripts_indexed": 0, "scripts_unchanged": 0}
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
            # POSIX separators regardless of host (#798): stored paths are
            # compared against proposal target_paths (always '/'-separated)
            # and prefix-matched against 'tests/' — a Windows checkout would
            # otherwise store 'scripts\\foo.py' and defeat both the
            # same-path carve-out and the tests/ kind detection.
            try:
                rel = py_path.relative_to(selfevo_repo).as_posix()
            except Exception:
                rel = str(py_path).replace("\\", "/")
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
    return counts, seen


def _origin_main_paths(selfevo_repo: Path) -> set[str] | None:
    """Return every path in the tree of ``origin/main`` (``git ls-tree``), or
    ``None`` when that cannot be determined (no git, no remote-tracking ref,
    timeout). Callers fall back to the working tree on ``None``."""
    try:
        proc = subprocess.run(
            [
                "git", "-c", f"safe.directory={Path(selfevo_repo).as_posix()}", "-C", str(selfevo_repo),
                "ls-tree", "-r", "--name-only", "origin/main",
            ],
            capture_output=True, text=True, timeout=15,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return {line.strip() for line in proc.stdout.splitlines() if line.strip()}


def _result_entries(state_dir: Path) -> list[tuple[Path, float]]:
    """Newest-first ``(path, mtime)`` of result artifacts across the live
    ``subagents/results/`` and the flat ``subagents/archive/``, bounded to
    :data:`_MAX_LEDGER_RESULTS` AFTER sorting (bounding an unsorted listing
    would pick an arbitrary slice of the archive's thousands of files).

    Results migrate out of ``results/`` within the hour (#1176), so the live
    directory alone holds only the last few attempts; the archive keeps the
    original filenames (``result-<id>.json`` next to ``request-<id>.json``),
    so only ``result-`` files are taken from it.
    """
    subagents = state_dir / "subagents"
    stamped: list[tuple[float, Path]] = []
    for directory, prefix in ((subagents / "results", ""), (subagents / "archive", "result-")):
        try:
            if not directory.is_dir():
                continue
            with os.scandir(str(directory)) as it:
                for entry in it:
                    if not entry.name.endswith(".json"):
                        continue
                    if prefix and not entry.name.startswith(prefix):
                        continue
                    try:
                        if entry.is_file():
                            stamped.append((entry.stat().st_mtime, Path(entry.path)))
                    except Exception:
                        continue
        except Exception:
            continue
    stamped.sort(key=lambda x: (x[0], x[1].name), reverse=True)
    return [(path, mtime) for mtime, path in stamped[:_MAX_LEDGER_RESULTS]]


def _reindex_ledger_titles(
    con: sqlite3.Connection, state_dir: Path, selfevo_repo: Path | None = None,
) -> tuple[dict[str, int], set[str]]:
    """Index the titles of past attempts that PRODUCED something (#1215).
    Returns ``(counts, evidence)``; :func:`reindex` retires every active
    ``ledger_title`` document not in ``evidence`` (#1219 contract).

    A ``ledger_title`` document is evidence that an artifact exists only
    when the attempt behind it integrated — its result carries
    ``rollback.integrated: true`` (written in the same step as the ledger's
    ``outcome: success`` row; the ledger is consulted for results that
    predate the rollback record) — or when the attempt's own ``target_path``
    now exists on ``origin/main`` (it arrived some other way; the artifact is
    real regardless of who shipped it). A refused/blocked/no-commit attempt
    asserts only that an attempt happened; indexing its title let the first
    refusal of a subject seed every later one (live: 75 of 83 ``ledger_title``
    suppressions matched an attempt that produced nothing, and
    ``tests/test_verify_and_proof.py`` was refused 4 times as a duplicate of
    a file never created).

    Retirement: every active ``ledger_title`` document whose attempt is not
    re-verified as evidence in this pass is deactivated (same
    :func:`_deactivate_missing` mechanism the ``script`` corpus uses for
    deleted files). That heals an already-poisoned index on the first
    reindex after deploy, and treats an attempt that can no longer be found
    in ``results/``/``archive/`` as unknown rather than as proof — an
    integrated artifact stays covered by the ``script`` corpus, since the
    file exists. A document is reactivated if its attempt later integrates.
    """
    counts = {
        "ledger_titles_indexed": 0,
        "ledger_titles_unchanged": 0,
        "ledger_titles_not_integrated": 0,
        "ledger_titles_deactivated": 0,
    }
    seen_ids: set[str] = set()  # dedupe: every request_id classified this pass
    evidence_ids: set[str] = set()  # subset that qualified; everything else is retired below
    success_cycles: set[str] | None = None  # lazy: only for rollback-less results
    main_paths: set[str] | None = None  # lazy: only for a non-integrated attempt with a target
    main_paths_resolved = False

    for entry, _mtime in _result_entries(state_dir):
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
        if request_id in seen_ids:
            continue  # newest copy wins (results/ and archive/ may both hold it)
        seen_ids.add(request_id)

        rollback = data.get("rollback")
        if isinstance(rollback, dict) and "integrated" in rollback:
            integrated = bool(rollback.get("integrated"))
        else:
            if success_cycles is None:
                try:
                    from nanobot.runtime.cycle_ledger import successful_cycle_ids
                    success_cycles = successful_cycle_ids(state_dir)
                except Exception:
                    success_cycles = set()
            cycle_id = str(data.get("cycle_id") or "")
            integrated = bool(cycle_id) and cycle_id in (success_cycles or set())

        is_evidence = integrated
        if not is_evidence and selfevo_repo is not None:
            target = str(data.get("target_path") or "").strip().replace("\\", "/")
            if target:
                if not main_paths_resolved:
                    main_paths = _origin_main_paths(selfevo_repo)
                    main_paths_resolved = True
                if main_paths is not None:
                    is_evidence = target in main_paths
                else:
                    try:
                        is_evidence = (selfevo_repo / target).is_file()
                    except Exception:
                        is_evidence = False

        if not is_evidence:
            counts["ledger_titles_not_integrated"] += 1
            continue
        evidence_ids.add(request_id)
        try:
            changed = _upsert_document(con, "ledger_title", request_id, title)
        except Exception:
            continue
        if changed:
            counts["ledger_titles_indexed"] += 1
        else:
            counts["ledger_titles_unchanged"] += 1
    return counts, evidence_ids


# ─── retirement contract (#1219) ─────────────────────────────────────────────
#
# Every kind the index has ever held, with the builder that produces its
# evidence this pass — or ``None`` for a kind that is no longer built. A
# builder returns ``(counts, evidence)``: ``evidence`` is the set of document
# paths it re-derived from a living source, and :func:`reindex` deactivates
# every other active document of that kind. A kind with no builder therefore
# retires completely on the next reindex.
#
# Why a contract and not a per-builder rule: retirement used to live inside
# each builder, so a builder without one (``hypothesis``) had no retirement
# path, and a source whose writer was deleted (``state/research/``, #924)
# left every document it had ever produced active — 6,214 ``hyp-research-*``
# documents, 98.4% matching nothing on the host, 71% of the index.
#
# ``hypothesis`` is retired, not rebuilt: a hypothesis is by definition a
# statement of something NOT yet done, so a hypothesis title is never
# evidence that an artifact exists — the backlog half exactly as much as the
# frozen research half. The corpus also had no consumer (never
# ``duplicate_suspect``, filtered out of ``related_scripts``) and only took
# BM25 slots from documents the gate can act on: on 400 recent proposals it
# pushed a real script duplicate out of the gate's top-5 ten times and cost
# the proposer 536 reuse hints. Non-goal: "has this already been PROPOSED?"
# is a legitimate question, and a different one from "does this EXIST?" — if
# it is wanted it needs its own mechanism, not this index with a hypothesis
# corpus.
_CORPORA: tuple[tuple[str, str], ...] = (
    ("script", "scripts"),
    ("ledger_title", "ledger_titles"),
    ("hypothesis", "hypotheses"),
)


def reindex(state_dir: Path, selfevo_repo: Path) -> dict[str, Any]:
    """Incrementally (re)build the existence index. Idempotent and crash-safe.

    Returns a counts dict (all keys always present, 0 if nothing to do) for
    logging: ``scripts_indexed``, ``scripts_unchanged``,
    ``scripts_deactivated``, ``ledger_titles_indexed``,
    ``ledger_titles_unchanged``, ``ledger_titles_not_integrated``,
    ``ledger_titles_deactivated`` (#1215), ``hypotheses_deactivated``
    (#1219 — the retired corpus; non-zero only until its documents are
    gone). On any unexpected failure, returns a dict with an ``"error"`` key
    instead of raising — callers must fail open.
    """
    state_dir = Path(state_dir)
    selfevo_repo = Path(selfevo_repo)
    counts: dict[str, Any] = {
        "scripts_indexed": 0,
        "scripts_unchanged": 0,
        "scripts_deactivated": 0,
        "ledger_titles_indexed": 0,
        "ledger_titles_unchanged": 0,
        "ledger_titles_not_integrated": 0,
        "ledger_titles_deactivated": 0,
        "hypotheses_deactivated": 0,
    }
    try:
        con = _open_db(state_dir)
    except Exception as exc:
        return {"error": str(exc)}
    try:
        evidence: dict[str, set[str]] = {kind: set() for kind, _ in _CORPORA}
        try:
            built, evidence["script"] = _reindex_scripts(con, selfevo_repo)
            counts.update(built)
        except Exception:
            pass
        try:
            built, evidence["ledger_title"] = _reindex_ledger_titles(con, state_dir, selfevo_repo)
            counts.update(built)
        except Exception:
            pass
        # Retire whatever no builder re-derived this pass — for EVERY kind,
        # including ``hypothesis``, which has no builder any more.
        for kind, prefix in _CORPORA:
            try:
                counts[f"{prefix}_deactivated"] = _deactivate_missing(con, kind, evidence[kind])
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

# #757: titles that announce a test-suite-for-subject intent, e.g.
# "Create test suite for approval truth normalization script" or
# "Create unit tests for backlog_health script". The captured remainder is
# the subject being tested.
# The subject capture stops at " script" / " to ..." / punctuation — live
# titles carry a descriptive tail ("... script to verify cycle summary
# prepending") whose words ("verify", "cycle", ...) are NOT part of the
# subject; capturing them let unrelated test suites share >=2 subject words
# and re-created the recent-failure cascade #757 was meant to kill.
_TEST_TITLE_RE = re.compile(
    r"\b(?:unit\s+)?tests?(?:\s+suite|\s+coverage)?\s+for\s+(?:the\s+)?"
    r"(.+?)(?:\s+script\b|\s+to\s+|[.,;:]|$)",
    re.IGNORECASE,
)


def derive_intent(
    title: str, target_path: str | None = None,
) -> tuple[str, frozenset[str] | str] | None:
    """Derive a structured dedup intent ``(action_class, key)`` from a
    proposal's title and (optional) target path (#757).

    The dedup chain's word-bag comparisons manufacture false positives for
    "write tests for X" proposals: a test-suite title MUST name the script it
    tests, so word overlap with that script is guaranteed — yet writing tests
    for existing code is legitimate new work. This helper keys duplicate
    decisions on structured intent instead:

    - ``("test-for", frozenset(subject_words))`` when ``target_path`` is
      under ``tests/`` or the title matches a test-suite-for-subject pattern
      ("test suite for X" / "unit tests for X" / "tests for X"). Subject
      words come from the title remainder after "for" AND the target
      basename (``tests/test_foo_bar.py`` → ``{"foo", "bar"}``), generic
      words stripped.
    - ``("change", <target_path>)`` otherwise, when a target path is known —
      create vs. extend are indistinguishable from a title, and the pair is
      only ever compared for equality, so one class keyed by path suffices.
    - ``None`` when neither can be derived (no target path, no test pattern,
      or an empty subject) — callers MUST fall back to their existing
      word-overlap behavior (fail-open discipline, as elsewhere here).
    """
    try:
        target = (target_path or "").strip().replace("\\", "/")
        is_test_target = target.startswith("tests/") or "/tests/" in target
        m = _TEST_TITLE_RE.search(title or "")
        if is_test_target or m:
            subject: set[str] = set()
            if m:
                subject |= {
                    w for w in _content_words(m.group(1))
                    if w not in ("test", "tests", "suite", "unit")
                }
            if is_test_target:
                stem = Path(target).stem
                stem = re.sub(r"^test_?", "", stem)
                subject |= {
                    t for t in _target_tokens(stem) if len(t) >= 4
                } - _GENERIC_WORDS
            if not subject:
                return None
            return ("test-for", frozenset(subject))
        if target:
            return ("change", target)
        return None
    except Exception:
        return None


def intents_match(
    a: tuple[str, frozenset[str] | str] | None,
    b: tuple[str, frozenset[str] | str] | None,
) -> bool:
    """Return True iff two derived intents name the same (action, target) (#757).

    ``change`` intents match on exact target-path equality. ``test-for``
    intents match when one subject word-set contains the other (the same
    subject derived from a path basename vs. a fuller title) or they share
    at least 2 subject words — a single incidental shared word (e.g. two
    test suites both mentioning "backlog") is NOT the same target.
    """
    try:
        if not a or not b or a[0] != b[0]:
            return False
        if a[0] == "test-for":
            wa, wb = a[1], b[1]
            if not isinstance(wa, frozenset) or not isinstance(wb, frozenset):
                return False
            if not wa or not wb:
                return False
            return wa <= wb or wb <= wa or len(wa & wb) >= 2
        return a[1] == b[1]
    except Exception:
        return False


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
    the same intent — but only for proposals that do NOT name a concrete
    target script themselves (see the #798 carve-out below).

    Concrete-target carve-out (#798): when the proposal's derived intent is
    ``("change", <target_path>)`` — it names one concrete non-test script —
    a ``script`` hit on a DIFFERENT path is never duplicate-suspect on word
    overlap alone: sibling artifacts share naming vocabulary by
    construction (live false positive: 'Archive unused
    collect_telegram_live_proof script' targeting
    ``scripts/collect_telegram_live_proof.py`` was flagged against
    ``scripts/validate_telegram_live_proof.py``, whose skip row then
    poisoned the whole decay lane via the recent-failure matcher). The
    different-artifact word-overlap flagging remains for proposals without
    a concrete target path.

    Kind-aware carve-out (#757): when the proposal's derived intent
    (:func:`derive_intent`) is ``test-for(<subject>)`` — the target is under
    ``tests/`` or the title is a test-suite-for-X pattern — a ``script`` hit
    under ``scripts/``/``surfaces/`` is NEVER duplicate-suspect: a test-suite
    title must name the script it tests, so that overlap is guaranteed, and
    writing tests for existing code is new work. Such a proposal may only be
    flagged against another test artifact (a ``script`` hit whose path is
    under ``tests/``) or a prior ``ledger_title`` whose own derived intent is
    test-for the same subject. Symmetrically, a NON-test proposal is never
    flagged against a ``tests/`` hit.

    Fail-open: any exception returns ``[]``.
    """
    state_dir = Path(state_dir)
    # Indexed paths are stored '/'-separated (see _reindex_scripts); normalize
    # the proposal's target the same way so the path != target_path carve-outs
    # below hold on any host (#798 review finding).
    target_path = (target_path or "").strip().replace("\\", "/") or None
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
    # #757: structured intent for the kind-aware tests-for-X carve-out.
    proposal_intent = derive_intent(title or "", target_path)
    proposal_is_test = proposal_intent is not None and proposal_intent[0] == "test-for"
    # #798: the proposal names one concrete non-test script — it is about
    # THAT artifact, so different-path script hits are never suspect (see
    # the concrete-target carve-out in the docstring).
    proposal_targets_script = (
        proposal_intent is not None and proposal_intent[0] == "change"
    )

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
            hit_is_test = (path or "").startswith("tests/") or "/tests/" in (path or "")
            if proposal_is_test:
                # #757: a tests-for-X proposal only duplicates other TEST
                # artifacts — a scripts/-kind hit is guaranteed word overlap
                # (the title names the script under test), not a duplicate.
                if kind == "script" and hit_is_test and path != target_path:
                    hit_words = _content_words(path or "", text or "")
                    duplicate_suspect = len(proposal_words & hit_words) >= 2
                elif kind == "ledger_title":
                    # A prior attempt title counts only if it is itself
                    # test-for the SAME subject (derive_intent on the title).
                    duplicate_suspect = intents_match(
                        proposal_intent, derive_intent(text or ""),
                    )
            elif (
                kind == "script" and path != target_path and not hit_is_test
                # #798: concrete-target proposals are never flagged against a
                # different script on word overlap alone.
                and not proposal_targets_script
            ):
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


def related_scripts(state_dir: Path, selfevo_repo: Path, query: str, limit: int = 12) -> list[str]:
    """Repo-relative scripts/ paths most RELEVANT to `query`, best-first (#840).

    Thin wrapper over find_similar for the proposer's reuse-ranking: reindexes,
    runs the FTS query, keeps only script-kind, non-test hits, de-dups preserving
    rank. Honors existence_index_enabled(); fail-open to [] (disabled, no match,
    or any error) so the caller falls back to its default ordering.
    """
    if not query or not existence_index_enabled():
        return []
    try:
        reindex(Path(state_dir), Path(selfevo_repo))
        hits = find_similar(
            Path(state_dir), query, target_path=None, limit=max(1, limit * 2),
        )
        out: list[str] = []
        seen: set[str] = set()
        for hit in hits:
            if hit.get("kind") != "script":
                continue
            path = str(hit.get("path") or "")
            if not path.startswith("scripts/"):
                continue
            if "tests/" in path:
                continue
            basename = path.rsplit("/", 1)[-1]
            if basename.startswith("test_"):
                continue
            if path in seen:
                continue
            seen.add(path)
            out.append(path)
            if len(out) >= limit:
                break
        return out
    except Exception:
        return []


def find_duplicate_script(
    state_dir: Path, selfevo_repo: Path, title: str, target_path: str | None = None,
) -> str | None:
    """Convenience wrapper for the bridge's pre-spawn dedup gate.

    Honors :func:`existence_index_enabled`, incrementally reindexes, then
    looks for a duplicate-suspect hit for ``title``/``target_path``: a
    ``script`` hit, or (for tests-for-X proposals, #757) a ``ledger_title``
    hit whose own intent is test-for the same subject. Proposals that name a
    concrete non-test target script are never matched against a
    different-path script (#798 — see :func:`find_similar`). Returns the matched
    document's path (repo-relative script path, or the prior attempt's
    request id), or ``None`` if disabled, no match, or any error occurred
    (fail-open — this must never raise or block a cycle it failed to
    evaluate).
    """
    if not title:
        return None
    if not existence_index_enabled():
        return None
    try:
        reindex(Path(state_dir), Path(selfevo_repo))
        hits = find_similar(Path(state_dir), title, target_path=target_path, limit=5)
        for hit in hits:
            if hit.get("duplicate_suspect"):
                return hit.get("path")
        return None
    except Exception:
        return None
