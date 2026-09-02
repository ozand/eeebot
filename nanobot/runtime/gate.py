"""Bounded mutation surface and smoke gate helpers for eeebot cycles.

Extracted from nanobot.runtime.bridge (#943). The bridge keeps thin
compatibility wrappers; gate policy and test execution live here.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from nanobot.runtime.promoted_overlay import effective_runtime_slice


def _git_cmd(repo_root: 'Path') -> list[str]:
    """Build the common ``git -C <repo>`` argv prefix used by the cycle-branch helpers."""
    return ['git', '-c', f'safe.directory={repo_root}', '-C', str(repo_root)]


# #947 (fix-pass): structural filename policy. Word-shaped names are
# evaluated on the final basename stem; trailing ``s`` is singularized before
# comparison so tokens/api_tokens/my_tokens remain blocked. The one genuine
# exception is a named operator allowlist, not an inline function special-case.
_BLOCKED_FILE_PATTERNS = (
    '.env', '.git', '.npmrc', 'package-lock', 'yarn.lock', 'id_rsa', 'private_key',
)
_BLOCKED_WORD_PATTERNS = frozenset({'secret', 'credential', 'token'})
_SENSITIVE_WORDS = _BLOCKED_WORD_PATTERNS
_ALLOWED_SENSITIVE_BASENAMES = frozenset({
    'token_report.py', 'summarize_token_costs.py', 'token_budget_check.py',
    'analyze_token_usage.py', 'check_token_budget.py', 'validate_no_secrets.py',
    'count_tokens.py',
})

# #944: explicit block list for files that must never be mutated by the
# instance regardless of path-prefix rules. goals.md is the immutable
# operator charter — it ships read-only in the release tree and must not
# appear on ANY mutation surface.
_BLOCKED_EXACT_PATHS = frozenset({'goals.md', 'IDENTITY.md'})


def _is_blocked_filename(
    f: str,
    *,
    blocked_file_patterns: tuple[str, ...] = _BLOCKED_FILE_PATTERNS,
    sensitive_words: frozenset[str] = _SENSITIVE_WORDS,
    allowed_sensitive_basenames: frozenset[str] = _ALLOWED_SENSITIVE_BASENAMES,
) -> bool:
    """Return True if *f* matches any blocked-file pattern.

    Two-tier check (#947 fix-pass):

    1. Structural hard-blocks: ``.env``, ``.git``, ``.npmrc``,
       ``package-lock``, ``yarn.lock``, ``id_rsa``, ``private_key`` —
       matched by basename or stem rules against the full lowercased path.

    2. Sensitive-word rule: split the basename stem on ``._-``; singularize
       a trailing ``s`` when the result is in ``_SENSITIVE_WORDS``; block
       when the last segment is a sensitive word, UNLESS immediately preceded
       by ``no`` (e.g. ``validate_no_secrets.py`` is allowed).

    ``_ALLOWED_SENSITIVE_BASENAMES`` holds explicit exceptions whose basename
    ends in a sensitive word yet are definitively innocent tooling.
    """
    import re as _re_blk
    lower = f.lower().replace('\\', '/')
    basename = lower.rsplit('/', 1)[-1]
    stem = basename.rsplit('.', 1)[0]

    # Named exception: counting/reporting utilities.
    if basename in allowed_sensitive_basenames:
        return False

    # Structural hard-blocks (path-level and exact basename families).
    structural_blocked = (
        '.git' in lower.split('/')
        or basename == '.env' or basename.startswith('.env.')
        or basename == '.npmrc' or basename.startswith('.npmrc.')
        or basename == 'package-lock.json' or basename.startswith('package-lock.')
        or basename == 'yarn.lock' or basename.startswith('yarn.lock.')
        or stem == 'id_rsa' or stem.startswith('id_rsa_')
        or 'private_key' in stem or 'secret_key' in stem
    )
    if structural_blocked:
        return True

    # Sensitive-word rule: final segment, singular-normalised.
    segments = [part for part in _re_blk.split(r'[._-]', stem) if part]
    if not segments:
        return False
    last = segments[-1]
    if last.endswith('s') and last[:-1] in sensitive_words:
        last = last[:-1]
    if last in sensitive_words:
        return True

    return False


# Allowed path prefixes for changed files (relative to repo root).
# 'skills/' — workspace/instance skill directories (SKILL.md + bundled resources).
# The repo-root AGENTS.md is operator-owned and is not a mutable loop surface.
_ALLOWED_PATH_PREFIXES = ('surfaces/', 'scripts/', 'memory/', 'lessons/', 'docs/', 'tests/', 'skills/')
_ALLOWED_EXACT_PATHS = frozenset()

# #863: the gate can only exercise/see-through these file types. Prefix
# rules bound WHERE the instance may write; this bounds WHAT KIND of file
# can auto-integrate. Unknown/compiled-language extensions (.rs/.c/.so/...)
# would integrate unexercised — fail closed instead.
# NB deliberately NOT listed: .env / .gitignore / .gitattributes — the
# _BLOCKED_FILE_PATTERNS substring check (".env", ".git") hard-blocks those
# paths BEFORE this policy runs; listing them here would falsely imply they
# can auto-integrate. Operator adds an extension by product PR editing this
# list — no env-flag relaxation path exists by design.
_GATE_EXT_ALLOWLIST = frozenset((
    ".py", ".md", ".json", ".yaml", ".yml", ".toml", ".txt",
    ".sh", ".service", ".timer", ".conf", ".cron", ".html", ".css",
    ".ts", ".js", ".example",
))
_GATE_BASENAME_ALLOWLIST = frozenset(("Makefile", "Dockerfile"))


def _validate_mutation_surfaces(
    changed_files: 'list[str]',
    *,
    is_blocked_filename: Any = _is_blocked_filename,
    blocked_exact_paths: frozenset[str] = _BLOCKED_EXACT_PATHS,
    allowed_exact_paths: frozenset[str] = _ALLOWED_EXACT_PATHS,
    allowed_path_prefixes: tuple[str, ...] = _ALLOWED_PATH_PREFIXES,
) -> 'list[str]':
    """Validate that changed files respect the bounded mutation surface contract.

    Returns a list of VIOLATIONS (empty list = clean).
    #678 F1/F3: violations are a HARD BLOCK on integration (see main()'s gate
    decision) — previously they were only printed while integration was decided
    solely by the smoke-test gate, so a cycle touching core nanobot/, CI config,
    or bridge.py itself could integrate as long as pytest happened to pass.

    #944: ``goals.md`` (the immutable operator charter) is explicitly rejected
    via ``_BLOCKED_EXACT_PATHS`` before the prefix check runs, so it is denied
    regardless of which directory it appears to be in.

    Inspired by Darwin Mode safety.ts (ruvnet/agent-harness-generator):
    BLOCKED_FILENAME_PATTERNS, APPROVED_FILES, inspectVariant().
    """
    violations: list[str] = []
    for f in changed_files:
        lower = f.lower()
        # #944: explicitly blocked paths (immutable files that must never be
        # mutated, independent of prefix rules).
        fname = f.rsplit('/', 1)[-1] if '/' in f else f
        if fname in blocked_exact_paths or f in blocked_exact_paths:
            violations.append(f'immutable file blocked from mutation: {f}')
            continue
        if f == 'AGENTS.md':
            violations.append(f'operator_owned_path: {f}')
            continue
        # Allowed exact paths bypass the prefix check.
        if f in allowed_exact_paths:
            continue
        # Blocked filename patterns
        if is_blocked_filename(f):
            violations.append(f'blocked filename pattern in: {f}')
        else:
            # Must be in an allowed path prefix
            if not any(f.startswith(prefix) for prefix in allowed_path_prefixes):
                violations.append(
                    f'file outside allowed paths {allowed_path_prefixes}: {f}'
                )
    return violations


# ── #812: runtime-slice tier ─────────────────────────────────────────────────
# The loop's PRIMARY goal (Vector 1) is to optimize its own runtime, but the
# script-only surface above structurally forbids touching nanobot/. #812 adds a
# SECOND tier: an operator-approved slice of runtime modules the loop may PROPOSE
# changes to. Runtime-slice changes never auto-integrate — a green cycle lands as
# a pending promotion candidate for operator review (see the gate decision).
#
# _RUNTIME_SLICE_ENV: comma-separated, repo-relative nanobot/runtime/*.py paths.
# Empty/unset → feature OFF (runtime stays immutable to the loop, exactly as
# before #812). Operator-owned env, mirroring the SELFEVO_DECAY_PROTECT precedent
# (usage_evidence.py) — the instance cannot set it (nanobot/ is off-surface).
_RUNTIME_SLICE_ENV = 'SELFEVO_RUNTIME_SLICE'

# #875: the deny-set + slice-parsing logic moved to the stdlib-only
# nanobot.runtime.runtime_deny module UNCHANGED, so the root promotion
# verifier (host/eeepc/libexec/eeepc_promotion_verifier.py) and the
# agent-side promoted_overlay loader can share the EXACT same
# safety-shell/slice-membership logic the gate uses below, rather than each
# maintaining its own copy. Re-exported here under the same names — existing
# tests (tests/test_runtime_slice.py) reference bridge._is_runtime_deny /
# bridge._runtime_slice_paths() directly and keep working unchanged.
from nanobot.runtime.runtime_deny import _RUNTIME_DENY_ALWAYS_FILES  # noqa: E402
from nanobot.runtime.runtime_deny import _RUNTIME_DENY_TOKENS  # noqa: E402
from nanobot.runtime.runtime_deny import _is_runtime_deny  # noqa: E402


def _runtime_slice_paths(
    *,
    runtime_slice_env: str = _RUNTIME_SLICE_ENV,
    slice_overlay: Any = effective_runtime_slice,
) -> 'set[str]':
    """Operator-approved + trust-ladder-earned runtime-slice paths (#812, #876).

    Thin env-reading wrapper around
    :func:`nanobot.runtime.promoted_overlay.effective_runtime_slice` — the
    operator's ``SELFEVO_RUNTIME_SLICE`` allow-list UNION whichever
    trust-ladder rungs the loop has earned via root-verified promotions
    (#876). Kept as a zero-arg function so existing callers/tests
    (``bridge._runtime_slice_paths()``, ``monkeypatch.setenv``) are
    unaffected. Byte-identical to the pre-#876 env-only result whenever no
    ladder rung is active (including when the env slice itself is unset)
    — see that function's docstring for the full fail-open contract.
    """
    return slice_overlay(os.environ.get(runtime_slice_env))


def _classify_mutation_surface(
    changed_files: 'list[str]',
    *,
    runtime_slice_paths: Any = _runtime_slice_paths,
    is_blocked_filename: Any = _is_blocked_filename,
    is_runtime_deny: Any = _is_runtime_deny,
    blocked_exact_paths: frozenset[str] = _BLOCKED_EXACT_PATHS,
    allowed_exact_paths: frozenset[str] = _ALLOWED_EXACT_PATHS,
    allowed_path_prefixes: tuple[str, ...] = _ALLOWED_PATH_PREFIXES,
    gate_basename_allowlist: frozenset[str] = _GATE_BASENAME_ALLOWLIST,
    gate_ext_allowlist: frozenset[str] = _GATE_EXT_ALLOWLIST,
) -> 'tuple[list[str], list[str], str]':
    """Classify a cycle's changed files into (blocked, violations, tier). #812.

    Extends the bounded-surface contract with a second tier without changing
    :func:`_validate_mutation_surfaces` (kept intact for its tests):

    - ``blocked``   : blocked filename-pattern hits (#678 F3) — hard block.
    - ``violations``: surface violations — a deny-set hit, a file in neither
      the script surface nor the operator-approved runtime slice, or a file
      in an allowed prefix whose extension is not gate-exercisable (#863,
      ``_GATE_EXT_ALLOWLIST`` / ``_GATE_BASENAME_ALLOWLIST``) — hard block.
    - ``tier``      : ``'script'`` when every non-blocked file is in the existing
      script surface (auto-integrate on green — unchanged behavior); ``'runtime'``
      when at least one file is an operator-approved runtime-slice module (green
      lands as a promotion candidate, never auto-integrated).

    Fail-closed: a deny-set path is always a violation, even when it is also
    listed in the allow-slice env; a mixed diff carrying any violation is blocked
    as a whole regardless of tier (the gate checks ``violations`` before it ever
    consults ``tier``).
    """
    slice_paths = runtime_slice_paths()
    blocked: 'list[str]' = []
    violations: 'list[str]' = []
    tier = 'script'
    for f in changed_files:
        lower = f.lower()
        # #944: explicitly blocked exact paths (immutable files).
        fname = f.rsplit('/', 1)[-1] if '/' in f else f
        if fname in blocked_exact_paths or f in blocked_exact_paths:
            blocked.append(f'immutable file blocked from mutation: {f}')
            continue
        if f == 'AGENTS.md':
            violations.append(f'operator_owned_path: {f}')
            continue
        # Allowed exact paths bypass the prefix and pattern checks.
        if f in allowed_exact_paths:
            basename2 = Path(f).name
            suffix2 = Path(f).suffix.lower()
            if basename2 not in gate_basename_allowlist and suffix2 not in gate_ext_allowlist:
                violations.append(
                    f'file extension not gate-exercisable (auto-integration denied): {f}'
                )
            continue
        if is_blocked_filename(f):
            blocked.append(f'blocked filename pattern in: {f}')
            continue
        if is_runtime_deny(f):
            violations.append(f'runtime deny-set path (immutable safety shell): {f}')
            continue
        if any(f.startswith(prefix) for prefix in allowed_path_prefixes):
            # #863: prefix rules bound WHERE; this bounds WHAT KIND of file can
            # auto-integrate. A file in an allowed prefix but of an unknown/
            # non-exercisable extension (e.g. scripts/foo.rs, scripts/blob.so)
            # would otherwise pass the smoke gate (py_compile + pytest only
            # touch .py) and integrate with zero content verification.
            basename = Path(f).name
            suffix = Path(f).suffix.lower()
            if basename not in gate_basename_allowlist and suffix not in gate_ext_allowlist:
                violations.append(
                    f'file extension not gate-exercisable (auto-integration denied): {f}'
                )
            continue
        if f.replace('\\', '/') in slice_paths:
            tier = 'runtime'
            continue
        violations.append(
            f'file outside allowed paths {allowed_path_prefixes} and not in runtime slice: {f}'        )
    return blocked, violations, tier




_SMOKE_ENV_STRIP_PREFIXES = (
    'STATE_DIR',
    'NANOBOT_',
    'SUBAGENT_',
    'EEEBOT_',
    'TARGET_WORKSPACE',
    'LITELLM_',
    'GOAL_',
    'SOURCE_',
    'SELFEVO_',
)


# #686: small, fixed, hermetic set of cross-cutting tests always run by the
# bounded gate, regardless of which files a cycle touched. Keeps catching
# breakage that isn't localized to a single changed file (e.g. an import-path
# regression) without paying for the full ~600s suite every cycle. Chosen for
# speed + criticality: import hygiene (nanobot.* import-only enforcement) and
# the config schema/path tests are all sub-second, dependency-free unit tests.
_CORE_SMOKE_TESTS = (
    'tests/test_import_hygiene.py',
    'tests/test_config_schema.py',
    'tests/test_config_paths.py',
)


def _select_gate_tests(
    repo_root: 'Path', changed_files: 'list[str]',
    *,
    core_smoke_tests: tuple[str, ...] = _CORE_SMOKE_TESTS,
) -> 'tuple[list[str], list[str]]':
    """Map a cycle's changed files to (test_paths, import_targets) for the bounded gate.

    #686: the subagent's mutation surface is bounded (scripts/docs/memory/
    lessons/tests — core ``nanobot/`` is hard-blocked, #678), so a per-cycle
    gate only needs to validate what a cycle can actually change, not the
    whole product suite (that's product CI + re-seed-time verification, see
    docs/specs/subagent-bridge/spec.md R10/R11).

    - ``import_targets``: every changed ``*.py`` file that still exists in the
      working tree (deleted files are skipped — nothing to compile).
    - ``test_paths``: for each changed file, its corresponding test module(s)
      (``scripts/foo.py`` -> ``tests/test_foo.py``; ``nanobot/x/y.py`` ->
      ``tests/test_y.py`` plus any ``tests/test_*y*.py``; a changed
      ``tests/test_*.py`` file -> itself), plus the fixed :data:`_CORE_SMOKE_TESTS`
      set. Only paths that exist in the working tree are returned. Order is
      deterministic (sorted) so gate output/tests are stable.

    Returns ``([], [])`` only when there is nothing to check at all (no
    changed .py files, no matching tests, AND none of the core smoke tests
    exist in this tree) — callers treat that as "nothing to gate on", never
    as an auto-pass.
    """
    import_targets: 'set[str]' = set()
    test_paths: 'set[str]' = set()

    for f in changed_files:
        f = f.strip()
        if not f:
            continue
        if f.endswith('.py') and (repo_root / f).exists():
            import_targets.add(f)

        path = Path(f)
        stem = path.stem
        if not stem:
            continue

        # A changed test file affects itself directly.
        if f.startswith('tests/') and path.name.startswith('test_') and f.endswith('.py'):
            if (repo_root / f).exists():
                test_paths.add(f)
            continue

        # Direct name mapping: <anything>/<stem>.py -> tests/test_<stem>.py
        candidate = f'tests/test_{stem}.py'
        if (repo_root / candidate).exists():
            test_paths.add(candidate)

        # Fuzzy mapping: any test module whose name contains the stem, so a
        # rename or a submodule (nanobot/x/y.py) still finds tests/test_*y*.py.
        tests_dir = repo_root / 'tests'
        if tests_dir.is_dir():
            try:
                for match in tests_dir.glob(f'test_*{stem}*.py'):
                    test_paths.add(str(match.relative_to(repo_root)))
            except (OSError, ValueError):
                pass

    for core in core_smoke_tests:
        if (repo_root / core).exists():
            test_paths.add(core)

    return sorted(test_paths), sorted(import_targets)


def _sanitized_smoke_env(
    *,
    smoke_env_strip_prefixes: tuple[str, ...] = _SMOKE_ENV_STRIP_PREFIXES,
) -> dict:
    """Build a subprocess env for the smoke-test gate with runtime state stripped.

    See #668 (env-pollution finding): the bridge systemd unit's environment
    (STATE_DIR, NANOBOT_CONFIG_PATH, SUBAGENT_BRIDGE_*, TARGET_WORKSPACE,
    LITELLM_*, ...) leaks into the pytest subprocess by default inheritance.
    Tests in the target repo that read process env to locate state (e.g.
    feedback-decision code consulting STATE_DIR) then observe LIVE production
    state instead of a hermetic test fixture, producing spurious gate failures
    that are not reproducible in a clean environment. Deterministically
    reproduced: tests/test_active_lane_continue.py passes in a clean env and
    fails with the bridge env sourced, on identical code.

    Strips every key starting with any prefix in _SMOKE_ENV_STRIP_PREFIXES.
    Deliberately leaves PATH/HOME/LANG/PYTHON* and provider auth vars alone —
    only runtime-state-redirecting keys are removed.
    """
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(smoke_env_strip_prefixes)
    }


def _run_smoke_tests(
    repo_root: 'Path', changed_files: 'list[str] | None' = None, timeout: int = 300,
    *,
    select_gate_tests: Any = _select_gate_tests,
    sanitized_smoke_env: Any = _sanitized_smoke_env,
) -> 'tuple[bool, str]':
    """Run a BOUNDED smoke gate in repo_root after a subagent commit (#686).

    Returns (passed: bool, output: str) where output is truncated to 2000 chars.

    Replaces the previous "run all of tests/" gate with a targeted selection,
    since the subagent's mutation surface is bounded (scripts/docs/memory/
    lessons/tests only — core nanobot/ is hard-blocked, #678): the bulk of the
    full suite (core nanobot/ tests) cannot have been broken by a cycle, so
    re-running it every cycle is pure waste against the 300s gate timeout.
    Full-suite validation of core is product CI + re-seed-time verification
    (docs/specs/subagent-bridge/spec.md R10/R11), not this per-cycle gate.

    Two phases, both fail-safe (never pass-open):
    1. Import-smoke: ``python -m py_compile`` every changed ``*.py`` file. A
       syntax/compile error fails the gate immediately, before pytest even
       runs. (py_compile only — it deliberately does NOT actually import
       arbitrary changed modules, which may have side effects; import-time
       errors are caught by the affected tests in phase 2 instead.)
    2. Targeted pytest: the union of tests affected by the changed files plus
       the fixed :data:`_CORE_SMOKE_TESTS` set (see :func:`_select_gate_tests`),
       run with the same hermetic env as before (#668).

    ``changed_files`` of ``None`` or ``[]`` still runs the core smoke set (never
    an auto-pass — see #678 finding 2/4): a self-evolving repo always has
    tests, so an empty selection when core tests are also missing is FAIL, not
    skip.

    Runs with sys.executable (the runtime's own venv interpreter, with all deps
    installed) rather than the bare system python — see #668: a bare `python3`
    lacks the runtime's dependencies (e.g. ddgs), producing spurious failures.

    Runs with a sanitized subprocess env (see _sanitized_smoke_env / #668
    env-pollution finding): the gate must evaluate the repo hermetically, not
    against live runtime state leaked in via inherited STATE_DIR / NANOBOT_* /
    SUBAGENT_* / TARGET_WORKSPACE / LITELLM_* (and related) environment keys.

    Inspired by Darwin Mode LEARNINGS.md §1:
    'closed-loop repair: run the failing tests, feed the traceback back → 2× improvement'
    """
    import subprocess as _sp
    tests_dir = repo_root / 'tests'
    if not tests_dir.exists():
        # #678 F2: a missing tests/ directory (e.g. a cycle that `rm -rf tests/`)
        # previously turned a failing change green. Fail closed instead.
        return False, 'no tests directory (fail-safe: #678)'

    changed_files = changed_files or []
    test_paths, import_targets = select_gate_tests(repo_root, changed_files)
    # Phase 1: import-smoke via py_compile — catches syntax/compile breakage
    # in every changed .py file before pytest collection even starts.
    if import_targets:
        try:
            compile_result = _sp.run(
                [sys.executable, '-m', 'py_compile', *import_targets],
                capture_output=True, text=True, timeout=timeout, cwd=str(repo_root),
                env=sanitized_smoke_env(),            )
        except _sp.TimeoutExpired:
            return False, 'import-smoke (py_compile) timed out'
        except Exception as exc:
            # #678 F4 parity: a crash in the compile-check subprocess itself is
            # suspicious, not benign — fail closed.
            return False, f'import-smoke harness error (fail-safe: #686): {exc}'
        if compile_result.returncode != 0:
            output = (compile_result.stdout + compile_result.stderr).strip()
            output = output[-2000:] if len(output) > 2000 else output
            return False, f'import-smoke FAIL (py_compile):\n{output}'

    # #678 F2 parity: an empty test selection is NOT an auto-pass. This only
    # happens when there are changed files but neither they nor the fixed
    # core-smoke set map to any test file present in the tree — treat that
    # the same as an emptied suite.
    if not test_paths:
        return False, 'no tests selected for gate (fail-safe: #686/#678)'

    try:
        result = _sp.run(
            [sys.executable, '-m', 'pytest', *test_paths,
             '-q', '--tb=native', '-p', 'no:cacheprovider'],
            capture_output=True, text=True, timeout=timeout, cwd=str(repo_root),
            env=sanitized_smoke_env(),        )
        output = (result.stdout + result.stderr).strip()
        output = output[-2000:] if len(output) > 2000 else output  # keep tail (most relevant)
        if 'no tests ran' in output or 'collected 0 items' in output:
            # #678 F2: an emptied suite previously passed the gate. Fail closed.
            return False, 'no tests collected (fail-safe: #678)'
        passed = result.returncode == 0
        return passed, output
    except _sp.TimeoutExpired:
        return False, 'pytest timed out'
    except FileNotFoundError as exc:
        # #678 F4: pytest is always installed in the runtime venv (sys.executable
        # above); a genuinely missing pytest module is itself suspicious on the
        # host, so fail closed rather than silently skipping the gate.
        return False, f'pytest unavailable (fail-safe: #678): {exc}'
    except Exception as exc:
        # #678 F4: previously `return True` here — a pytest subprocess crash
        # (OOM/OSError/disk-full) integrated untested code. Fail closed.
        return False, f'smoke harness error (fail-safe: #678): {exc}'


def _count_tests(repo_root: 'Path') -> int:
    """Count ``def test_`` occurrences across ``tests/**/*.py`` in the working tree.

    A cheap, hermetic proxy for suite size (#678 F2 suite-shrink guard) — avoids
    a second pytest collection pass just to get a number. Returns 0 if there is
    no tests/ directory or nothing readable; callers treat 0 as "unknown", never
    as a negative signal on its own.
    """
    tests_dir = repo_root / 'tests'
    if not tests_dir.exists():
        return 0
    count = 0
    for f in tests_dir.rglob('*.py'):
        try:
            count += f.read_text(encoding='utf-8', errors='ignore').count('def test_')
        except Exception:
            pass
    return count


def _count_tests_at_ref(
    repo_root: 'Path', ref: str, *, git_cmd: Any = _git_cmd,
) -> int:
    """Count ``def test_`` occurrences across ``tests/**/*.py`` at a git ref, without checkout.

    Reads blobs via ``git show <ref>:<path>`` so it works while the working tree
    is checked out to a different branch (e.g. capturing the pre-cycle baseline
    for origin/main right after ``_setup_cycle_branch`` has already moved the
    checkout to the cycle branch). Returns 0 on any git failure (missing ref, no
    tests/ tree at that ref, ...) — a 0 baseline is treated as "nothing to compare
    against" by :func:`_run_smoke_tests_with_shrink_guard`, never as a violation.
    """
    import subprocess as _sp
    git = git_cmd(repo_root)
    try:
        ls = _sp.run(git + ['ls-tree', '-r', '--name-only', ref, '--', 'tests/'],
                      capture_output=True, text=True)
    except Exception:
        return 0
    if ls.returncode != 0:
        return 0
    count = 0
    for path in ls.stdout.splitlines():
        path = path.strip()
        if not path.endswith('.py'):
            continue
        try:
            show = _sp.run(git + ['show', f'{ref}:{path}'], capture_output=True, text=True)
        except Exception:
            continue
        if show.returncode != 0:
            continue
        count += show.stdout.count('def test_')
    return count


def _test_function_names(repo_root: 'Path') -> 'set[str]':
    """Return the set of ``test_*`` function names defined across ``tests/**/*.py``
    in the working tree (#846 suite-shrink guard hardening).

    A count-only shrink guard can be defeated by swapping N real tests for N
    ``def test_x(): pass`` stubs — the count stays flat and the gate passes.
    Comparing NAMES against a baseline closes that hole: a baseline name that
    disappears from the current set is a real regression even when the count
    matches. Fail-open: returns an empty set on any error or missing tests/
    directory — an empty set is "unknown baseline" to callers, never treated
    as a violation on its own.
    """
    import re as _re_names

    tests_dir = repo_root / 'tests'
    if not tests_dir.exists():
        return set()
    names: 'set[str]' = set()
    try:
        for f in tests_dir.rglob('*.py'):
            try:
                text = f.read_text(encoding='utf-8', errors='ignore')
            except Exception:
                continue
            names.update(_re_names.findall(r'def (test_\w+)', text))
    except Exception:
        return set()
    return names


def _test_function_names_at_ref(
    repo_root: 'Path', ref: str, *, git_cmd: Any = _git_cmd,
) -> 'set[str]':
    """Like :func:`_test_function_names` but reads blobs at a git ``ref`` via
    ``git show``/``ls-tree``, without touching the working tree — mirrors
    :func:`_count_tests_at_ref` (#846). Returns an empty set on any git
    failure (missing ref, no tests/ tree at that ref, ...): "unknown
    baseline" to callers, never a violation.
    """
    import re as _re_names
    import subprocess as _sp
    git = git_cmd(repo_root)
    try:
        ls = _sp.run(git + ['ls-tree', '-r', '--name-only', ref, '--', 'tests/'],
                      capture_output=True, text=True)
    except Exception:
        return set()
    if ls.returncode != 0:
        return set()
    names: 'set[str]' = set()
    for path in ls.stdout.splitlines():
        path = path.strip()
        if not path.endswith('.py'):
            continue
        try:
            show = _sp.run(git + ['show', f'{ref}:{path}'], capture_output=True, text=True)
        except Exception:
            continue
        if show.returncode != 0:
            continue
        names.update(_re_names.findall(r'def (test_\w+)', show.stdout))
    return names


def _run_smoke_tests_with_shrink_guard(
    repo_root: 'Path', baseline_test_count: int,
    changed_files: 'list[str] | None' = None, timeout: int = 300,
    baseline_test_names: 'set[str] | None' = None,
    *,
    smoke_runner: Any = _run_smoke_tests,
    count_tests: Any = _count_tests,
    test_function_names: Any = _test_function_names,
) -> 'tuple[bool, str]':
    """Gate wrapper: fail immediately if the cycle's test count dropped below baseline.

    #678 F2: without this, a repair loop could iteratively delete or weaken tests
    across revisions until the suite happens to pass — closing that path requires
    checking suite size on every gate evaluation (initial AND each repair retry),
    not just once. ``baseline_test_count`` of 0 means "could not establish a
    baseline" and never blocks (nothing to compare against); otherwise a strictly
    lower current count fails the gate without needing to run pytest at all.

    #846: a count-only guard is defeated by swapping N real tests for N
    ``def test_x(): pass`` stubs (count stays flat, guard passes). When
    ``baseline_test_names`` is given, this also requires the baseline's test
    FUNCTION NAMES to be a subset of the current tree's names — a name-level
    superset check that catches a swap the count alone would miss.
    ``baseline_test_names`` of ``None``/empty means "could not establish a
    baseline" and never blocks.

    The shrink guard itself counts/names tests present in the WHOLE tree
    (unchanged by #686) — it is independent of which tests the bounded gate
    below actually executes, so a cycle can't dodge it by only touching
    untested files. ``changed_files`` is forwarded to :func:`_run_smoke_tests`
    for the bounded selection (#686); see there for the import-smoke +
    affected + core design.
    """
    if baseline_test_count > 0:
        current = count_tests(repo_root)
        if current < baseline_test_count:
            return False, (
                f'suite-shrink guard (#678): test count dropped from '
                f'{baseline_test_count} to {current} vs main baseline'
            )
    if baseline_test_names:
        current_names = test_function_names(repo_root)
        missing = baseline_test_names - current_names
        # #846: tolerate legitimate renames / small refactors (a few baseline
        # names vanishing) but block WHOLESALE gutting — swapping most real
        # tests for trivial stubs to force a pass at a flat count. Trip only
        # when MORE THAN HALF the baseline's named tests disappeared. The
        # held-out pack (#780) is the instance-invisible behavioral backstop;
        # this name check is defense-in-depth against count-preserving gutting.
        if len(missing) > len(baseline_test_names) // 2:
            return False, (
                f'suite-shrink guard (#678/#846): {len(missing)}/{len(baseline_test_names)} '
                f'baseline test function(s) removed vs main — wholesale gutting: '
                f'{sorted(missing)[:5]}'
            )
    return smoke_runner(repo_root, changed_files=changed_files, timeout=timeout)
