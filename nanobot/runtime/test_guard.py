"""#1119: deterministic test-weakening detector ("never weaken a test").

The gate stack had no defense against the classic reward-hack path: a
candidate commit that makes the smoke gate pass by weakening the tests
instead of fixing the code (pattern borrowed from dream-machine's
adversarial "reward-hack check", https://github.com/ruvnet/dream-machine).
This module runs BEFORE the smoke gate is allowed to decide a cycle's fate
— same placement discipline as ``bridge._validate_mutation_surfaces`` (#678
F1) — so a subagent under pressure to "make tests green" cannot delete a
failing test, comment out an assertion, or narrow a parametrization and
have the smoke gate happily pass.

Pure and deterministic: :func:`evaluate` is exactly ``(repo_path, base_ref,
head_ref) -> structured verdict``. No LLM calls. Driven entirely by ``git
diff --find-renames --name-status`` restricted to test files (``tests/``,
``test_*.py``, ``*_test.py``) plus stdlib :mod:`ast` parsing of the old/new
blob contents at each ref — files are never executed or imported, only
parsed as syntax trees via ``git show <ref>:<path>``. Bounded runtime: one
``git diff`` + one ``git show`` per changed test file.

Hard signals (block, ``blocked: True``):

- an existing test file is deleted (status ``D`` — i.e. NOT collapsed into
  a ``git`` rename pairing, see below) while the cycle ALSO touches
  non-test files;
- net loss of ``assert`` statements + ``pytest.raises(...)`` calls in a
  test file that still exists at ``head_ref``. Any touched EXISTING test
  file is, by construction, part of the bounded gate's own test selection
  (``gate._select_gate_tests`` always adds a changed ``tests/test_*.py``
  file to its own selection) — the issue's "file appears in the bounded
  gate's selected tests" scoping is therefore automatically satisfied for
  every file this function considers, with no extra plumbing needed;
- a ``@pytest.mark.skip`` / ``skipif`` / ``xfail`` decorator added to a
  test function that existed, unmarked, at ``base_ref``.

Soft signals (recorded only, never block):

- a statically-countable ``@pytest.mark.parametrize`` argument list that
  shrank;
- an assert/raises count drop in a file git ALSO reports as renamed
  (``status R`` — a real content-similarity rename, most likely a
  legitimate refactor that moved equivalent coverage elsewhere).

New tests and brand-new test files (git status ``A``) are never penalized
— only files present at BOTH refs (or a detected rename's old/new pair)
are compared. A genuine test-file rename-with-equivalent-coverage is never
treated as a deletion: when ``git`` (with ``--find-renames``) judges the
old and new blobs similar enough, it reports a single ``R`` row, not a
``D``+``A`` pair, so the deletion branch above is never reached for it.

Fail-open throughout: any git failure, timeout, or syntax error degrades
straight to an unblocked, empty-violations verdict — a detector bug must
never block an otherwise-clean cycle. It can only ever ADD a rollback
reason on a genuinely observed, structurally-detected weakening.
"""
from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

_TEST_FILE_RE = re.compile(r'(^|/)(test_[^/]+\.py|[^/]+_test\.py)$')
_SKIP_MARKS = frozenset({'skip', 'skipif', 'xfail'})

_EMPTY_VERDICT: dict = {'blocked': False, 'hard_violations': [], 'soft_signals': []}


def _is_test_path(path: str) -> bool:
    p = path.replace('\\', '/')
    return p.startswith('tests/') or bool(_TEST_FILE_RE.search(p))


def _git(repo_path: Path, *args: str, timeout: int = 30) -> 'subprocess.CompletedProcess | None':
    try:
        return subprocess.run(
            ['git', '-c', f'safe.directory={repo_path}', '-C', str(repo_path), *args],
            capture_output=True, text=True, timeout=timeout,
        )
    except Exception:
        return None


def _blob_at(repo_path: Path, ref: str, path: str) -> 'str | None':
    result = _git(repo_path, 'show', f'{ref}:{path}')
    if result is None or result.returncode != 0:
        return None
    return result.stdout


def _diff_status(repo_path: Path, base_ref: str, head_ref: str) -> 'list[tuple[str, str, str]]':
    """Return ``(status_letter, old_path, new_path)`` rows for ``base_ref..head_ref``.

    ``--find-renames`` makes a genuine, content-similar rename come back as
    a single ``R<score>\told\tnew`` row rather than a ``D``+``A`` pair —
    the caller relies on this to tell a real deletion apart from
    equivalent-coverage-moved-elsewhere.
    """
    result = _git(repo_path, 'diff', '--find-renames', '--name-status', base_ref, head_ref)
    if result is None or result.returncode != 0:
        return []
    rows: 'list[tuple[str, str, str]]' = []
    for line in result.stdout.splitlines():
        parts = line.split('\t')
        if len(parts) < 2:
            continue
        status = parts[0][:1]
        if status in ('R', 'C') and len(parts) >= 3:
            rows.append((status, parts[1], parts[2]))
        elif status not in ('R', 'C'):
            rows.append((status, parts[1], parts[1]))
    return rows


class _FuncStats:
    __slots__ = ('asserts', 'raises', 'skip_marks')

    def __init__(self) -> None:
        self.asserts = 0
        self.raises = 0
        self.skip_marks: 'set[str]' = set()


def _decorator_mark_name(node: ast.expr) -> 'str | None':
    """Return the mark name if *node* is a bare or called ``pytest.mark.<name>`` decorator."""
    target = node.func if isinstance(node, ast.Call) else node
    if isinstance(target, ast.Attribute) and target.attr in _SKIP_MARKS:
        return target.attr
    return None


def _is_raises_call(node: ast.Call) -> bool:
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr == 'raises':
        return True
    if isinstance(func, ast.Name) and func.id == 'raises':
        return True
    return False


def _parametrize_len(node: ast.expr) -> 'int | None':
    """Statically-countable arg-list length of a ``parametrize(...)`` decorator call, else ``None``."""
    if not isinstance(node, ast.Call):
        return None
    target = node.func
    if not (isinstance(target, ast.Attribute) and target.attr == 'parametrize'):
        return None
    if len(node.args) < 2:
        return None
    values = node.args[1]
    if isinstance(values, (ast.List, ast.Tuple)):
        return len(values.elts)
    return None


def _collect_test_functions(source: str) -> 'dict[str, _FuncStats] | None':
    """Parse *source* and return per-test-function structural stats, or ``None`` on a syntax error."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None
    stats: 'dict[str, _FuncStats]' = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith('test_'):
            continue
        fs = _FuncStats()
        for dec in node.decorator_list:
            mark = _decorator_mark_name(dec)
            if mark:
                fs.skip_marks.add(mark)
        for inner in ast.walk(node):
            if isinstance(inner, ast.Assert):
                fs.asserts += 1
            elif isinstance(inner, ast.Call) and _is_raises_call(inner):
                fs.raises += 1
        stats[node.name] = fs
    return stats


def _file_parametrize_total(source: str) -> 'int | None':
    """Sum of statically-countable ``parametrize`` arg-list lengths across the whole file.

    Returns ``None`` (unknown, never compared) when the file has zero
    statically-countable ``parametrize`` decorators — a file may use only
    dynamically-built parametrize values, which this can never see, so
    "unknown" must never be conflated with "zero" (which would produce a
    false shrink report on every such file).
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None
    total = 0
    seen = False
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for dec in getattr(node, 'decorator_list', []):
            length = _parametrize_len(dec)
            if length is not None:
                seen = True
                total += length
    return total if seen else None


def evaluate(repo_path: 'str | Path', base_ref: str, head_ref: str) -> dict:
    """Deterministically evaluate ``base_ref..head_ref`` for test-weakening signals.

    Returns ``{"blocked": bool, "hard_violations": [str, ...], "soft_signals": [str, ...]}``.
    Fail-open: any git/parse trouble yields the unblocked, empty-violations
    result (never a false positive from a detector bug).
    """
    repo_path = Path(repo_path)
    try:
        rows = _diff_status(repo_path, base_ref, head_ref)
        if not rows:
            return dict(_EMPTY_VERDICT, hard_violations=[], soft_signals=[])

        any_non_test_change = any(not _is_test_path(new) for _status, _old, new in rows)

        hard: 'list[str]' = []
        soft: 'list[str]' = []

        for status, old_path, new_path in rows:
            if status == 'D':
                if _is_test_path(old_path) and any_non_test_change:
                    hard.append(
                        f'existing test file deleted alongside non-test changes: {old_path}'
                    )
                continue
            if status == 'A':
                continue  # brand-new files are never penalized
            if not _is_test_path(new_path):
                continue

            # M, R, or C with a test path at head_ref: compare old vs new content.
            old_source = _blob_at(repo_path, base_ref, old_path)
            new_source = _blob_at(repo_path, head_ref, new_path)
            if old_source is None or new_source is None:
                continue
            old_stats = _collect_test_functions(old_source)
            new_stats = _collect_test_functions(new_source)
            if old_stats is None or new_stats is None:
                continue
            is_renamed = status in ('R', 'C')

            old_total = sum(s.asserts + s.raises for s in old_stats.values())
            new_total = sum(s.asserts + s.raises for s in new_stats.values())
            if new_total < old_total:
                msg = (
                    f'net loss of assert/pytest.raises in {new_path}: '
                    f'{old_total} -> {new_total}'
                )
                if is_renamed:
                    soft.append(msg + ' (file also renamed — recorded, not blocked)')
                else:
                    hard.append(msg)

            for name, old_fs in old_stats.items():
                new_fs = new_stats.get(name)
                if new_fs is None:
                    continue  # function removed entirely — covered by the total check above
                added_marks = new_fs.skip_marks - old_fs.skip_marks
                if added_marks:
                    hard.append(
                        f'skip/xfail marker added to previously-unmarked test '
                        f'{new_path}::{name}: {sorted(added_marks)}'
                    )

            old_param_total = _file_parametrize_total(old_source)
            new_param_total = _file_parametrize_total(new_source)
            if (
                old_param_total is not None
                and new_param_total is not None
                and new_param_total < old_param_total
            ):
                soft.append(
                    f'parametrize argument list shrank in {new_path}: '
                    f'{old_param_total} -> {new_param_total}'
                )

        return {'blocked': bool(hard), 'hard_violations': hard, 'soft_signals': soft}
    except Exception:
        return dict(_EMPTY_VERDICT, hard_violations=[], soft_signals=[])
