"""eeepc self-evolving subagent bridge.

Reads the latest queued subagent request from state/subagents/requests/,
builds a concrete task prompt from the source_artifact, and spawns a
bounded subagent via the nanobot SubagentManager.

Treats "blocked/local_executor_unavailable" results as NOT handled —
those are created by the coordinator materializer when no executor is
configured, and should be superseded by a real LLM bridge run.

This is the canonical implementation (moved from
``scripts/eeepc_self_evolving_subagent_bridge.py`` in #599). The systemd
service still execs the thin wrapper at that script path; it just calls
:func:`cli_main` here. The deploy contract (file copy to
``/usr/local/libexec/``) is intentionally unchanged in this PR — see #601 for
the follow-up that retires the file-copy path in favour of a console-script
entrypoint.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

try:  # pragma: no cover - fcntl is POSIX-only; the host is always Linux
    import fcntl
except ImportError:  # pragma: no cover - exercised only on non-POSIX platforms
    fcntl = None  # type: ignore[assignment]

from nanobot.agent.subagent import SubagentManager
from nanobot.bus.queue import MessageBus
from nanobot.cli.commands import _make_provider
from nanobot.config.loader import load_config, set_config_path
from nanobot.observability.llm_telemetry import set_call_context

# #875: install the root-verified runtime-slice overlay BEFORE any
# nanobot.runtime.* module is imported below — a root-promoted module must
# shadow the installed one from the very first import site in this process,
# not just for call sites deeper in the file. install_promoted_overlay() is
# itself fully fail-closed/fail-open internally (see its docstring); this
# call site never wraps it in try/except so an unexpected exception there
# would be a genuine bug worth surfacing, not something to paper over.
from nanobot.runtime.promoted_overlay import install_promoted_overlay

install_promoted_overlay()

from nanobot.runtime import llm_proposer, demand  # noqa: E402
from nanobot.runtime.backlog_snapshot import write_backlog_snapshot  # noqa: E402
from nanobot.runtime.cycle_ledger import (  # noqa: E402
    VALID_OUTCOMES,
    append_event,
    record_cycle_outcome,
    record_cycle_started,
    record_dedup_decision,
    record_gate_decision,
)
from nanobot.runtime.existence_index import (  # noqa: E402
    derive_intent,
    find_duplicate_script,
    intents_match,
)
from nanobot.runtime.goal_review import read_charter_text  # noqa: E402
from nanobot.runtime.goal_text_utils import filter_completed_priorities_from_goal_text  # noqa: E402
from nanobot.runtime.lesson_v2 import (  # noqa: E402
    bounded_load_yaml as _bounded_lesson_load,
)
from nanobot.runtime.lesson_v2 import (
    find_duplicate as _find_lesson_duplicate,
)
from nanobot.runtime.lesson_v2 import (
    normalize_problem as _normalize_lesson_problem,
)
from nanobot.runtime.lesson_v2 import (
    validate_lesson as _validate_lesson_v2,
    validate_lesson_for_mint as _validate_lesson_for_mint,
)
from nanobot.runtime.model_registry import resolve_max_tool_iterations, resolve_model  # noqa: E402
from nanobot.runtime.schemas import CONTROLLED_LESSON_TAGS  # noqa: E402

# #789: the fitness-input sidecar list + hash helper live in scorecard.py
# (the fitness module — #603 placement; the list's contents stay out of this
# instance-facing source). Hashed immediately before the subagent spawn and
# re-hashed right before the gate verdict — any mismatch means code executed
# INSIDE the spawn window wrote a fitness input, which only the harness may
# do (live reward-hack 2026-07-17). The bridge's own legitimate writes
# (demand fold/exhaustion updates via collect_demand, scorecard recompute —
# all on the proposer path) happen OUTSIDE this window, so a clean cycle
# never records an incident (pinned in tests/test_bridge_fitness_integrity.py).
from nanobot.runtime.scorecard import (  # noqa: E402
    FITNESS_SIDECARS as _FITNESS_SIDECARS,
)
from nanobot.runtime.scorecard import (  # noqa: E402
    fitness_sidecar_hashes as _fitness_sidecar_hashes,
)
from nanobot.runtime.stop_guards import REVISION_CAP_DEFAULT, revision_outcome  # noqa: E402
# #1119: deterministic test-weakening detector — runs BEFORE the smoke gate
# decides a cycle's fate, same placement discipline as
# _validate_mutation_surfaces (#678 F1). Deny-set protected (runtime_deny.py).
from nanobot.runtime.test_guard import evaluate as _evaluate_test_weakening  # noqa: E402

STATE_DIR = Path(os.environ.get('STATE_DIR', '/var/lib/eeepc-agent/self-evolving-agent/state'))
TARGET_WORKSPACE = Path(os.environ.get('TARGET_WORKSPACE', '/opt/eeepc-agent/runtimes/self-evolving-agent/current'))
# #966: RELEASE_ROOT is the read-only release tree (goals.md, IDENTITY.md,
# goal_text.json). Defaults to the canonical release path so a fresh install
# without any env override reads from the correct place. TARGET_WORKSPACE
# keeps only its writable-workspace role (.nanobot/subagents, latest.json).
RELEASE_ROOT = Path(os.environ.get('RELEASE_ROOT', '/opt/eeepc-agent/runtimes/self-evolving-agent/current'))
CONFIG_PATH = Path(os.environ.get('NANOBOT_CONFIG_PATH', '/run/user/1001/nanobot-eeepc/config.json'))
BRIDGE_STATE_DIR = Path(os.environ.get('SUBAGENT_BRIDGE_STATE_DIR', str(STATE_DIR / 'subagent_bridge')))

# #1280: a cycle whose executor LLM call never returned is a failed cycle, and
# the process says so with its exit status — the __main__ guard turns any
# non-zero code into a `failure` row in bridge/exit_streak.json (#1197), so a
# sustained model outage moves consecutive_failures instead of reading as a
# run of successes. Distinct from 1 (internal error) so the journal and the
# streak's last_exit_status name the class.
EXIT_EXECUTOR_LLM_ERROR = 3
# How many times a request whose subagent died on the LLM call is re-offered
# before it is retired with the handled_ marker like any other request. Bounded
# so a permanently-bad request (bad model name, oversize prompt) cannot spin
# through every cycle forever; 3 covers an outage of two or three cycles and
# leaves the rest to the next proposal.
LLM_ERROR_MAX_RETRIES = int(os.environ.get('SUBAGENT_BRIDGE_LLM_ERROR_MAX_RETRIES', '3'))
BRIDGE_ENABLED =os.environ.get('SUBAGENT_BRIDGE_ENABLED', '1').strip().lower() in {'1', 'true', 'yes', 'on'}
FORCE_PROFILE = os.environ.get('SUBAGENT_BRIDGE_FORCE_PROFILE', '').strip()
FORCE_BUDGET = os.environ.get('SUBAGENT_BRIDGE_FORCE_BUDGET', '').strip()
BRIDGE_MODEL = resolve_model('executor')
try:
    # #716: bounded window (hours) for _recent_failure_match() to suppress
    # re-proposing a recently-failed/rejected task. Bounded so a legitimately
    # retryable task is never blocked forever — only silences short-term repeats.
    FAILURE_SUPPRESS_HOURS = float(os.environ.get('SUBAGENT_BRIDGE_FAILURE_SUPPRESS_HOURS', '24').strip() or '24')
except ValueError:
    FAILURE_SUPPRESS_HOURS = 24.0

# #1176: how many newest artifacts (live results/ + rotated archive/) are
# considered before the failure filter runs. state_access.artifacts caps its own
# path scan at 256 and the host writes roughly 144 results a day, so this covers
# the 24h window with room to spare. It bounds CANDIDATES, not how far back the
# window reaches — the age cutoff is FAILURE_SUPPRESS_HOURS and stays separate.
_FAILURE_SCAN_CANDIDATES = 256

try:
    # #733: bounded cap on how many pre-spawn duplicate requests _main_impl
    # will bulk-skip (each a cheap, zero-LLM git check) in a single bridge
    # run before returning — a stale queue must never turn one timer
    # invocation into an unbounded loop on the weak host.
    MAX_SKIPS_PER_RUN = int(os.environ.get('SUBAGENT_BRIDGE_MAX_SKIPS_PER_RUN', '10').strip() or '10')
    if MAX_SKIPS_PER_RUN < 1:
        MAX_SKIPS_PER_RUN = 10
except ValueError:
    MAX_SKIPS_PER_RUN = 10

# #721: bounded cap on how many local pre-cycle-*/cycle-* tags _prune_cycle_tags
# inspects per bridge run — a pathologically large tag namespace (e.g. a stuck
# retention env) must never turn pruning into an unbounded git operation.
_PRUNE_TAG_CAP = 500


def load_json(path: Path):
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def approval_open() -> bool:
    gate = load_json(STATE_DIR / 'approvals' / 'apply.ok') or {}
    return int(gate.get('expires_at_epoch', 0) or 0) > int(time.time())


def _is_real_result(result: dict) -> bool:
    """Return True only if this result represents actual LLM execution, not a blocked stub."""
    status = str(result.get('result_status') or result.get('status') or '').lower()
    terminal = str(result.get('terminal_reason') or '').lower()
    materialized_from = str(result.get('materialized_from') or '').lower()
    blocker = result.get('blocker') or {}
    blocker_reason = str(blocker.get('reason') or '') if isinstance(blocker, dict) else ''

    # Blocked stubs from queued_request_terminalizer or unconfigured executor
    if status == 'blocked':
        return False
    if terminal == 'local_executor_unavailable':
        return False
    if materialized_from == 'queued_request_terminalizer':
        return False
    if blocker_reason == 'local_executor_unavailable':
        return False
    return True


# #1040: Module-level cache of parsed result entries keyed by results_dir_str
# to avoid repeated scandir passes across bridge invocation stages.
_RESULT_ENTRIES_CACHE: dict[str, list[tuple[Path, dict, float]]] = {}


def _clear_result_entries_cache() -> None:
    """Clear the module-level result entries cache (called at start of bridge runs)."""
    _RESULT_ENTRIES_CACHE.clear()


def _iter_result_entries(results_dir: 'Path',
                         entries: 'list[tuple[Path, dict, float]] | None' = None,
                         use_cache: bool = True) -> list[tuple[Path, dict, float]]:
    """Scan results_dir in a single os.scandir pass and yield (path, data, mtime).

    #1040: Replaces multiple separate glob/scandir sweeps across results consumers.
    Supports cached entries or module-level cache to avoid multi-pass scandir.
    """
    if entries is not None:
        return entries
    if not results_dir or not results_dir.exists():
        return []

    r_key = str(results_dir)

    if use_cache and r_key in _RESULT_ENTRIES_CACHE:
        return _RESULT_ENTRIES_CACHE[r_key]

    records: list[tuple[Path, dict, float]] = []
    try:
        with os.scandir(str(results_dir)) as it:
            for entry in it:
                if not entry.name.endswith('.json'):
                    continue
                try:
                    if not entry.is_file():
                        continue
                    mtime = entry.stat().st_mtime
                    p = Path(entry.path)
                    data = json.loads(p.read_text(encoding='utf-8'))
                    if isinstance(data, dict):
                        records.append((p, data, mtime))
                except Exception:
                    continue
    except Exception:
        return []

    if use_cache:
        _RESULT_ENTRIES_CACHE[r_key] = records
    return records


def _iter_archive_entries(archive_dir: 'Path', limit: int) -> list[tuple['Path', dict, float]]:
    """Newest `limit` archived result payloads as (path, data, mtime) (#1176).

    Results migrate out of `results/` within the hour while the failure window
    is 24h, so the live directory alone held 1 of the 9 failures inside its own
    window when measured on the host (2026-09-02). This reads the archive to
    close that gap.

    Deliberately NOT merged into `_iter_result_entries`: that function's cache
    is keyed on `results_dir` and is reused by every other consumer, and #1040's
    single-scandir invariant counts passes over `results_dir` specifically. This
    is a separate pass over a separate directory, so the invariant is unaffected.

    Ordering before bounding: `stat()` every candidate, sort newest-first, then
    take `limit` and only then parse. Bounding an unsorted listing would pick an
    arbitrary `limit` of the archive's thousands of files.
    """
    records: list[tuple[Path, dict, float]] = []
    try:
        if not archive_dir or not archive_dir.is_dir():
            return []
        stamped: list[tuple[float, Path]] = []
        with os.scandir(str(archive_dir)) as it:
            for entry in it:
                if not entry.name.endswith('.json'):
                    continue
                try:
                    if entry.is_file():
                        stamped.append((entry.stat().st_mtime, Path(entry.path)))
                except Exception:
                    continue
        stamped.sort(key=lambda x: x[0], reverse=True)
        for mtime, path in stamped[:max(0, limit)]:
            try:
                data = json.loads(path.read_text(encoding='utf-8'))
            except Exception:
                continue
            if isinstance(data, dict):
                records.append((path, data, mtime))
    except Exception:
        return []
    return records


def find_pending_request() -> tuple[Path | None, dict]:
    """Find the oldest queued subagent request not yet handled by a real executor."""
    req_dir = STATE_DIR / 'subagents' / 'requests'
    if not req_dir.exists():
        return None, {}

    # Collect request_ids/paths that have REAL results (not blocked stubs)
    real_handled: set[str] = set()
    result_dir = STATE_DIR / 'subagents' / 'results'
    for _rp, rd, _mtime in _iter_result_entries(result_dir):
        if not _is_real_result(rd):
            continue  # skip blocked stubs — still eligible for bridge
        if rid := rd.get('request_id') or rd.get('verification_task_id'):
            real_handled.add(rid)
        if rpath := rd.get('request_path'):
            real_handled.add(str(rpath))

    # Also check bridge's own handled markers (those ARE real LLM runs)
    if BRIDGE_STATE_DIR.exists():
        for m in BRIDGE_STATE_DIR.glob('handled_*.txt'):
            content = m.read_text(encoding='utf-8').strip()
            real_handled.add(content)
            # marker name encodes request_id
            stem = m.stem[len('handled_'):]
            real_handled.add(stem)

    candidates = sorted(
        [p for p in req_dir.glob('*.json') if p.is_file()],
        key=lambda p: p.stat().st_mtime,
    )
    for path in candidates:
        req = load_json(path) or {}
        status = str(req.get('request_status') or req.get('status') or 'queued').lower()
        if status not in ('queued', 'pending'):
            continue
        rid = req.get('request_id') or req.get('verification_task_id') or str(path)
        # Bridge handled markers are filed under the SANITIZED id (see
        # `safe_id = request_id.replace('/', '_')[:120]` below); compare that
        # form too, or a raw id containing sanitized characters (e.g. '/')
        # slips this filter and gets returned forever (#733 wedge).
        safe_rid = rid.replace('/', '_')[:120]
        if rid in real_handled or safe_rid in real_handled or str(path) in real_handled:
            continue
        return path, req
    return None, {}



def _get_previous_attempts(
    state_dir: 'Path',
    backlog_title: str,
    cycle_id: str,
    max_attempts: int = 3,
    entries: 'list[tuple[Path, dict, float]] | None' = None,
) -> list[dict]:
    """Return last N bridge result entries for the same backlog_title or cycle.

    Matching priority (first hit wins):
    1. Primary: source_artifact → next_bounded_candidate.title keyword match
       (≥3 word matches, same logic as _task_already_done). Most reliable because
       the title comes from the artifact, not from a generic summary string.
    2. Fallback: summary keyword match (when artifact file missing/unreadable).
    3. Tertiary: exact cycle_id match.

    Used by build_task() to inject a '## Previous attempts' section into the
    subagent prompt so it knows what the prior session did (and why it failed).
    """
    results_dir = state_dir / 'subagents' / 'results'
    import json as _json
    import re as _re2
    candidates: list[tuple[float, dict]] = []
    title_words = [w.lower() for w in _re2.findall(r'[A-Za-z]{4,}', backlog_title)] if backlog_title else []

    for _p, data, mtime in _iter_result_entries(results_dir, entries=entries):
        if data.get('materialized_from') != 'bridge_llm_execution':
            continue

        is_match = False

        # 1. Primary: read source_artifact → nbc.title and match keywords
        if title_words and not is_match:
            _src = data.get('source_artifact', '')
            if _src:
                try:
                    _art = _json.loads(Path(_src).read_text(encoding='utf-8'))
                    _nbc_title = (_art.get('next_bounded_candidate') or {}).get('title', '')
                    if _nbc_title:
                        _nbc_words = [w.lower() for w in _re2.findall(r'[A-Za-z]{4,}', _nbc_title)]
                        _matches = sum(1 for w in title_words if w in _nbc_words)
                        if _matches >= min(3, len(title_words)):
                            is_match = True
                except Exception:
                    pass  # artifact missing or unreadable — fall through to summary

        # 2. Fallback: summary keyword match (generic but better than nothing)
        if title_words and not is_match:
            summary_txt = str(data.get('summary', '')).lower()
            if any(w in summary_txt for w in title_words):
                is_match = True

        # 3. Tertiary: exact cycle_id match
        if not is_match:
            is_match = data.get('cycle_id') == cycle_id

        if is_match:
            candidates.append((mtime, data))

    candidates.sort(key=lambda x: x[0], reverse=True)
    return [d for _, d in candidates[:max_attempts]]


def _duplicate_check_title(req: dict, backlog_title: str) -> str:
    """Return the title to use for the pre-spawn duplicate check (#713).

    The coordinator-derived `backlog_title` is preferred (it is the most
    reliable, artifact-sourced title), but a request may carry no backlog
    artifact at all — only its own `task_title` or `semantic_task_id` — and
    that combination previously bypassed the `_task_already_done` gate
    entirely (#711). Falls back in order: backlog_title -> req.task_title ->
    req.semantic_task_id -> '' (no title available, gate is skipped as before).
    """
    return backlog_title or req.get('task_title') or req.get('semantic_task_id') or ''


def _migrate_backlog_title_in_results(results_dir: 'Path',
                                      entries: 'list[tuple[Path, dict, float]] | None' = None) -> int:
    """One-time migration: backfill backlog_title into existing bridge result files.

    Iterates bridge_llm_execution results that lack backlog_title and reads the
    title from source_artifact → next_bounded_candidate.title. Idempotent.
    Returns count of files updated.
    """
    if not results_dir.exists():
        return 0
    import json as _json
    updated = 0
    try:
        for f, data, _mtime in _iter_result_entries(results_dir, entries=entries):
            if data.get('materialized_from') != 'bridge_llm_execution':
                continue
            if 'backlog_title' in data:
                continue  # already migrated
            src = data.get('source_artifact', '')
            if not src:
                continue
            try:
                art = _json.loads(Path(src).read_text(encoding='utf-8'))
                title = (art.get('next_bounded_candidate') or {}).get('title', '')
            except Exception:
                continue
            if not title:
                continue
            data['backlog_title'] = title
            try:
                f.write_text(_json.dumps(data, indent=2, ensure_ascii=False), encoding='utf-8')
                updated += 1
            except Exception:
                pass
    except Exception:
        pass
    if updated > 0:
        _clear_result_entries_cache()
    return updated


def _capture_pre_spawn_sha(selfevo_repo: 'Path', sha_file: 'Path') -> str:
    """Record HEAD SHA of selfevo repo before subagent spawn.

    Written to sha_file unconditionally (overwrites). Returns SHA or '' on error.
    Used by _count_commits_since() to count commits the subagent made, even
    when the subagent pushes itself (which makes origin/main..HEAD = 0).
    """
    import subprocess as _sp_sha
    try:
        r = _sp_sha.run(
            ['git', '-c', f'safe.directory={selfevo_repo}', '-C', str(selfevo_repo),
             'rev-parse', 'HEAD'],
            capture_output=True, text=True,
        )
        sha = r.stdout.strip()
        if sha and r.returncode == 0:
            sha_file.write_text(sha, encoding='utf-8')
            return sha
    except Exception:
        pass
    return ''


def _count_commits_since(selfevo_repo: 'Path', pre_spawn_sha: str) -> int:
    """Count commits in selfevo_repo made since pre_spawn_sha.

    Uses git rev-list <pre_spawn_sha>..HEAD so it counts commits regardless
    of whether the subagent or bridge auto-push did the actual git push.
    Returns 0 on any error or if pre_spawn_sha is empty.
    """
    if not pre_spawn_sha:
        return 0
    import subprocess as _sp_cnt
    try:
        r = _sp_cnt.run(
            ['git', '-c', f'safe.directory={selfevo_repo}', '-C', str(selfevo_repo),
             'rev-list', '--count', f'{pre_spawn_sha}..HEAD'],
            capture_output=True, text=True,
        )
        n = r.stdout.strip()
        if r.returncode == 0 and n.isdigit():
            return int(n)
    except Exception:
        pass
    return 0



def _changed_files_and_violations(repo_root: 'Path', base_sha: str) -> 'tuple[list[str], list[str], list[str], str]':
    """Compute the full changed-file set of ``base_sha..HEAD`` and classify its surface.

    #678 F1/F3: returns ``(files_changed, blocked_pattern_violations,
    mutation_violations, tier)`` for ALL commits since ``base_sha`` (the pre-spawn
    SHA), so it reflects the initial subagent commit(s) AND every subsequent
    repair-turn commit. The gate decision recomputes this immediately before it
    decides whether to integrate — a repair subagent that edits a deny-set
    ``nanobot/`` file or drops a secret-shaped file must be caught even though the
    first commit was clean. On any git failure returns ``([], [], [], 'script')``
    — the caller keeps the last-known lists rather than silently treating a broken
    diff as "clean".

    ``blocked_pattern_violations`` are secret/lockfile/.git filename matches
    (``_BLOCKED_FILE_PATTERNS``); ``mutation_violations`` are edits outside both
    the script surface (``_ALLOWED_PATH_PREFIXES``) and the operator-approved
    runtime slice, plus any deny-set hit; ``tier`` is ``'script'`` or ``'runtime'``
    (#812 — runtime-slice cycles are gated to a promotion candidate, not merged).
    """
    import subprocess as _sp
    git = _git_cmd(repo_root)
    try:
        diff = _sp.run(git + ['diff', '--name-only', base_sha, 'HEAD'], capture_output=True, text=True)
    except Exception:
        return [], [], [], 'script'
    if diff.returncode != 0:
        return [], [], [], 'script'
    files_changed = [f for f in diff.stdout.splitlines() if f.strip()]
    # #812: two-tier classification (script vs operator-approved runtime slice).
    blocked, mutation, tier = _classify_mutation_surface(files_changed)
    return files_changed, blocked, mutation, tier


def _check_test_weakening(repo_root: 'Path', base_sha: str) -> 'tuple[bool, list[str], list[str]]':
    """#1119: run the deterministic test-weakening detector for ``base_sha..HEAD``.

    Thin bridge-side wrapper around :func:`nanobot.runtime.test_guard.evaluate`
    — same recompute discipline as :func:`_changed_files_and_violations`
    (called at the same points: initial commit, every repair retry, and the
    final pre-gate recompute), so a repair turn that itself weakens a test
    (or CURES a previously-flagged weakening) is caught on the very next gate
    re-run. Returns ``(blocked, hard_violations, soft_signals)``. Fail-open:
    on any unexpected error returns ``(False, [], [])`` — a detector bug must
    never block an otherwise-clean cycle.
    """
    try:
        verdict = _evaluate_test_weakening(repo_root, base_sha, 'HEAD')
        return (
            bool(verdict.get('blocked')),
            list(verdict.get('hard_violations') or []),
            list(verdict.get('soft_signals') or []),
        )
    except Exception:
        return False, [], []


def _diff_against_remote_touches_only(repo_root: 'Path', remote_ref: str, allowed: 'set[str]') -> bool:
    """Return True iff every file changed between ``remote_ref`` and local HEAD is in ``allowed``.

    #678 F5/F6: several bookkeeping code paths (already_done mark-done, memory
    archiver, structured lesson) commit and ``git push origin main`` directly,
    with NO smoke gate at all. This is the only thing standing between those
    paths and an unconstrained push — it must be checked immediately before each
    such push. Returns False (refuse to push) on any git failure, or when there
    is nothing to push, or when the diff touches anything outside ``allowed`` —
    fail closed in every ambiguous case.
    """
    import subprocess as _sp
    git = _git_cmd(repo_root)
    try:
        diff = _sp.run(git + ['diff', '--name-only', remote_ref, 'HEAD'], capture_output=True, text=True)
    except Exception:
        return False
    if diff.returncode != 0:
        return False
    changed = [f.strip() for f in diff.stdout.splitlines() if f.strip()]
    if not changed:
        return False
    return all(f in allowed for f in changed)


def _safe_ref_id(cycle_id: str) -> str:
    """Sanitize a cycle_id into a safe git ref component (branch/tag name).

    Shared by ``_setup_cycle_branch`` (branch names) and the #721 tag helpers
    (tag names) — both need the same character restriction and length cap.
    """
    import re as _re_ref

    return _re_ref.sub(r'[^A-Za-z0-9._-]', '-', str(cycle_id or 'unknown'))[:80]


def _setup_cycle_branch(repo_root: 'Path', cycle_id: str, state_dir: 'Path | None' = None) -> dict:
    """Isolate the upcoming subagent run on a fresh branch off ``origin/main``.

    Implements R8/R9 of docs/specs/subagent-bridge/spec.md: the subagent commits
    against ``selfevo/cycle-<cycle_id>``, never directly against ``main``, so a
    self-push (or a bridge crash mid-run) can only ever publish the cycle
    branch — never ``origin/main``.

    #877 (git-native evolutionary tree): after resolving the real
    ``origin/main`` sha, checks whether the coordinator's population
    archive (``archive.CycleArchive.stalled()``) judges the current line
    stalled — if so, and ``evolution_tree.should_switch`` has a stronger
    dormant line to offer, the cycle branches off THAT sha instead (a
    git-native "switch to a stronger line": population = branches,
    generation = commit — see docs/changes/877-evolution-tree). Before
    abandoning the old tip, a ``evo/node-<sha[:12]>`` keeper branch is
    created so the loser stays reachable, never deleted. Byte-identical
    to pre-#877 behaviour whenever the archive is empty/not stalled or the
    tree has no better candidate — fully fail-open, wrapped so an error
    anywhere in this decision never blocks the cycle.

    Returns ``{"ok": bool, "branch": str, "main_sha": str,
    "origin_main_sha": str, "reason": str | None}``. ``main_sha`` is the
    BASE this cycle actually branched from (may be a switched-to ancestor);
    ``origin_main_sha`` is always the real, unswitched ``origin/main`` tip
    observed at setup time (used by the caller for out-of-band-drift
    detection and as the integration push's force-with-lease value).
    ``reason`` is set only when ``ok`` is False, e.g. ``"repo_missing"``,
    ``"not_a_git_repo"``, ``"dirty_tree"``, ``"fetch_failed"``, ``"checkout_failed"``.
    ``state_dir`` defaults to the module-level ``STATE_DIR`` when omitted
    (existing callers/tests are unaffected). Never raises — git/subprocess
    failures degrade to a blocked result.
    """
    import subprocess as _sp_setup

    safe_cycle_id = _safe_ref_id(cycle_id)
    branch = f'selfevo/cycle-{safe_cycle_id}'

    if not repo_root.is_dir():
        return {'ok': False, 'branch': branch, 'main_sha': '', 'origin_main_sha': '', 'reason': 'repo_missing'}

    git = _git_cmd(repo_root)
    try:
        status = _sp_setup.run(git + ['status', '--porcelain'], capture_output=True, text=True)
    except Exception:
        return {'ok': False, 'branch': branch, 'main_sha': '', 'origin_main_sha': '', 'reason': 'not_a_git_repo'}
    if status.returncode != 0:
        return {'ok': False, 'branch': branch, 'main_sha': '', 'origin_main_sha': '', 'reason': 'not_a_git_repo'}
    if status.stdout.strip():
        return {'ok': False, 'branch': branch, 'main_sha': '', 'origin_main_sha': '', 'reason': 'dirty_tree'}

    try:
        fetch = _sp_setup.run(git + ['fetch', 'origin', 'main'], capture_output=True, text=True)
    except Exception:
        fetch = None
    if fetch is None or fetch.returncode != 0:
        return {'ok': False, 'branch': branch, 'main_sha': '', 'origin_main_sha': '', 'reason': 'fetch_failed'}

    main_sha = _sp_setup.run(git + ['rev-parse', 'origin/main'], capture_output=True, text=True).stdout.strip()

    # #877: git-native evolutionary tree — a stalled line may switch to a
    # stronger dormant one. `base` starts at the real origin/main and is
    # only ever overridden below; every failure mode (archive load error,
    # no tree, not stalled, no candidate, missing commit) falls through
    # with `base` unchanged — byte-identical to pre-#877 behaviour.
    base = main_sha
    _sd = state_dir if state_dir is not None else STATE_DIR
    try:
        from nanobot.runtime.archive import CycleArchive
        _archive = CycleArchive()
        _archive.load(Path(_sd) / 'goals' / 'cycle_archive.json')
        if _archive.stalled():
            from nanobot.runtime import evolution_tree as _evo_tree
            _target = _evo_tree.should_switch(_sd, True, main_sha)
            if _target:
                _target_sha, _target_branch = _target
                _exists = _sp_setup.run(
                    git + ['cat-file', '-e', f'{_target_sha}^{{commit}}'],
                    capture_output=True, text=True,
                )
                if _exists.returncode == 0:
                    # Keeper ref at the abandoned tip BEFORE switching —
                    # losers stay reachable as branches, never deleted.
                    _sp_setup.run(
                        git + ['branch', '-f', f'evo/node-{main_sha[:12]}', main_sha],
                        capture_output=True, text=True,
                    )
                    append_event(_sd, {
                        'phase': 'evolution_tree',
                        'reason': 'line_switch',
                        'from_sha': main_sha,
                        'to_sha': _target_sha,
                    })
                    _evo_tree.record_switch(_sd, from_sha=main_sha, to_sha=_target_sha, reason='stalled')
                    base = _target_sha
    except Exception:
        base = main_sha

    checkout = _sp_setup.run(git + ['checkout', '-B', branch, base], capture_output=True, text=True)
    if checkout.returncode != 0:
        return {'ok': False, 'branch': branch, 'main_sha': base, 'origin_main_sha': main_sha, 'reason': 'checkout_failed'}

    # #830: bound leaked cycle branches. Integrated ones are removed by
    # _cleanup_cycle_branch, but forensic (gate-failed/blocked) branches were
    # never pruned and grew one-per-failed-cycle. Prune now that we sit on the
    # fresh branch; best-effort so a prune failure never blocks the cycle.
    _prune_stale_cycle_branches(repo_root, state_dir=_sd)

    return {'ok': True, 'branch': branch, 'main_sha': base, 'origin_main_sha': main_sha, 'reason': None}


def _integrate_cycle_to_main(
    repo_root: 'Path', cycle_branch: str, main_sha_before: str,
    expected_origin_main: 'str | None' = None,
) -> dict:
    """Merge a green cycle branch into ``main`` and push — the ONLY way ``origin/main`` advances.

    Implements R12/R14 of docs/specs/subagent-bridge/spec.md: ``--no-ff`` merge of
    the cycle HEAD onto ``main`` reset to ``main_sha_before``, then push. Any
    failure (merge conflict, rejected push) leaves ``main`` reset back to
    ``main_sha_before`` — ``origin/main`` is never left in a half-merged state.

    #877: the push always uses ``--force-with-lease=main:<lease>`` rather
    than a plain push. ``expected_origin_main`` — when omitted, defaults to
    ``main_sha_before`` (the pre-#877 caller shape: identical outcome to a
    plain push, since a fast-forward IS exactly what a matching-lease
    force-with-lease produces) — should be the REAL ``origin/main`` sha
    observed right before this cycle began. That distinction only matters
    when the cycle branched off an ANCESTOR via a #877 line switch: there
    ``main_sha_before`` is the switched-to ancestor, not the current
    ``origin/main``, so a plain push (or a lease matching the ancestor)
    would always be rejected as a non-fast-forward. Passing the true prior
    ``origin/main`` sha as ``expected_origin_main`` makes the rewrite an
    atomic compare-and-swap: it succeeds for exactly this one intentional
    line switch while still rejecting any OTHER concurrent origin/main
    movement (out-of-band race safety, #846 — see ``_detect_out_of_band_main``,
    which the caller must also point at ``expected_origin_main``, not
    ``main_sha_before``, for the same reason).

    Returns ``{"ok": bool, "main_sha_after": str, "reason": str | None}``.
    """
    import subprocess as _sp_int

    git = _git_cmd(repo_root)
    base = main_sha_before or 'origin/main'

    # #828: force the shared checkout onto the CYCLE BRANCH's committed tip with a
    # clean tree BEFORE moving to main. Some subagents run `git checkout ...` / other
    # git ops mid-cycle despite the branch-discipline rule, leaving the tree dirty
    # (and occasionally moving HEAD off the cycle branch). Without this, a dirty tree
    # makes `git checkout -B main` refuse to overwrite local modifications and return
    # checkout_main_failed — silently discarding the committed, gate-passing cycle
    # work (observed 8x/24h once a capable model started producing integrable work).
    # Checking out ``cycle_branch`` BY NAME (not a bare HEAD-relative reset) also
    # guarantees we integrate the real committed deliverable regardless of where a
    # misbehaving subagent left HEAD (#828 review). SAFE: the deliverable is COMMITTED
    # on ``cycle_branch``; only non-deliverable stray tree changes are cleared.
    co_cycle = _sp_int.run(git + ['checkout', '-f', cycle_branch], capture_output=True, text=True)
    if co_cycle.returncode != 0:
        return {'ok': False, 'main_sha_after': main_sha_before, 'reason': 'checkout_cycle_failed'}
    _sp_int.run(git + ['clean', '-fd'], capture_output=True)

    checkout_main = _sp_int.run(git + ['checkout', '-B', 'main', base], capture_output=True, text=True)
    if checkout_main.returncode != 0:
        return {'ok': False, 'main_sha_after': main_sha_before, 'reason': 'checkout_main_failed'}

    merge = _sp_int.run(
        git + ['merge', '--no-ff', cycle_branch, '-m', f'merge: integrate {cycle_branch}'],
        capture_output=True, text=True,
    )
    if merge.returncode != 0:
        _sp_int.run(git + ['merge', '--abort'], capture_output=True)
        _sp_int.run(git + ['reset', '--hard', base], capture_output=True)
        return {'ok': False, 'main_sha_after': main_sha_before, 'reason': 'merge_conflict'}

    # #828 review: a `--no-ff` merge of a cycle branch with real commits ALWAYS
    # creates a merge commit, so HEAD advances past ``base``. If HEAD is still at
    # ``base`` here, ``cycle_branch`` had nothing to integrate (e.g. a misbehaving
    # subagent committed OFF the cycle branch) — fail loudly rather than push a
    # no-op and falsely report the cycle as integrated with ``main`` unchanged.
    _post_merge = _sp_int.run(git + ['rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip()
    # Resolve ``base`` to a sha for the comparison — it may be the literal
    # 'origin/main' fallback, which would never equal a resolved HEAD sha and so
    # silently defeat the guard (#828 review LOW). Only fire when both resolve.
    _base_sha = _safe_rev_parse(repo_root, base)
    if _post_merge and _base_sha and _post_merge == _base_sha:
        _sp_int.run(git + ['reset', '--hard', base], capture_output=True)
        return {'ok': False, 'main_sha_after': main_sha_before, 'reason': 'empty_integration'}

    # #877: always force-with-lease (see docstring). A ``None``/falsy lease
    # (e.g. a genuinely first-ever push with no known prior value) falls
    # back to a plain push rather than force-pushing blind.
    _lease_sha = expected_origin_main if expected_origin_main is not None else main_sha_before
    if _lease_sha:
        push = _sp_int.run(
            git + ['push', f'--force-with-lease=main:{_lease_sha}', 'origin', 'main'],
            capture_output=True, text=True,
        )
    else:
        push = _sp_int.run(git + ['push', 'origin', 'main'], capture_output=True, text=True)
    if push.returncode != 0:
        _sp_int.run(git + ['reset', '--hard', base], capture_output=True)
        return {'ok': False, 'main_sha_after': main_sha_before, 'reason': 'push_rejected'}

    main_sha_after = _sp_int.run(git + ['rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip()
    return {'ok': True, 'main_sha_after': main_sha_after, 'reason': None}


def _detect_out_of_band_main(repo_root: 'Path', main_sha_before: str) -> str:
    """Return origin/main's current sha if it has moved away from
    ``main_sha_before`` OUT OF BAND (#846) — i.e. a push that did not go
    through :func:`_integrate_cycle_to_main`. The bridge loop is serial, so
    within one cycle origin/main can only move via this cycle's own
    integrate; any other movement means the subagent (or something) pushed
    directly, bypassing the gate. Fetches origin/main first. FAIL-OPEN:
    returns '' (no drift) on any error, missing sha, or empty
    ``main_sha_before`` — a detection bug must never block a legitimate
    cycle. A truthy return is a positively-confirmed out-of-band drift."""
    import subprocess as _sp_oob

    if not main_sha_before:
        return ''
    try:
        git = _git_cmd(repo_root)
        # Best-effort: a fetch failure (offline host, transient network) must
        # not stop us from checking whatever origin/main ref is already known
        # locally — the surrounding try/except still fails this whole helper
        # open (returns '') if rev-parse itself then errors.
        _sp_oob.run(git + ['fetch', 'origin', 'main'], capture_output=True, text=True, timeout=30)
        observed = _safe_rev_parse(repo_root, 'origin/main')
        if observed and observed != main_sha_before:
            return observed
        return ''
    except Exception:
        return ''


def _safe_rev_parse(repo_root: 'Path', ref: str) -> str:
    """``git rev-parse <ref>``, returning ``''`` on any failure (never raises)."""
    import subprocess as _sp_rp

    try:
        result = _sp_rp.run(
            _git_cmd(repo_root) + ['rev-parse', ref], capture_output=True, text=True, timeout=10,
        )
        return result.stdout.strip() if result.returncode == 0 else ''
    except Exception:
        return ''


def _cycle_tag_exists(repo_root: 'Path', tag_name: str) -> bool:
    """Return True iff the local tag ``tag_name`` exists. Fail-open (False on any error)."""
    import subprocess as _sp_texist

    try:
        if not repo_root.is_dir():
            return False
        result = _sp_texist.run(
            _git_cmd(repo_root) + ['tag', '--list', tag_name], capture_output=True, text=True, timeout=10,
        )
        return result.returncode == 0 and tag_name in result.stdout.split()
    except Exception:
        return False


def _tag_cycle_pre(repo_root: 'Path', cycle_id: str, main_sha: str) -> None:
    """Tag ``pre-cycle-<id>`` at ``main_sha`` — the pre half of the #721 bracket.

    KB-mined a-evolve pattern (``pre-evo-*``/``evo-*`` version tags): every
    cycle gets a git-native rollback anchor bracketing its mutation, without
    any new storage. LOCAL ONLY — never pushed to origin; ``origin/main``
    only ever advances via ``_integrate_cycle_to_main``'s explicit push.
    Fail-open (a tag failure must never block a cycle); ``-f`` so a retried
    cycle reusing the same ``cycle_id`` overwrites cleanly instead of erroring.
    """
    if not main_sha:
        return
    import subprocess as _sp_tagpre

    try:
        if not repo_root.is_dir():
            return
        _sp_tagpre.run(
            _git_cmd(repo_root) + ['tag', '-f', f'pre-cycle-{_safe_ref_id(cycle_id)}', main_sha],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        pass


def _maybe_propose_after_skip(selfevo_repo: 'Path') -> None:
    """Invoke the #707 LLM proposer after a pre-spawn duplicate skip.

    First canary finding: the deterministic planner mints stale duplicate
    requests faster than the bridge consumes them, so the queue never empties
    and the no-pending-request hook in ``_main_impl`` (near the top, guarded
    by ``if not req_path``) never runs — a queue full of duplicates IS
    novelty exhaustion. Called from the ``already_done`` and
    ``_recent_failure_match`` pre-spawn skip branches, after their terminal
    ledger/result bookkeeping, right before their ``return 0``. Fails open
    (``maybe_propose`` never raises), so this is safe to call unconditionally.
    The written request (if any) queues behind whatever stale requests remain
    and is picked up oldest-first over the next few cycles as the queue
    drains — no reordering logic needed.
    """
    _proposer_title = llm_proposer.maybe_propose(STATE_DIR, selfevo_repo)
    if _proposer_title:
        print(f'llm-proposer: queued {_proposer_title}')


def _tag_cycle_post(repo_root: 'Path', cycle_id: str, outcome: str, sha: str | None = None) -> None:
    """Tag ``cycle-<id>-<outcome>`` at the terminal HEAD — the post half of the #721 bracket.

    ``outcome`` should be the same enum value passed to this cycle's
    ``cycle_ledger.record_cycle_outcome`` call — coerced to ``'failed'`` if not
    one of :data:`nanobot.runtime.cycle_ledger.VALID_OUTCOMES`, mirroring that
    function's own coercion so the tag and the ledger row can never disagree.
    ``sha`` defaults to the repo's current HEAD when omitted — used by the
    pre-spawn skip paths, which terminate before any cycle branch exists.
    Fail-open, local-only — see :func:`_tag_cycle_pre`.
    """
    if outcome not in VALID_OUTCOMES:
        outcome = 'failed'
    import subprocess as _sp_tagpost

    try:
        if not repo_root.is_dir():
            return
        target = sha or _safe_rev_parse(repo_root, 'HEAD')
        if not target:
            return
        _sp_tagpost.run(
            _git_cmd(repo_root) + ['tag', '-f', f'cycle-{_safe_ref_id(cycle_id)}-{outcome}', target],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        pass


def _prune_cycle_tags(repo_root: 'Path', keep_days: int | None = None) -> None:
    """Delete local ``pre-cycle-*``/``cycle-*`` tags older than the retention window (#721).

    Bounded (inspects at most :data:`_PRUNE_TAG_CAP` tags per run) and
    fail-open: any git failure here must never block a bridge cycle.
    Local-only — no tag is ever pushed, so this never touches origin.
    ``keep_days`` defaults to ``CYCLE_TAG_RETENTION_DAYS`` (default 30),
    mirroring ``cycle_ledger``'s own ``CYCLE_LEDGER_RETENTION_DAYS`` pattern.
    """
    import subprocess as _sp_prune
    import time as _time_prune

    if keep_days is None:
        raw = os.environ.get('CYCLE_TAG_RETENTION_DAYS', '').strip()
        try:
            keep_days = max(1, int(raw)) if raw else 30
        except ValueError:
            keep_days = 30

    try:
        if not repo_root.is_dir():
            return
        git = _git_cmd(repo_root)
        listed = _sp_prune.run(
            git + ['tag', '--list', 'pre-cycle-*', 'cycle-*'],
            capture_output=True, text=True, timeout=10,
        )
        if listed.returncode != 0:
            return
        tag_names = [t for t in listed.stdout.splitlines() if t.strip()][:_PRUNE_TAG_CAP]
        if not tag_names:
            return
        cutoff = _time_prune.time() - (keep_days * 86400)
        for tag in tag_names:
            try:
                dated = _sp_prune.run(
                    git + ['log', '-1', '--format=%ct', tag], capture_output=True, text=True, timeout=10,
                )
                ts = int(dated.stdout.strip())
            except Exception:
                continue
            if ts < cutoff:
                _sp_prune.run(git + ['tag', '-d', tag], capture_output=True, text=True, timeout=10)
    except Exception:
        pass


def _cleanup_cycle_branch(repo_root: 'Path', cycle_branch: str) -> bool:
    """Delete a cycle branch after it has been integrated into ``main`` (R15).

    Best-effort: a failed delete is not itself a bridge failure (the branch is
    already merged; a stray local ref is forensically harmless).
    """
    import subprocess as _sp_clean
    try:
        git = _git_cmd(repo_root)
        result = _sp_clean.run(git + ['branch', '-D', cycle_branch], capture_output=True, text=True)
        return result.returncode == 0
    except Exception:
        return False


_FORENSIC_CYCLE_BRANCH_KEEP = 20


_EVO_NODE_REF_KEEP = 20


def _prune_stale_cycle_branches(
    repo_root: 'Path', keep: int = _FORENSIC_CYCLE_BRANCH_KEEP, state_dir: 'Path | None' = None,
) -> dict:
    """Bound the number of leaked ``selfevo/cycle-*`` branches (#830).

    Every cycle runs on its own ``selfevo/cycle-<id>`` branch. Integrated
    branches are removed by ``_cleanup_cycle_branch``; forensic ones
    (gate-failed, blocked-file, repair-exhausted, empty-integration) are
    intentionally kept for inspection but were never bounded, so they grew
    one-per-failed-cycle without limit (162 accumulated on the eeepc host).
    The full per-cycle record already lives in ``state/ledger/cycles.jsonl`` —
    a branch older than the retention window adds nothing the ledger lacks.

    Deletes: (a) every cycle branch already merged into ``origin/main`` (its
    commits are safely in main), and (b) all but the newest ``keep`` unmerged
    (forensic) cycle branches by commit date. Never deletes the branch that is
    currently checked out, nor one whose tip sha is still indexed by the
    #877 evolution tree (``evolution_tree.tree_indexed_shas`` — fail-open to
    an empty set, so this exemption is a no-op wherever the tree is absent
    or empty). Also prunes the ``evo/node-*`` keeper refs #877 line-switches
    create, capped at :data:`_EVO_NODE_REF_KEEP` (see
    :func:`_prune_evo_node_refs`). Best-effort: never raises; a failed
    delete is not a cycle failure.

    Returns ``{"deleted": int, "kept": int}``.
    """
    import subprocess as _sp_prune

    if not repo_root.is_dir():
        return {'deleted': 0, 'kept': 0}
    git = _git_cmd(repo_root)

    def _run(args):
        return _sp_prune.run(git + args, capture_output=True, text=True)

    _sd = state_dir if state_dir is not None else STATE_DIR
    try:
        from nanobot.runtime.evolution_tree import tree_indexed_shas as _tree_indexed_shas
        _indexed_shas = _tree_indexed_shas(_sd)
    except Exception:
        _indexed_shas = set()

    try:
        current = _run(['rev-parse', '--abbrev-ref', 'HEAD']).stdout.strip()

        merged_out = _run(
            ['branch', '--merged', 'origin/main', '--list', 'selfevo/cycle-*']
        ).stdout
        merged = {
            ln.strip().lstrip('*+ ').strip()  # '*' current, '+' checked-out in a worktree
            for ln in merged_out.splitlines()
            if ln.strip()
        }

        listed = _run(
            ['for-each-ref', '--sort=-committerdate',
             '--format=%(refname:short)', 'refs/heads/selfevo/cycle-*']
        ).stdout
        all_branches = [ln.strip() for ln in listed.splitlines() if ln.strip()]
        unmerged = [b for b in all_branches if b not in merged]

        to_delete = set(merged)
        to_delete.update(unmerged[keep:])
        to_delete.discard(current)  # never delete the branch we are on
        to_delete.discard('')

        # #877: never delete a branch whose tip sha the evolution tree still
        # indexes (e.g. a forensic branch that also happens to be a
        # recorded node's source — belt-and-suspenders alongside the
        # evo/node-* keeper refs, which live under a different prefix and
        # are never matched by the selfevo/cycle-* glob above anyway).
        if _indexed_shas and to_delete:
            _survivors = {
                b for b in to_delete
                if _run(['rev-parse', b]).stdout.strip() in _indexed_shas
            }
            to_delete -= _survivors

        deleted = 0
        for branch in to_delete:
            if _run(['branch', '-D', branch]).returncode == 0:
                deleted += 1

        deleted += _prune_evo_node_refs(repo_root, current, _sd)

        return {'deleted': deleted, 'kept': max(len(all_branches) - deleted, 0)}
    except Exception:
        return {'deleted': 0, 'kept': 0}


def _prune_evo_node_refs(repo_root: 'Path', current_branch: str, state_dir: 'Path | None' = None) -> int:
    """Bound the ``evo/node-*`` keeper refs a #877 line switch creates.

    Keeps the newest :data:`_EVO_NODE_REF_KEEP` refs by commit date,
    deleting older ones. Never deletes the currently checked-out branch,
    nor one whose tip sha equals the evolution tree's live
    ``current_sha`` (the current line's own keeper, if any happens to
    exist). Fail-open: any error returns 0 (nothing deleted), never raises.
    """
    import subprocess as _sp_evo

    if not repo_root.is_dir():
        return 0
    git = _git_cmd(repo_root)

    def _run(args):
        return _sp_evo.run(git + args, capture_output=True, text=True)

    try:
        _sd = state_dir if state_dir is not None else STATE_DIR
        try:
            from nanobot.runtime.evolution_tree import current_sha as _tree_current_sha
            _live_sha = _tree_current_sha(_sd) or ''
        except Exception:
            _live_sha = ''

        listed = _run(
            ['for-each-ref', '--sort=-committerdate',
             '--format=%(refname:short) %(objectname)', 'refs/heads/evo/node-*']
        ).stdout
        entries = []
        for ln in listed.splitlines():
            ln = ln.strip()
            if not ln:
                continue
            parts = ln.split(' ', 1)
            if len(parts) == 2:
                entries.append((parts[0], parts[1]))

        stale = entries[_EVO_NODE_REF_KEEP:]
        deleted = 0
        for name, sha in stale:
            if name == current_branch or sha == _live_sha:
                continue
            if _run(['branch', '-D', name]).returncode == 0:
                deleted += 1
        return deleted
    except Exception:
        return 0


def _restore_to_main(repo_root: 'Path') -> bool:
    """Return the shared checkout to ``main`` with a clean tree.

    Called whenever a cycle does NOT end in integration (setup failure, gate
    failure, integration failure) so every other bridge code path can keep
    assuming the checkout sits on ``main``. Discards any uncommitted stray
    changes left by a subagent that forgot to commit — R13/R15's "leave the
    cycle branch for inspection" only covers committed history, never the
    shared working tree.
    """
    import subprocess as _sp_restore
    if not repo_root.is_dir():
        return False
    git = _git_cmd(repo_root)
    try:
        _sp_restore.run(git + ['reset', '--hard'], capture_output=True)
        _sp_restore.run(git + ['clean', '-fd'], capture_output=True)
        result = _sp_restore.run(git + ['checkout', 'main'], capture_output=True, text=True)
        if result.returncode != 0:
            result = _sp_restore.run(git + ['checkout', '-B', 'main', 'origin/main'], capture_output=True, text=True)
        return result.returncode == 0
    except Exception:
        return False


def _auto_commit_uncommitted_work(
    repo_root: 'Path',
    branch: str,
    backlog_title: str = '',
    task_snippet: str = '',
) -> dict:
    """Safety net (#666): commit uncommitted subagent work before the smoke gate runs.

    Live-observed gap (#656 verification, 2026-07-06): a subagent implemented real
    changes via edit_file but finished its turn without running ``git commit``. The
    bridge saw ``cycle_commit_count == 0``, skipped the gate entirely, and the
    ``finally``-block :func:`_restore_to_main` discarded the uncommitted work — every
    following cycle re-did (and re-lost) the same task.

    Only meaningful when the caller has already established ``cycle_commit_count == 0``
    for this cycle. Checks ``git status --porcelain`` itself, so it is a no-op (and
    returns ``committed: False``) on a clean tree.

    Files matching ``_BLOCKED_FILE_PATTERNS`` (secret-shaped names, lockfiles, ``.git``
    internals, ...) are excluded from the auto-commit and reported back — the smoke
    gate remains the actual arbiter of whether the *included* changes are good; this
    function only decides whether the subagent's uncommitted work gets a chance to be
    judged by that gate at all.

    Returns ``{"committed": bool, "excluded": list[str], "files_committed": int}``.
    Never raises — degrades to ``committed: False`` on any git failure.
    """
    import re as _re_auto
    import subprocess as _sp_auto

    git = _git_cmd(repo_root)
    try:
        status = _sp_auto.run(git + ['status', '--porcelain'], capture_output=True, text=True)
    except Exception:
        return {'committed': False, 'excluded': [], 'files_committed': 0}
    if status.returncode != 0 or not status.stdout.strip():
        return {'committed': False, 'excluded': [], 'files_committed': 0}

    changed_files: list[str] = []
    for line in status.stdout.splitlines():
        if not line.strip():
            continue
        # Porcelain format: "XY <path>" (or "XY <old> -> <new>" for renames).
        path = line[3:].strip()
        if ' -> ' in path:
            path = path.split(' -> ', 1)[1]
        path = path.strip('"')
        if path:
            changed_files.append(path)

    # NOTE: intentionally a for-loop, not a comprehension assigned in one shot —
    # tests/test_mutation_surfaces.py AST-extracts any module-level assignment
    # whose source mentions _BLOCKED_FILE_PATTERNS as a "constant" to splice into
    # its isolated exec() of _validate_mutation_surfaces(); a one-line comprehension
    # here would get swept up by that (fragile, but out of scope to fix in #666).
    excluded_set: set[str] = set()
    for f in changed_files:
        if _is_blocked_filename(f):
            excluded_set.add(f)
    excluded = sorted(excluded_set)
    included = [f for f in changed_files if f not in excluded_set]

    if not included:
        return {'committed': False, 'excluded': excluded, 'files_committed': 0}

    for f in included:
        try:
            _sp_auto.run(git + ['add', '--', f], capture_output=True, text=True)
        except Exception:
            pass

    title = (backlog_title or task_snippet or 'subagent task').strip()
    title = _re_auto.sub(r'\s+', ' ', title)[:80]
    subject = f'selfevo: auto-commit uncommitted subagent work — {title}'
    body = (
        f'Subagent finished on {branch} without running git commit; the bridge\n'
        'committed its working-tree changes so the smoke gate can evaluate them (#666).'
    )
    try:
        commit = _sp_auto.run(
            git + ['commit', '-m', subject, '-m', body],
            capture_output=True, text=True,
        )
    except Exception:
        return {'committed': False, 'excluded': excluded, 'files_committed': 0}
    if commit.returncode != 0:
        return {'committed': False, 'excluded': excluded, 'files_committed': 0}
    return {'committed': True, 'excluded': excluded, 'files_committed': len(included)}


# #955 — operator knob: default-off, fail-open prompt dump.
# Write exact system and task prompts to state/prompts/<cycle_id>.{system,task}.txt
# before each subagent spawn so operators can inspect what the bridge sent.
# NOT added to FITNESS_SIDECARS — this is diagnostic output, not a fitness input.
_SELFEVO_DUMP_PROMPTS_ENV = 'SELFEVO_DUMP_PROMPTS'
_DUMP_PROMPTS_RETENTION = 20  # max stored cycle prompt pairs (system + task)


def dump_spawn_prompts(
    state_dir: 'Path',
    cycle_id: str,
    system_prompt: str,
    task_prompt: str,
) -> None:
    """Write system and task prompts to state/prompts/<cycle_id>.{system,task}.txt.

    Gated by ``SELFEVO_DUMP_PROMPTS`` env var (default-off). Fail-open:
    any write or retention error is swallowed so a disk/permission issue
    can never block a spawn. Bounded retention: after writing, prunes the
    oldest ``.system.txt`` files (and matching ``.task.txt``) so at most
    :data:`_DUMP_PROMPTS_RETENTION` cycle pairs are kept.

    Not added to ``FITNESS_SIDECARS`` — this is diagnostic output only.
    """
    if os.environ.get(_SELFEVO_DUMP_PROMPTS_ENV, '0').strip().lower() not in {
        '1', 'true', 'yes', 'on',
    }:
        return
    try:
        prompts_dir = Path(state_dir) / 'prompts'
        prompts_dir.mkdir(parents=True, exist_ok=True)
        # Sanitize cycle_id for use as a filename component.
        import re as _re_dump
        safe_cycle = _re_dump.sub(r'[^A-Za-z0-9._-]', '-', str(cycle_id or 'unknown'))[:80]
        (prompts_dir / f'{safe_cycle}.system.txt').write_text(
            system_prompt, encoding='utf-8',
        )
        (prompts_dir / f'{safe_cycle}.task.txt').write_text(
            task_prompt, encoding='utf-8',
        )
        # Bounded retention: keep newest _DUMP_PROMPTS_RETENTION .system.txt files.
        existing = sorted(
            prompts_dir.glob('*.system.txt'),
            key=lambda p: p.stat().st_mtime,
        )
        excess = len(existing) - _DUMP_PROMPTS_RETENTION
        if excess > 0:
            for old in existing[:excess]:
                try:
                    old.unlink(missing_ok=True)
                    task_counterpart = old.with_suffix('').with_suffix('.task.txt')
                    task_counterpart.unlink(missing_ok=True)
                except Exception:
                    pass
    except Exception:
        pass  # fail-open: never block a spawn


def _recent_activity_context(
    state_dir: 'Path | None',
    selfevo_repo_root: 'Path | None',
    entries: 'list[tuple[Path, dict, float]] | None' = None,
) -> str:
    """Build a '## Recent activity (do not repeat)' block for the proposal prompt (#713).

    Novelty pressure: gives the subagent a quick, honest picture of what was
    just done or just rejected, so it does not re-propose/re-implement the
    same thing. Two sources:

    1. Recently completed — the last ~8 commit subject lines from
       `_recent_git_log` (the same #575 done-detection git-log text already
       used elsewhere in this module).
    2. Recently rejected/no-commit — a FAILURE PROXY, not a ledger: scans the
       same `state/subagents/results/*.json` directory `_get_previous_attempts`
       reads, for the ~5 most recent entries with a `rollback.reason` set or
       `result_status` in {'blocked', 'no_commit'}, WITHOUT filtering by title
       (unlike `_get_previous_attempts`, which is title-scoped). This is a
       best-effort recency signal derived from bridge result files, not a
       durable rejection ledger (see #704 for that).

    Fail-open: any missing dir/repo, or parse error, returns '' so the
    section is simply omitted from the prompt.
    """
    try:
        lines: list[str] = []

        if selfevo_repo_root is not None:
            from nanobot.runtime.goal_text_utils import _recent_git_log
            git_log = _recent_git_log(selfevo_repo_root, since="7 days ago")
            subjects = [ln for ln in git_log.splitlines() if ln.strip()][:8]
            if subjects:
                lines.append('Recently completed (recent commits):')
                lines.extend(f'- {s}' for s in subjects)

        if state_dir is not None:
            results_dir = state_dir / 'subagents' / 'results'
            # #1040: use cached or single-pass result entries
            rejected: list[tuple[float, str]] = []
            for entry_path, data, mtime in _iter_result_entries(results_dir, entries=entries):
                reason = (data.get('rollback') or {}).get('reason')
                status = data.get('result_status')
                if not reason and status not in ('blocked', 'no_commit'):
                    continue
                title = (
                    data.get('backlog_title')
                    or data.get('task_title')
                    or data.get('cycle_id')
                    or '(untitled)'
                )
                note = reason or status or 'rejected'
                rejected.append((mtime, f'{title}: {note}'))
            rejected.sort(key=lambda x: x[0], reverse=True)
            top = [text for _, text in rejected[:5]]
            if top:
                if lines:
                    lines.append('')
                lines.append(
                    'Recently rejected / no-commit (proxy signal, not exhaustive):'
                )
                lines.extend(f'- {t}' for t in top)

        if not lines:
            return ''
        return '\n'.join(['## Recent activity (do not repeat)', *lines, ''])
    except Exception:
        return ''


def build_task(req: dict, goal_text: str, report_source: str,
               state_dir: 'Path | None' = None,
               repair_context: 'str | None' = None,
               selfevo_repo_root: 'Path | None' = None,
               max_iterations: int = 15,
               charter_in_system: bool = False) -> str:
    """Build a concrete task prompt for the subagent from the request payload.

    Args:
        repair_context: If set, adds a '## Repair context' section with the failed test
            traceback. Used by the closed-loop repair cycle (issue #526).
        selfevo_repo_root: If set (with state_dir), used to inject a
            '## Recent activity (do not repeat)' section (#713 novelty pressure).
        max_iterations: Resolved iteration cap for this subagent run (#578/#906).
            Interpolated into the prompt so the agent knows its actual budget.
        charter_in_system: When True the immutable operator charter is already
            present in the SubagentManager system_context (#944); the System
            mission section emits a single pointer line instead of the full
            charter text, eliminating the duplicate (~2.7 KB saved per spawn).
            ``goal_text`` must carry ONLY the derived/filtered priorities when
            this flag is True (#954).
    """
    task_title = req.get('task_title') or req.get('semantic_task_id') or 'subagent review task'
    request_id = req.get('request_id') or req.get('verification_task_id') or '?'
    cycle_id = req.get('cycle_id') or '?'
    goal_id = req.get('goal_id') or '?'
    source_artifact = req.get('source_artifact') or ''

    # Read the source artifact content inline so subagent has concrete data
    artifact_content = ''
    artifact_data: dict = {}
    if source_artifact and Path(source_artifact).exists():
        try:
            raw = Path(source_artifact).read_text(encoding='utf-8')
            artifact_content = raw[:4000]
            if len(raw) > 4000:
                artifact_content += '\n... [truncated]'
            try:
                artifact_data = json.loads(raw)
            except Exception:
                pass
        except Exception as e:
            artifact_content = f'[could not read artifact: {e}]'
    else:
        artifact_content = '[source artifact not found or not specified]'

    # Extract concrete task from backlog if coordinator injected it
    backlog_title = artifact_data.get('next_bounded_candidate', {}).get('title', '')
    backlog_instructions = artifact_data.get('next_bounded_candidate', {}).get('backlog_instructions', '')
    backlog_priority = artifact_data.get('next_bounded_candidate', {}).get('backlog_priority')
    recommended_action = artifact_data.get('recommended_next_action', '')
    # #1118: the proposer's optional, FROZEN falsifiable claim (never rewritten
    # after write_request wrote it) — informational only, never enforced here.
    expected_outcome = artifact_data.get('expected_outcome') if isinstance(artifact_data.get('expected_outcome'), dict) else None

    # Build lessons context block from coordinator-injected cards
    lessons_context = req.get('lessons_context') or {}
    reflection_hints = lessons_context.get('reflection_hints') or []
    lessons_lines: list[str] = []
    if lessons_context.get('relevant_error'):
        err = lessons_context['relevant_error']
        lessons_lines += [
            '## Known pitfall for this task (from lessons/errors.yaml)',
            f"ID: {err.get('id')}  Title: {err.get('title')}",
            f"Root cause: {err.get('root_cause', '')}",
            f"Prevention: {err.get('prevention', '')}",
        ]
        # Add compact related hint if present (#1095); absent when empty → byte-identical.
        if err.get('related'):
            lessons_lines.append(f"Related: {err['related']}")
        lessons_lines.append('')
    if lessons_context.get('relevant_lesson'):
        less = lessons_context['relevant_lesson']
        less_id = less.get('id') or '<unknown>'
        lessons_lines += [
            '## Proven approach for this task (from lessons/lessons.yaml)',
            f"ID: {less_id}  Title: {less.get('title')}",
            f"Problem: {less.get('problem') or less.get('approach', '')}",
            f"Solution: {less.get('solution') or less.get('reusable_insight', '')}",
            f"If you apply this lesson, cite [Lesson {less_id}] in your proposal/response.",
        ]
        # Add compact related hint if present (#1095); absent when empty → byte-identical.
        if less.get('related'):
            lessons_lines.append(f"Related: {less['related']}")
        lessons_lines.append('')
    if reflection_hints:
        lessons_lines += [
            '## Recent reflections (how past cycles worked — steering hints)',
            *[f'- {str(h)[:200]}' for h in reflection_hints[:3]],
            '',
        ]

    lines = [
        f'Task: {task_title}',
        f'Request ID: {request_id}',
        f'Cycle ID: {cycle_id}',
        f'Goal ID: {goal_id}',
    ]
    # #913: report_source is optional now that goal_id no longer requires an
    # outbox bootstrap — an empty value (fresh install / registry-only state)
    # simply omits this cosmetic line instead of printing "Origin report: ".
    if report_source:
        lines.append(f'Origin report: {report_source}')
    # #954: charter is already in system_context when charter_in_system=True;
    # emit a single pointer line so the combined prompt has the charter text
    # exactly once.  goal_text must carry derived/filtered priorities only
    # (not the full charter) when charter_in_system is True.
    if charter_in_system:
        lines += [
            '',
            '## System mission (read before acting)',
            'Full operator charter: see system context (already loaded above).',
            '',
            goal_text,
            '',
        ]
    else:
        lines += [
            '',
            '## System mission (read before acting)',
            goal_text,
            '',
        ]
    _recent_activity = _recent_activity_context(state_dir, selfevo_repo_root)
    if _recent_activity:
        lines.append(_recent_activity)
    lines += [
        '## Source artifact',
        f'Path: {source_artifact}',
        '',
        '```json',
        artifact_content,
        '```',
        '',
    ]
    if lessons_lines:
        lines += lessons_lines

    # Inject doc-only budget notice if coordinator or demand item supplied one (#1090)
    doc_budget_notice = req.get('doc_budget_notice') or artifact_data.get('doc_budget_notice') or ''
    if doc_budget_notice:
        lines += [
            '## Value steering notice',
            str(doc_budget_notice),
            'Prefer code-bearing improvements (scripts/, runtime/, tests/) that create measurable runtime effects.',
            '',
        ]

    # Inject previous attempts section so subagent knows what prior sessions did
    if state_dir is not None and backlog_title:
        _prev = _get_previous_attempts(
            state_dir=state_dir,
            backlog_title=backlog_title,
            cycle_id=str(cycle_id),
        )
        if _prev:
            prev_lines = ['## Previous attempts for this task']
            for i, _p in enumerate(_prev, 1):
                _ts = str(_p.get('created_at', ''))[:16].replace('T', ' ')
                _c = _p.get('commits_pushed', 0) or 0
                _raw_kl = str((_p.get('key_learnings') or ['(no detail)'])[0])
                _kl = _raw_kl[:120]
                if len(_raw_kl) > 120:
                    _kl = _kl.rsplit(' ', 1)[0].rstrip() + '…'
                _status = _p.get('result_status', 'completed')
                if _c > 0:
                    _outcome_str = f'{_c} commit(s) pushed ✓'
                else:
                    _outcome_str = f'no commits ({_status})'
                prev_lines.append(f'- Attempt {i} ({_ts} UTC): {_outcome_str}. {_kl}')
            # #954: removed forced-commit fallback — if the task is already done
            # the executor reports outcome: "skipped" without a mandatory
            # bookkeeping commit (a commit whose only purpose is to mark work
            # done counts as an integration and corrupts the ledger).
            prev_lines.append('')
            lines += prev_lines

    if backlog_title and backlog_instructions:
        curriculum_level = artifact_data.get('next_bounded_candidate', {}).get('curriculum_level')
        curriculum_note = (
            f'Curriculum level: P{curriculum_level} — complete THIS priority before attempting any higher-numbered priority.'
            if curriculum_level else ''
        )
        lines += [
            '## Concrete task to implement',
            f'Priority: {backlog_priority}' if backlog_priority else '',
            f'Title: {backlog_title}',
            curriculum_note,
            '',
            backlog_instructions,
            '',
        ]
    # #1118: surface the frozen claim (informational, never enforced) right
    # after the concrete task — same place regardless of which branch above
    # populated the task, since expected_outcome is orthogonal to backlog vs.
    # recommended_action framing.
    if expected_outcome and expected_outcome.get('claim'):
        lines += [
            '## Expected outcome (frozen claim from the proposal — informational)',
            str(expected_outcome['claim'])[:300],
            '',
        ]
    elif recommended_action and 'Materialize one' not in recommended_action:
        lines += [
            '## Concrete task to implement',
            recommended_action,
            '',
        ]

    lines += [
        '## Branch discipline (MANDATORY)',
        'This checkout is already isolated on a fresh cycle branch off origin/main.',
        'Implement and commit on THIS branch. Do NOT run git checkout/switch/branch,',
        'and do NOT run git push — the bridge integrates your commit(s) into main',
        'itself, only after your changes pass the test-suite gate. A stray push from',
        'this branch cannot reach main (it is not the checked-out branch), but it',
        'still wastes a turn, so just commit and let the bridge handle integration.',
        'Work you do not commit is discarded when this turn ends; commit completed',
        'implementation work on this cycle branch before the session ends.',
        '',
    ]

    _pytest_available = importlib.util.find_spec('pytest') is not None
    _verification_line = (
        '   - Verify: exec(\"python3 -m pytest <affected test file>\") — pytest is installed; run the tests you touch.'
        if _pytest_available
        else '   - Verify: exec(\"python3 -c \'import <module>; print(ok)\'\") or exec(\"python3 <script>\")'
    )
    _verification_note = (
        '' if _pytest_available else '     (pytest is not installed — use python3 -c imports as smoke tests)'
    )
    lines += [
        '## Your instructions',
        'You MUST take a concrete action in this session. Do not return a review only.',
        '',
        '1. Before implementing, check the "Recent activity" section above and',
        '   the codebase — if this task is already done, do NOT re-implement it;',
        '   report outcome: skipped.',
        '2. Read the source artifact and the concrete task above.',
        f'3. Implement the task within the resolved limit of {max_iterations} tool iterations:',
        '   - Write or edit the file using write_file or edit_file.',
        _verification_line,
        _verification_note,
        "   - Commit implementation changes: exec(\"git add <file> && git commit -m '<type>: <what>'\") ",
        '   - Do not create bookkeeping-only commits.',
        '4. If the task is already done or not applicable: report outcome: "skipped" without a bookkeeping commit.',
        '',
        '## Your final response MUST be this JSON (no markdown wrapping):',
        '{',
        '  "action_taken": "<one sentence: what you actually did>",',
        '  "files_changed": ["<path1>", "<path2>"],',
        '  "outcome": "completed" | "skipped" | "blocked",',
        '  "concrete_next_action": "<what the next subagent should do>",',
        '  "findings": ["<observation1>", "<observation2>"]',
        '}',
        '',
        'Use your tools: read_file, write_file, edit_file, list_dir, exec.',
        f'You have up to {max_iterations} tool iterations. Use them deliberately.',
    ]

    # Mutation surfaces are generated from the gate constants above.
    surface_names = list(_ALLOWED_PATH_PREFIXES) + list(_ALLOWED_EXACT_PATHS)
    lines += [
        '',
        '## Mutation surfaces',
        'Allowed targets: ' + ', '.join(surface_names),
        'Creating or improving skills for repeated patterns is valuable work.',
        'Do NOT modify: state/, goals.md, IDENTITY.md, secrets, or systemd units.',
        '',
    ]
    # #812: the runtime-slice tier is enforced entirely at the gate
    # (_classify_mutation_surface + R12b) and is intentionally NOT advertised in
    # this prompt — steering the proposer toward runtime work is #815 (vector
    # bias). Keeping build_task free of the surface helpers also preserves its
    # standalone-exec test contract (tests/test_repair_loop.py).

    # Repair context: injected when previous commit broke tests (closed-loop repair loop)
    if repair_context:
        lines += [
            '',
            '## Repair context — tests failed after your last commit',
            'The smoke test suite ran after your previous commit and FAILED.',
            'You MUST fix the failing tests. Do NOT introduce new failures.',
            '',
            '```',
            repair_context[-1500:],  # keep most recent output (tail)
            '```',
            '',
            'Instructions for this repair turn:',
            '1. Read the traceback above carefully.',
            '2. Edit the file(s) that caused the failure.',
            '3. Verify with: exec("python3 -m pytest tests/ -x -q --tb=short")',
            '4. Commit the fix: git add <file> && git commit -m "fix: repair failing tests"',
            '5. Do NOT exit without at least one commit.',
        ]

    return '\n'.join(lines)


class _NullLock:
    """Sentinel returned by :func:`_acquire_bridge_lock` when ``fcntl`` is
    unavailable (non-POSIX platform). Distinct from ``None`` (which means "the
    lock is held by another process") — a truthy, no-op stand-in so callers can
    treat "locking disabled" and "lock acquired" the same way.
    """

    def close(self) -> None:
        pass


def _acquire_bridge_lock(state_dir: 'Path'):
    """Acquire an exclusive, non-blocking flock on ``<state_dir>/bridge.lock``.

    Defense-in-depth (#680) against concurrent bridge runs: today concurrency
    safety relies solely on systemd's ``Type=oneshot`` single-unit semantics.
    A manual ``python -m nanobot.runtime.bridge`` invocation overlapping a
    timer-triggered cycle (subagent turns can run for up to ~3000s plus repair
    turns) would otherwise race two processes through ``_setup_cycle_branch``/
    ``_git_cmd`` on the same shared checkout — ``checkout -B``/``reset --hard``/
    ``commit``/``push`` from two cycles could interleave and corrupt it.

    Returns an open file handle holding the lock — the caller must keep it
    open for the lifetime of the cycle and ``close()`` it to release — or
    ``None`` if another process already holds it. On platforms without
    ``fcntl`` (non-POSIX; the eeepc host is always Linux), logs a warning and
    returns a :class:`_NullLock` so callers proceed without locking rather
    than hard-failing.
    """
    if fcntl is None:
        print('bridge: fcntl unavailable on this platform; skipping concurrency lock')
        return _NullLock()
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = state_dir / 'bridge.lock'
    handle = open(lock_path, 'a+')
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        # BlockingIOError (lock held) is an OSError subclass; any other flock
        # failure is treated the same way — fail closed, do not proceed.
        handle.close()
        return None
    return handle


async def main():
    """Thin entry point: honour the disabled switch, take the concurrency
    lock, run the cycle, release.

    The actual bridge logic lives in :func:`_main_impl`; this wrapper exists
    solely so the lock (#680) covers the whole cycle via try/finally without
    reindenting ``_main_impl``'s body. The ``BRIDGE_ENABLED`` check stays here
    (ahead of the lock) so a disabled bridge still never touches ``STATE_DIR``
    at all — see ``tests/test_bridge_wrapper.py::test_wrapper_runs_disabled_without_error``.
    """
    if not BRIDGE_ENABLED:
        print('bridge_disabled')
        return 0

    lock_handle = _acquire_bridge_lock(STATE_DIR)
    if lock_handle is None:
        print('bridge: another run holds the lock (bridge.lock); exiting cleanly')
        return 0
    try:
        return await _main_impl()
    finally:
        try:
            lock_handle.close()
        except Exception:
            pass


async def _main_impl():
    # #913: bridge-native hypothesis backlog snapshot — regenerate
    # hypotheses/backlog.json at the END of every bridge run (success,
    # skip, already_handled, no_active_goal, or any early-return/blocked
    # path in between), via a `finally` around the actual cycle logic in
    # `_main_impl_body` below, so this is exactly one call per invocation
    # regardless of which return point that function hits. Never allowed to
    # affect the cycle's own result — wrapped in its own try/except even
    # though write_backlog_snapshot already fails open internally.
    try:
        return await _main_impl_body()
    finally:
        try:
            write_backlog_snapshot(STATE_DIR, STATE_DIR.parent / 'eeebot-self-evolving')
        except Exception:
            pass


def _pickup_staged_promotions(repo_root: 'Path', state_dir: 'Path') -> int:
    """Integrate curator-staged promotions into main as one commit AND push it. (#1001, #1209)

    Called at a safe cycle-start boundary: bridge lock held, HEAD on clean main.
    Reads ``state_dir/curator/staged/manifest.json``, copies fact payloads into
    the repo checkout, appends index lines, merges staged reflector v2 lesson
    cards into ``lessons/lessons.yaml`` (``kind == LESSONS_KIND``), validates
    paths via ``_validate_mutation_surfaces`` (the script-surface gate — NOT
    ``_classify_mutation_surface``, which would incorrectly deny
    ``memory/facts/release-promotion-metadata.md`` via the 'promotion' token
    match in ``_is_runtime_deny``), commits on main, pushes to ``origin/main``
    (a plain, never-forced push), then clears staging only after the push.

    The push is what makes the commit durable (#1209): the cycle branch is cut
    from ``origin/main`` and the integration step runs ``checkout -B main
    <origin base>``, so a commit left on local ``main`` only is orphaned two
    minutes later — on the host six of seven pickup commits were dangling
    (#986) while the journal said "committed N fact(s) on main". If the push is
    rejected (or there is no ``origin``), the commit is dropped with
    ``reset --hard <pre-commit sha>``, staging is retained for the next cycle,
    and every item is recorded ``pickup_deferred`` in ``decisions.jsonl``;
    on success every item is recorded ``promoted`` with the pushed sha.

    Idempotent: if a payload file is missing but the manifest entry remains,
    the entry is skipped (already applied from a prior retry); a manifest whose
    entries are all already applied is cleared without a commit.

    Returns the number of facts plus lesson cards pushed (0 = nothing to do).
    Fail-open: any unexpected error is printed and 0 is returned so the normal
    cycle is never blocked by a pickup failure.
    """
    import subprocess as _sp_pick
    try:
        from nanobot.runtime.knowledge_curator import (
            LESSONS_KIND,
            LESSONS_REL,
            _fact_path,
            apply_staged_lesson_cards,
            clear_staged_manifest,
            load_staged_manifest,
            record_pickup_outcome,
        )
    except Exception as _e:
        print(f'bridge: staged pickup: import failed ({_e}); skipping')
        return 0
    try:
        entries = load_staged_manifest(state_dir)
        if not entries:
            return 0
        staged_dir = state_dir / 'curator' / 'staged'
        git = _git_cmd(repo_root)
        changed_files: list[str] = []
        applied_lesson_ids: list[str] = []
        fact_ids: list[str] = []
        loose_lesson_ids: list[str] = []
        # Filter unsupported entries before any validation (#1094 tier-2).
        # overlap_flag=True means zero keyword overlap between fact and support_claim;
        # these entries are kept in staging for audit but never committed to main.
        unsupported = [
            e for e in entries
            if e.get('overlap_flag') or e.get('verification_status') == 'unsupported'
        ]
        entries = [
            e for e in entries
            if not (e.get('overlap_flag') or e.get('verification_status') == 'unsupported')
        ]
        if unsupported:
            print(
                f'bridge: staged pickup: skipping {len(unsupported)} unsupported entry/entries '
                f'(overlap_flag=True): {", ".join(str(e.get("path")) for e in unsupported)}'
            )
        if not entries:
            return 0
        # Validate the complete manifest before touching the shared checkout.
        # This prevents a malformed entry from partially materializing facts.
        for entry in entries:
            rel = str(entry.get('path') or '').replace('\\', '/')
            slug = str(entry.get('payload_file') or '')
            if str(entry.get('kind') or '') == LESSONS_KIND:
                path_ok = rel == LESSONS_REL
            elif str(entry.get('kind') or '') == 'loose_lesson':
                source_rel = str(entry.get('source_path') or '').replace('\\', '/')
                rel_parts = Path(rel).parts
                source_parts = Path(source_rel).parts
                path_ok = (
                    rel.startswith('lessons/archive/loose/')
                    and rel_parts[:3] == ('lessons', 'archive', 'loose')
                    and len(rel_parts) == 4
                    and rel_parts[3].endswith('.md')
                    and source_rel.startswith('lessons/')
                    and source_parts[:1] == ('lessons',)
                    and len(source_parts) == 2
                    and source_parts[1].endswith('.md')
                )
            else:
                path_ok = _fact_path(rel) is not None
            if not rel or not path_ok or not slug or Path(slug).name != slug:
                print(f'bridge: staged pickup: invalid manifest entry; staging retained: {entry!r}')
                return 0
        snapshot_paths: set[Path] = set()
        for entry in entries:
            rel = str(entry.get('path') or '').replace('\\', '/')
            snapshot_paths.add(repo_root / rel)
            if str(entry.get('action') or '') == 'create':
                index_rel = str(entry.get('index_rel') or '').replace('\\', '/')
                if index_rel:
                    snapshot_paths.add(repo_root / index_rel)
            source_rel = str(entry.get('source_path') or '').replace('\\', '/')
            if source_rel:
                snapshot_paths.add(repo_root / source_rel)
        snapshots = {path: path.read_bytes() if path.exists() else None for path in snapshot_paths}

        def _rollback_pickup() -> None:
            for path, original in snapshots.items():
                try:
                    if original is None:
                        path.unlink(missing_ok=True)
                    else:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(original)
                except Exception:
                    pass

        for entry in entries:
            rel = str(entry.get('path') or '').replace('\\', '/')
            slug = str(entry.get('payload_file') or '')
            action = str(entry.get('action') or '')
            index_line = str(entry.get('index_line') or '')
            index_rel = str(entry.get('index_rel') or '')
            if not rel or not slug:
                continue
            payload_path = staged_dir / slug
            if str(entry.get('kind') or '') == LESSONS_KIND:
                # #1209: reflector v2 cards are merged into the checkout's store
                # here, at the boundary, instead of being written by the curator
                # into a working tree the next reset --hard discards.
                if not payload_path.is_file():
                    continue  # payload already consumed by a prior pickup
                _applied = apply_staged_lesson_cards(
                    repo_root, json.loads(payload_path.read_text(encoding='utf-8')),
                )
                if _applied:
                    applied_lesson_ids.extend(_applied)
                    if rel not in changed_files:
                        changed_files.append(rel)
                continue
            if not payload_path.is_file():
                # Already applied on a prior retry — skip but count as applied.
                if (repo_root / rel).exists():
                    changed_files.append(rel)
                    fact_ids.append(str(entry.get('lesson_id') or rel))
                continue
            if str(entry.get('kind') or '') == 'loose_lesson':
                loose_lesson_ids.append(str(entry.get('lesson_id') or rel))
            else:
                fact_ids.append(str(entry.get('lesson_id') or rel))
            target = repo_root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload_path.read_bytes())
            changed_files.append(rel)
            source_rel = str(entry.get('source_path') or '').replace('\\', '/')
            if source_rel:
                source = repo_root / source_rel
                if source.exists():
                    source.unlink()
                    changed_files.append(source_rel)
            if action == 'create' and index_line and index_rel:
                index_path = repo_root / index_rel
                index_path.parent.mkdir(parents=True, exist_ok=True)
                existing_index = index_path.read_text(encoding='utf-8') if index_path.exists() else ''
                if index_line.rstrip() not in existing_index.splitlines():
                    with index_path.open('a', encoding='utf-8') as _fh:
                        _fh.write('\n' + index_line.rstrip() + '\n')
                    if index_rel not in changed_files:
                        changed_files.append(index_rel)
        if not changed_files:
            # Every entry was already applied (retry after a pickup that
            # committed but whose clear did not run, or cards already present).
            # Clear it so the manifest does not re-run as a no-op every cycle.
            clear_staged_manifest(state_dir, retain_overlap_flag=True)
            print('bridge: staged pickup: nothing to apply; staging cleared')
            return 0
        # Validate via _validate_mutation_surfaces (script-surface gate only).
        # memory/facts/* paths are allowed; no _is_runtime_deny applied here.
        violations = _validate_mutation_surfaces(changed_files)
        if violations:
            _rollback_pickup()
            print(f'bridge: staged pickup: surface violation(s) — aborting pickup, staging retained: {violations}')
            return 0
        n_facts = sum(1 for f in changed_files if f.startswith(('memory/facts/', 'docs/facts/')))
        n_cards = len(applied_lesson_ids)
        n_loose = len(loose_lesson_ids)
        commit_msg = f'curator: promote {n_facts} fact(s), {n_cards} lesson card(s), and {n_loose} loose lesson(s) from staging (#1001, #1209)'
        pre_sha = _sp_pick.run(git + ['rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip()
        add_r = _sp_pick.run(git + ['add'] + changed_files, capture_output=True, text=True)
        if add_r.returncode != 0:
            _rollback_pickup()
            print(f'bridge: staged pickup: git add failed: {add_r.stderr[:200]}')
            return 0
        commit_r = _sp_pick.run(git + ['commit', '-m', commit_msg], capture_output=True, text=True)
        if commit_r.returncode != 0:
            _sp_pick.run(git + ['reset', 'HEAD'] + changed_files, capture_output=True)
            _rollback_pickup()
            print(f'bridge: staged pickup: git commit failed: {commit_r.stderr[:200]}')
            return 0
        # #1209: the commit is durable only once origin/main carries it. A plain
        # (never forced) push: a fast-forward also carries any earlier commit
        # left on local main (e.g. a bridge lesson commit whose own push
        # failed); a non-fast-forward means the remote moved and is rejected.
        all_ids = fact_ids + applied_lesson_ids + loose_lesson_ids
        remote_r = _sp_pick.run(git + ['remote', 'get-url', 'origin'], capture_output=True, text=True)
        if remote_r.returncode != 0:
            push_error = 'no origin remote configured'
        else:
            push_r = _sp_pick.run(git + ['push', 'origin', 'main'], capture_output=True, text=True)
            push_error = '' if push_r.returncode == 0 else (
                (push_r.stderr or push_r.stdout).strip().splitlines()[-1:] or [f'exit {push_r.returncode}']
            )[0][:200]
        if push_error:
            if pre_sha:
                _sp_pick.run(git + ['reset', '--hard', pre_sha], capture_output=True)
            else:
                _rollback_pickup()
            print(
                f'bridge: staged pickup: push to origin/main failed ({push_error}); '
                f'commit dropped, staging retained for retry (#1209)'
            )
            record_pickup_outcome(
                state_dir, all_ids, 'pickup_deferred',
                f'push to origin/main failed: {push_error}',
            )
            return 0
        new_sha = _sp_pick.run(git + ['rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip()
        # Clear staging only after the commit is on origin/main.
        # Preserve unsupported entries (overlap_flag=True) for audit (#1094).
        clear_staged_manifest(state_dir, retain_overlap_flag=True)
        record_pickup_outcome(
            state_dir, fact_ids, 'promoted', f'pushed to origin/main as {new_sha[:12]}',
        )
        record_pickup_outcome(
            state_dir, applied_lesson_ids, 'promoted',
            f'pushed to origin/main as {new_sha[:12]}', LESSONS_REL,
        )
        record_pickup_outcome(
            state_dir, loose_lesson_ids, 'promoted',
            f'pushed to origin/main as {new_sha[:12]}', 'lessons/archive/loose',
        )
        print(
            f'bridge: staged pickup: pushed {n_facts} fact(s), {n_cards} lesson card(s), '
            f'and {n_loose} loose lesson(s) to origin/main {new_sha[:12]} (#1209)'
        )
        return n_facts + n_cards + n_loose
    except Exception as _exc:
        print(f'bridge: staged pickup: unexpected error ({_exc}); staging retained for retry')
        return 0


async def _main_impl_body():
    _clear_result_entries_cache()
    set_config_path(CONFIG_PATH)
    config = load_config(CONFIG_PATH)
    # #721: bounded, fail-open tag pruning — run once per bridge invocation
    # (this function runs exactly once per process, per `main()`'s docstring),
    # right after the concurrency lock in `main()` is held, before anything
    # else touches the shared checkout.
    _selfevo_repo_early = STATE_DIR.parent / 'eeebot-self-evolving'
    _prune_cycle_tags(_selfevo_repo_early)

    # #1083: keep usage evidence and serves confirmation refreshed once per
    # bridge run (fail-open, watermark-gated by 6h/git_head). Bypassed previously
    # whenever queued requests, already_handled, or early gate skips returned
    # before demand.collect_demand() was reached.
    try:
        from nanobot.runtime import usage_evidence

        usage_evidence.refresh_usage(STATE_DIR, _selfevo_repo_early)
        usage_evidence.confirm_serves(STATE_DIR, _selfevo_repo_early)
    except Exception:
        pass

    # #1222: the active goal id comes from the operator's goals/goal_text.json.
    # #913 made goals/registry.json primary and outbox/report.index.json a
    # fallback; both were written only by the coordinator and froze on
    # 2026-08-22 when it was deleted (#916/#923) — twelve days of reading a
    # dead file as the live goal. goal_text.json (seeded by deploy_release.sh)
    # has carried goal_id all along.
    from nanobot.runtime.goal_review import active_goal_id as _active_goal_id_from_canon

    goal_id = _active_goal_id_from_canon(STATE_DIR)
    # build_task's optional "Origin report" line; only the coordinator's
    # outbox ever supplied one.
    report_source = ''

    if not goal_id:
        print('no_active_goal')
        return 0

    BRIDGE_STATE_DIR.mkdir(parents=True, exist_ok=True)

    # #733: bulk-skip pre-spawn duplicates in one run. Each iteration pulls
    # the next pending request; a pre-spawn duplicate (tag-first match,
    # _task_already_done, or _recent_failure_match) does its full bookkeeping
    # then `continue`s to the next request instead of returning — bounded by
    # MAX_SKIPS_PER_RUN so a stale queue can't turn one timer invocation into
    # an unbounded loop. A non-duplicate request `break`s out to the
    # unchanged single-spawn path below. At most one _setup_cycle_branch/
    # spawn happens per run either way (S6 invariant) — the loop only ever
    # iterates on pre-spawn skips.
    _skips = 0
    # #680 precondition (below) verifies shared-checkout repo state, not the
    # request content — it only needs to run once per run, not once per
    # skipped request, since no skip branch touches the cycle-branch/repo
    # state it guards against.
    _precondition_checked = False
    # #733 follow-up: a request whose handled marker exists under the
    # SANITIZED request_id can still slip find_pending_request's real_handled
    # filter (which compares the RAW request_id/marker stem) and keep being
    # returned every iteration — wedging the bulk-skip loop at one skip per
    # run. Track paths already returned this run; if the same path comes back
    # we cannot make progress, so end the run cleanly instead of looping
    # forever (defense-in-depth alongside turning the marker-exists branch
    # below into a `continue`).
    _seen_req_paths: set[str] = set()
    while True:
        req_path, req = find_pending_request()
        if req_path is not None:
            if str(req_path) in _seen_req_paths:
                print('bridge: find_pending_request returned the same request twice this run; ending run')
                return 0
            _seen_req_paths.add(str(req_path))
        if not req_path:
            # #707: state-light LLM proposer — only fires on proven novelty
            # exhaustion (see llm_proposer.should_propose), behind the
            # SELFEVO_LLM_PROPOSER_ENABLED kill-switch (default OFF). Fails open
            # (never raises), so this is safe to call unconditionally here. If it
            # writes a request, the NEXT bridge invocation (next timer cycle)
            # picks it up through the normal find_pending_request/dedup/gate path
            # above — untouched by this change.
            _selfevo_repo_for_proposer = STATE_DIR.parent / 'eeebot-self-evolving'
            _proposer_title = llm_proposer.maybe_propose(STATE_DIR, _selfevo_repo_for_proposer)
            if _proposer_title:
                print(f'llm-proposer: queued {_proposer_title}')
            print('already_handled')
            return 0

        request_id = req.get('request_id') or req.get('verification_task_id') or str(req_path)
        # Issue #675: attribute every LLM call this cycle makes to this bridge
        # invocation. This process runs once per cycle (asyncio.run(main()) via
        # cli_main()) and exits afterward, so there is no need to reset the
        # context — it never outlives this process.
        set_call_context(req.get('cycle_id') or request_id, "bridge")
        safe_id = request_id.replace('/', '_')[:120]
        handled_marker = BRIDGE_STATE_DIR / f'handled_{safe_id}.txt'
        if handled_marker.exists():
            print('already_handled')
            # #733 follow-up: don't cap the whole run on one already-marked
            # request — the _seen_req_paths guard above now protects against
            # find_pending_request re-returning the same request forever, so
            # it's safe to step over this one and keep bulk-skipping.
            continue

        # #720: cycle_id resolved once, up front, so every ledger row for this
        # cycle (write-ahead start, dedup, gate, terminal outcome) joins on the
        # same value. Branch is not known yet (resolved by _setup_cycle_branch
        # below) — the write-ahead row below records it as None.
        _cycle_id = str(req.get('cycle_id') or request_id)
        # #720 piece 3: write-ahead cycle marker, appended BEFORE any dedup check
        # or subagent spawn — a crashed/timed-out cycle leaves this row with no
        # matching terminal outcome row, a deterministic recovery signal.
        record_cycle_started(STATE_DIR, _cycle_id, request_id, None)

        # ── #680 defense-in-depth: HEAD-on-main precondition ────────────────
        # _restore_to_main() below only WARNs when it fails (see the `finally` in
        # the cycle-branch block further down) — it does not abort the *current*
        # cycle, because by the time it runs the cycle already happened. But if a
        # PRIOR cycle's restore failed twice, the shared checkout is left sitting
        # on a stray `selfevo/cycle-<id>` branch, and this (next) invocation would
        # otherwise proceed straight into the already_done bookkeeping check below
        # on that stray branch — a commit would land on it and be silently
        # discarded the moment _setup_cycle_branch() does its own
        # `checkout -B ... origin/main`. Re-run _restore_to_main defensively here,
        # before any git work happens, and hard-abort the cycle (no subagent
        # spawn) if it still can't repair the checkout. A missing repo (not yet
        # cloned) is not a stray-branch condition — leave that to
        # _setup_cycle_branch's existing 'repo_missing' handling below.
        # #733: this check verifies repo state, not the request, so it only
        # needs to run once per bridge run — subsequent skip iterations reuse
        # the already-verified state (no skip branch below moves HEAD off main).
        _selfevo_repo_check = STATE_DIR.parent / 'eeebot-self-evolving'
        if not _precondition_checked:
            _precondition_checked = True
            if _selfevo_repo_check.is_dir() and not _restore_to_main(_selfevo_repo_check):
                print(
                    f'bridge: HEAD-on-main precondition failed for {_selfevo_repo_check}; '
                    'aborting cycle (blocked), no subagent spawned'
                )
                handled_marker.write_text(str(req_path), encoding='utf-8')
                _write_bridge_completed_result(
                    state_dir=STATE_DIR,
                    req=req,
                    request_id=request_id,
                    cycle_id=req.get('cycle_id') or '',
                    goal_id=goal_id,
                    files_changed=[],
                    commits_pushed=0,
                    result_status='blocked',
                    backlog_title='',
                    key_learnings=[
                        'HEAD-on-main precondition failed: the shared eeebot-self-evolving '
                        'checkout could not be restored to main (both `checkout main` and '
                        '`checkout -B main origin/main` failed). Aborting cycle without '
                        'spawning a subagent to avoid running bookkeeping on a stray branch.',
                    ],
                    rollback={
                        'integrated': False,
                        'cycle_branch': None,
                        'main_sha_before': None,
                        'main_sha_after': None,
                        'reason': 'head_on_main_precondition_failed',
                        'auto_committed': False,
                    },
                )
                _v, _vr = _derive_cycle_verdict('failed', 'head_on_main_precondition_failed')
                record_cycle_outcome(
                    STATE_DIR, _cycle_id, 'failed', 'head_on_main_precondition_failed', [], None,
                    verdict=_v, verdict_reason=_vr,
                )
                # #721: no cycle branch exists yet on this path — tag at current HEAD.
                _tag_cycle_post(_selfevo_repo_check, _cycle_id, 'failed')
                return 0

        # #1001: pick up any curator-staged fact promotions at the safe cycle-start
        # boundary — bridge lock held, HEAD on clean main. Fail-open: a pickup
        # failure is printed and the cycle proceeds normally.
        if _selfevo_repo_check.is_dir():
            _pickup_staged_promotions(_selfevo_repo_check, STATE_DIR)

        # #944: read executor mission from immutable goals.md at the release
        # root when available; fall back to the legacy goal_text.json chain.
        # Derived priorities (derived_priorities.json) are always folded in.
        try:
            from nanobot.runtime.goal_review import merged_goal_text
            _charter = read_charter_text(RELEASE_ROOT)
        except Exception:
            _charter = ''
        if _charter:
            _base_goal_text = _charter
        else:
            _base_goal_text = (
                # Prefer goal_text.json in state dir
                (load_json(STATE_DIR / 'goals' / 'goal_text.json') or {}).get('text')
                # Fallback: read from release root (deployed with release)
                or (load_json(RELEASE_ROOT / 'host' / 'eeepc' / 'etc' / 'goal_text.json') or {}).get('text')
                or goal_id
            )
        try:
            from nanobot.runtime.goal_review import merged_goal_text
            goal_text = merged_goal_text(STATE_DIR, _base_goal_text)
        except Exception:
            goal_text = _base_goal_text
        # #712: strip completed "Current priority target" entries (per the #575
        # git-log done-detection heuristic) before this raw text is injected
        # verbatim into the subagent prompt below — otherwise a priority the
        # coordinator already treats as done keeps being shown/re-proposed every
        # cycle (novelty collapse, per the #711 shadow run).
        # #773: state_dir enables the completed-demand sidecar check — the
        # ledger-chain done-truth that text evidence cannot provide for
        # demand-mode integrations (refined titles carry no verbatim label).
        goal_text = filter_completed_priorities_from_goal_text(
            goal_text, _selfevo_repo_check, state_dir=STATE_DIR
        )
        # #1222: the coordinator's per-goal subagent_policy (preferred_profile /
        # budget_class in goals/registry.json) went with the coordinator; the
        # live registry never carried the key, so this was always the default.
        profile = FORCE_PROFILE or req.get('profile') or 'bounded_execution'
        budget_class = FORCE_BUDGET or req.get('budget') or 'standard'
        gate_open = approval_open()
        mode_at_start = 'auto' if gate_open else 'strict'

        resolved_iterations = resolve_max_tool_iterations(config.agents.defaults.max_tool_iterations)
        task_goal_text = goal_text
        if _charter:
            marker = 'Current priority targets:'
            task_goal_text = goal_text[goal_text.find(marker):] if marker in goal_text else ''
        task = build_task(
            req, task_goal_text, report_source, state_dir=STATE_DIR,
            selfevo_repo_root=_selfevo_repo_check,
            max_iterations=resolved_iterations,
            charter_in_system=bool(_charter),
        )

        # Extract backlog title for MEMORY.md safety-net update after execution
        _source_artifact_path = req.get('source_artifact') or ''
        _artifact_data: dict = {}
        if _source_artifact_path and Path(_source_artifact_path).exists():
            try:
                _artifact_data = json.loads(Path(_source_artifact_path).read_text(encoding='utf-8'))
            except Exception:
                pass
        backlog_title: str = _artifact_data.get('next_bounded_candidate', {}).get('title', '')

        # #721: tag-first dedup — checked BEFORE the fuzzy keyword heuristic below.
        # An exact `cycle-<id>-success` tag means THIS cycle_id already completed
        # successfully (e.g. a retried/replayed request) — a structured, exact
        # match, unlike the keyword heuristic. Same-title-but-different-cycle_id
        # duplicates are NOT caught here (there is no tag for a different
        # cycle_id) — those still rely on _task_already_done's keyword heuristic
        # below, which stays exactly as it was as the semantic-dup fallback.
        _cycle_success_tag = f'cycle-{_safe_ref_id(_cycle_id)}-success'
        if _cycle_tag_exists(_selfevo_repo_check, _cycle_success_tag):
            print(f'bridge: cycle {_cycle_id} already tagged {_cycle_success_tag}; skipping subagent spawn')
            handled_marker.write_text(str(req_path), encoding='utf-8')
            _write_bridge_completed_result(
                state_dir=STATE_DIR,
                req=req,
                request_id=request_id,
                cycle_id=req.get('cycle_id') or '',
                goal_id=goal_id,
                files_changed=[],
                commits_pushed=0,
                result_status='already_done',
                backlog_title=backlog_title,
                key_learnings=[
                    f'Cycle {_cycle_id} already carries a success tag ({_cycle_success_tag}) — an '
                    'exact retry/replay of a completed cycle. Skipped without spawning a subagent.',
                ],
            )
            record_dedup_decision(STATE_DIR, _cycle_id, 'skipped_duplicate', f'tag:{_cycle_success_tag}')
            _v, _vr = _derive_cycle_verdict('skipped-duplicate', 'already_done_tag')
            record_cycle_outcome(
                STATE_DIR, _cycle_id, 'skipped-duplicate', 'already_done_tag', [], None,
                verdict=_v, verdict_reason=_vr,
            )
            _tag_cycle_post(_selfevo_repo_check, _cycle_id, 'skipped-duplicate')
            # #733: bulk-skip — bookkeeping done for this duplicate; move on to
            # the next pending request in the same run (bounded by MAX_SKIPS_PER_RUN).
            _skips += 1
            if _skips >= MAX_SKIPS_PER_RUN:
                _maybe_propose_after_skip(_selfevo_repo_check)
                return 0
            continue

        # Before spawning: detect if task is already done in recent git commits.
        # If yes, mark Done in MEMORY.md, write result, and exit without spawning.
        # (_selfevo_repo_check was already resolved above for the #680 HEAD-on-main
        # precondition check.)
        # #713: the coordinator-derived backlog_title is not the only source of a
        # duplicate task — an arbitrary request can carry its own task_title (or
        # semantic_task_id) that never flows through backlog_title at all, which
        # is exactly the #711 bypass that let duplicate proposals reach full
        # subagent spawn. _duplicate_check_title widens the gate to those fields
        # WITHOUT changing _task_already_done itself or the bookkeeping identity
        # (backlog_title) used below.
        _dup_check_title = _duplicate_check_title(req, backlog_title)
        # #736: LLM-proposed requests always carry a `Target path: <path>`
        # line in their task text. The plain keyword heuristic above matches
        # against the WHOLE 7-day git log, which becomes saturated with
        # overlapping words as history accumulates (self-worsening false
        # positives — see #736 live evidence). If the request names a target
        # path and that file does NOT exist in the instance repo, the task
        # cannot possibly be already done — skip the keyword heuristic
        # entirely. If the target path exists, scope the keyword heuristic to
        # commits that actually touched it (more precise than the whole log).
        # Any extraction/lookup error falls open to the pre-#736 behavior
        # (plain _task_already_done over the whole log).
        _already_done = False
        try:
            _target_path = _extract_target_path(req)
        except Exception:
            _target_path = None
        # #760 follow-up (live 2026-07-15 20:42Z): demand-vetted requests
        # ('serves: demand <id>') were already judged not-done by the demand
        # collector's strong filter (#748/#769 label evidence + extend
        # carve-out); the word heuristics below are strictly weaker and
        # falsely killed the P14 proposal (its title shared 4 words with a
        # P11 commit touching the same dashboard file). Single source of
        # done-truth: skip already_done entirely for such requests — the
        # existence-index and recent-failure gates below still apply.
        try:
            _serves_demand = _request_serves_demand(req)
        except Exception:
            _serves_demand = False
        if _serves_demand:
            pass
        elif _dup_check_title and _target_path:
            try:
                _target_exists = (_selfevo_repo_check / _target_path).exists()
            except Exception:
                _target_exists = None
            if _target_exists is False:
                _already_done = False
            elif _target_exists is True:
                _already_done = _task_already_done_for_path(
                    _dup_check_title, _selfevo_repo_check, _target_path,
                )
            else:
                # Fail-open: couldn't resolve target_exists — fall back.
                _already_done = _task_already_done(_dup_check_title, _selfevo_repo_check)
        elif _dup_check_title:
            _already_done = _task_already_done(_dup_check_title, _selfevo_repo_check)
        if _already_done:
            import subprocess as _sp_check
            _git_chk = ['git', '-c', f'safe.directory={_selfevo_repo_check}',
                        '-C', str(_selfevo_repo_check)]
            _log_r = _sp_check.run(
                _git_chk + ['log', '--since=14 days ago', '--oneline', '--grep',
                            _dup_check_title[:40]],
                capture_output=True, text=True,
            )
            _found_commit = _log_r.stdout.strip().splitlines()[0] if _log_r.stdout.strip() else 'recent commit'
            print(f'bridge: task already done (found in git: {_found_commit[:80]}); skipping subagent spawn')
            # Mark [Done] in MEMORY.md (only meaningful when we have the original,
            # coordinator-derived backlog_title — _try_mark_backlog_done is a
            # no-op for an empty title, the correct fail-open behavior for a bare
            # task_title/semantic_task_id request with no backlog entry).
            if _selfevo_repo_check.is_dir():
                _try_mark_backlog_done(
                    repo_root=_selfevo_repo_check,
                    backlog_title=backlog_title,
                    what_was_done=f'task detected as already done via git log: {_found_commit[:60]}',
                )
                # #678 F5: this path runs BEFORE _setup_cycle_branch, with NO smoke
                # gate at all, on most cycles. Previously it was a bare
                # `git push origin main` — constrain it to only push when the
                # resulting diff is pure MEMORY.md bookkeeping.
                if _diff_against_remote_touches_only(
                    _selfevo_repo_check, 'origin/main', {'memory/MEMORY.md'},
                ):
                    _sp_check.run(
                        _git_chk + ['push', 'origin', 'main'],
                        capture_output=True,
                    )
                else:
                    print(
                        'bridge: already_done bookkeeping touched more than memory/MEMORY.md '
                        'or nothing to push — skipping ungated push (#678 F5)'
                    )
            handled_marker.write_text(str(req_path), encoding='utf-8')
            _write_bridge_completed_result(
                state_dir=STATE_DIR,
                req=req,
                request_id=request_id,
                cycle_id=req.get('cycle_id') or '',
                goal_id=goal_id,
                files_changed=[],
                commits_pushed=0,
                result_status='already_done',
                backlog_title=backlog_title,
                key_learnings=[
                    f'Task "{_dup_check_title[:60]}" was already completed in git: {_found_commit[:60]}. '
                    'Marked [Done] in MEMORY.md. No re-execution needed.',
                ],
            )
            record_dedup_decision(STATE_DIR, _cycle_id, 'skipped_duplicate', _found_commit)
            _v, _vr = _derive_cycle_verdict('skipped-duplicate', 'already_done')
            record_cycle_outcome(
                STATE_DIR, _cycle_id, 'skipped-duplicate', 'already_done', [], None,
                verdict=_v, verdict_reason=_vr,
            )
            # #721: no cycle branch on this path — tag at current HEAD.
            _tag_cycle_post(_selfevo_repo_check, _cycle_id, 'skipped-duplicate')
            # #733: bulk-skip — bookkeeping done for this duplicate; move on to
            # the next pending request in the same run (bounded by MAX_SKIPS_PER_RUN).
            # The proposer hook (#707: a queue full of stale duplicates is novelty
            # exhaustion too) fires once, only when the loop actually ends (cap
            # reached below, or the queue empties via the no-request branch above)
            # — not on every individual skip.
            _skips += 1
            if _skips >= MAX_SKIPS_PER_RUN:
                _maybe_propose_after_skip(_selfevo_repo_check)
                return 0
            continue
        # #716: _task_already_done above only catches proposals that already landed
        # as a real git commit. A proposal that was blocked/rolled-back/produced no
        # commit is not in git log at all, so it can be re-proposed and re-spawned
        # every cycle (Gemini MVP M2==M3 repeat). _recent_failure_match is a
        # SEPARATE, narrower, bounded-recency (default 24h) check over recent
        # bridge results — it does not affect the already_done bookkeeping/[Done]
        # marking above, only adds this additional pre-spawn suppression.
        # (The proposer-context half of #716 — showing the subagent what was
        # recently rejected — is already covered by #713's _recent_activity_context,
        # wired into build_task() above; this only adds pre-spawn enforcement.)
        elif _dup_check_title and (
            _recent_failure_title := _recent_failure_match(
                _dup_check_title, STATE_DIR, target_path=_target_path,
            )
        ):
            print(
                f'bridge: task "{_dup_check_title[:60]}" matches recent failure/rejection '
                f'"{_recent_failure_title[:60]}" (within {FAILURE_SUPPRESS_HOURS}h); '
                'skipping subagent spawn'
            )
            handled_marker.write_text(str(req_path), encoding='utf-8')
            _write_bridge_completed_result(
                state_dir=STATE_DIR,
                req=req,
                request_id=request_id,
                cycle_id=req.get('cycle_id') or '',
                goal_id=goal_id,
                files_changed=[],
                commits_pushed=0,
                result_status='blocked',
                backlog_title=backlog_title,
                key_learnings=[
                    f'Task "{_dup_check_title[:60]}" matches recently-failed/rejected proposal '
                    f'"{_recent_failure_title[:60]}" (within {FAILURE_SUPPRESS_HOURS}h); suppressed '
                    'to avoid re-spawning the same rejected work. Not marked [Done] — this is a '
                    'suppression, not completion.',
                ],
                rollback={
                    'integrated': False,
                    'reason': 'recent_duplicate_failure',
                },
            )
            # #757: record the matched HISTORICAL title, not the proposal's
            # own title — matched_against must say what it actually matched.
            record_dedup_decision(STATE_DIR, _cycle_id, 'skipped_recent_failure', _recent_failure_title)
            _v, _vr = _derive_cycle_verdict('skipped-duplicate', 'recent_duplicate_failure')
            record_cycle_outcome(
                STATE_DIR, _cycle_id, 'skipped-duplicate', 'recent_duplicate_failure', [], None,
                verdict=_v, verdict_reason=_vr,
            )
            # #721: no cycle branch on this path — tag at current HEAD.
            _tag_cycle_post(_selfevo_repo_check, _cycle_id, 'skipped-duplicate')
            # #733: bulk-skip — bookkeeping done for this duplicate; move on to
            # the next pending request in the same run (bounded by MAX_SKIPS_PER_RUN).
            _skips += 1
            if _skips >= MAX_SKIPS_PER_RUN:
                _maybe_propose_after_skip(_selfevo_repo_check)
                return 0
            continue
        # #750: the two checks above are exact/keyword title matching — they
        # miss SEMANTIC near-duplicates whose title shares no literal words
        # with the past commit/failure (e.g. "monitor RAM and memory usage"
        # vs. an existing track_memory.py — the overnight #750 evidence).
        # find_duplicate_script incrementally reindexes a local FTS5
        # existence index (scripts + past titles + hypotheses) and flags a
        # duplicate-suspect SCRIPT hit via word-overlap over the FTS
        # candidates. Fail-open by construction (see
        # nanobot.runtime.existence_index) — any internal error yields None,
        # so this branch is a pure ADDITION to the dedup gate, never a new
        # way to block a legitimately novel proposal.
        elif _dup_check_title and (
            _existence_match := find_duplicate_script(
                STATE_DIR, _selfevo_repo_check, _dup_check_title, _target_path,
            )
        ):
            print(
                f'bridge: task "{_dup_check_title[:60]}" is a semantic near-duplicate of '
                f'existing {_existence_match} (existence index); skipping subagent spawn'
            )
            handled_marker.write_text(str(req_path), encoding='utf-8')
            _write_bridge_completed_result(
                state_dir=STATE_DIR,
                req=req,
                request_id=request_id,
                cycle_id=req.get('cycle_id') or '',
                goal_id=goal_id,
                files_changed=[],
                commits_pushed=0,
                result_status='blocked',
                backlog_title=backlog_title,
                key_learnings=[
                    f'Task "{_dup_check_title[:60]}" matched an existing artifact '
                    f'({_existence_match}) via the #750 existence index (semantic '
                    'word-overlap, not exact title match); suppressed to avoid shipping '
                    'a near-duplicate script. Not marked [Done] — this is a suppression, '
                    'not completion.',
                ],
                rollback={
                    'integrated': False,
                    'reason': 'existence_index_duplicate',
                },
            )
            record_dedup_decision(
                STATE_DIR, _cycle_id, 'skipped_duplicate', f'existence-index:{_existence_match}',
            )
            _v, _vr = _derive_cycle_verdict('skipped-duplicate', 'existence_index_duplicate')
            record_cycle_outcome(
                STATE_DIR, _cycle_id, 'skipped-duplicate', 'existence_index_duplicate', [], None,
                verdict=_v, verdict_reason=_vr,
            )
            # #721: no cycle branch on this path — tag at current HEAD.
            _tag_cycle_post(_selfevo_repo_check, _cycle_id, 'skipped-duplicate')
            # #733: bulk-skip — bookkeeping done for this duplicate; move on to
            # the next pending request in the same run (bounded by MAX_SKIPS_PER_RUN).
            _skips += 1
            if _skips >= MAX_SKIPS_PER_RUN:
                _maybe_propose_after_skip(_selfevo_repo_check)
                return 0
            continue

        else:
            # #720 piece 4: neither pre-spawn suppression fired — the dedup
            # heuristic's own "proceeded" decision, so #705 can measure the
            # heuristic's false-positive rate (matched vs. proceeded counts).
            record_dedup_decision(STATE_DIR, _cycle_id, 'proceeded', None)

        # #733: non-duplicate request found — exit the bulk-skip loop and fall
        # through to the normal single-spawn path below (still exactly one
        # _setup_cycle_branch/spawn per run, S6 unchanged).
        break

    # One-time migration: backfill backlog_title into existing result files
    # so _get_previous_attempts() can match by artifact title.
    _results_dir_mig = STATE_DIR / 'subagents' / 'results'
    _mig_count = _migrate_backlog_title_in_results(_results_dir_mig)
    if _mig_count:
        print(f'migration: backfilled backlog_title in {_mig_count} result file(s)')

    # #1072: repair existing parent links in tree.json using git ancestry
    try:
        from nanobot.runtime.evolution_tree import migrate_tree_ancestry
        _selfevo_repo_mig = STATE_DIR.parent / 'eeebot-self-evolving'
        _tree_mig = migrate_tree_ancestry(STATE_DIR, repo_root=_selfevo_repo_mig)
        if _tree_mig.get("repaired", 0) > 0:
            print(f'migration: repaired {_tree_mig["repaired"]} parent link(s) in evolution tree')
    except Exception:
        pass

    bridge_model = resolve_model('executor', config_fallback=config.tools.subagent.model)
    if _request_serves_demand(req):
        demand_id = req.get('task', '').split('Serves: demand ', 1)[-1].splitlines()[0].strip()
        cycle_id = str(req.get('cycle_id') or request_id)
        marker = demand._escalation_marker(STATE_DIR, demand_id)
        if marker and marker.get('cycle_id') == cycle_id:
            bridge_model = str(marker.get('model') or bridge_model)
    config.agents.defaults.model = bridge_model
    provider = _make_provider(config)
    bus = MessageBus()
    TARGET_WORKSPACE.mkdir(parents=True, exist_ok=True)
    (TARGET_WORKSPACE / '.nanobot' / 'subagents').mkdir(parents=True, exist_ok=True)

    # ── Cycle-branch isolation (R8/R9) ───────────────────────────────────────
    # Every cycle runs on its own selfevo/cycle-<id> branch off origin/main, so
    # the subagent (or an errant self-push) can only ever publish that branch —
    # origin/main advances only via _integrate_cycle_to_main() below, and only
    # after the smoke gate passes (R12-R15).
    async def _evaluate_candidate(cand_cycle_id: str, do_integration: bool, meas_metric: str = "") -> dict:
        _cycle_id = cand_cycle_id
        _selfevo_repo = STATE_DIR.parent / 'eeebot-self-evolving'
        # _cycle_id was already resolved up front (right after request_id, before
        # the write-ahead ledger marker) — reused here unchanged.
        _cycle_setup = _setup_cycle_branch(_selfevo_repo, _cycle_id, STATE_DIR)
        cycle_branch = _cycle_setup['branch']
        main_sha_before = _cycle_setup['main_sha']
        # #877: the real, unswitched origin/main sha observed at setup time —
        # distinct from main_sha_before whenever a line switch occurred. Used
        # for out-of-band-drift detection (#846) and as the integration push's
        # force-with-lease value; falls back to main_sha_before for a
        # setup-failure dict that never reached the rev-parse (both are '').
        _origin_main_observed = _cycle_setup.get('origin_main_sha') or main_sha_before

        if not _cycle_setup['ok']:
            print(f"cycle-branch setup failed ({_cycle_setup['reason']}); recording blocked result, no subagent spawned")
            _restore_to_main(_selfevo_repo)
            handled_marker.write_text(str(req_path), encoding='utf-8')
            _write_bridge_completed_result(
                state_dir=STATE_DIR,
                req=req,
                request_id=request_id,
                cycle_id=req.get('cycle_id') or '',
                goal_id=goal_id,
                files_changed=[],
                commits_pushed=0,
                result_status='blocked',
                backlog_title=backlog_title,
                key_learnings=[
                    f"Cycle-branch setup failed ({_cycle_setup['reason']}); "
                    'the eeebot-self-evolving checkout was left untouched, no subagent was spawned.',
                ],
                rollback={
                    'integrated': False,
                    'cycle_branch': cycle_branch,
                    'main_sha_before': main_sha_before,
                    'main_sha_after': main_sha_before,
                    'reason': _cycle_setup['reason'],
                },
            )
            _v, _vr = _derive_cycle_verdict('failed', _cycle_setup['reason'])
            record_cycle_outcome(
                STATE_DIR, _cycle_id, 'failed', _cycle_setup['reason'], [], cycle_branch,
                verdict=_v, verdict_reason=_vr,
            )
            # #721: cycle branch setup itself failed — tag at main_sha_before
            # (may be '' if even the pre-checkout rev-parse failed; _tag_cycle_post
            # falls back to current HEAD in that case).
            _tag_cycle_post(_selfevo_repo, _cycle_id, 'failed', main_sha_before)
            return {'status': 0}

        # #721: pre-cycle tag at main_sha_before, right after cycle-branch setup
        # succeeds — the pre half of the pre/post bracket (see _tag_cycle_pre).
        _tag_cycle_pre(_selfevo_repo, _cycle_id, main_sha_before)

        # #718: the subagent must write into the git checkout the bridge branches,
        # commits, gates, and integrates (_selfevo_repo) — not TARGET_WORKSPACE
        # (the deployed release tree in prod, which is not a git repo and is never
        # synced from _selfevo_repo). Constructed here, after _selfevo_repo is
        # defined and validated by _cycle_setup['ok'] above, so the subagent lands
        # on the checked-out cycle branch. restrict_to_workspace=False already
        # leaves no fencing behavior to change.
        charter_text = read_charter_text(RELEASE_ROOT)
        identity_path = RELEASE_ROOT / 'IDENTITY.md'
        identity_text = identity_path.read_text(encoding='utf-8').strip() if identity_path.is_file() else ''
        # #939 Part E: builtins irrelevant to the self-evolving loop are excluded
        # from the subagent skills summary to reduce context noise.  The list is
        # closed here (bridge-side, not instance-controlled) — instance code cannot
        # widen or override it.
        # #958 Part B: add cron, summarize, github (never used by the loop).
        _LOOP_EXCLUDED_SKILLS = ["weather", "tmux", "clawhub", "cron", "summarize", "github"]
        mgr = SubagentManager(
            provider=provider,
            workspace=_selfevo_repo,
            bus=bus,
            model=config.agents.defaults.model,
            web_search_config=config.tools.web.search,
            web_proxy=config.tools.web.proxy,
            exec_config=config.tools.exec,
            subagent_config=config.tools.subagent,
            restrict_to_workspace=False,
            max_running=config.tools.subagent.max_running,
            # Issue #578: reuse the same cap as the main agent (agents.defaults.maxToolIterations)
            # instead of the SubagentManager default of 15 — one consistent value end-to-end.
            # Issue #906: SELFEVO_MAX_TOOL_ITERATIONS (operator preset knob) overrides the
            # config value when set to a valid positive int; fail-open to config otherwise.
            max_iterations=resolved_iterations,
            system_context=(
                "# Immutable operator charter\n\n" + charter_text
                + ("\n\n# Loop agent identity\n\n" + identity_text if identity_text else "")
                if charter_text
                else ("# Loop agent identity\n\n" + identity_text if identity_text else "")
            ),
            # #939 Part C: skill-fitness instrumentation context.  The bridge
            # supplies repo + cycle context so skill_fitness.py can resolve the
            # last-edit commit of each SKILL.md and apply the birth-use guard.
            skill_fitness_state_dir=STATE_DIR,
            skill_fitness_repo=_selfevo_repo,
            skill_fitness_cycle_id=_cycle_id,
            skill_fitness_cycle_base_sha=main_sha_before,
            # #939 Part E: suppress loop-irrelevant builtin skills.
            excluded_skill_names=_LOOP_EXCLUDED_SKILLS,
        )

        # Capture HEAD SHA before spawn so we can count subagent commits correctly,
        # even when the subagent pushes itself (harmless under isolation: the
        # checkout sits on the cycle branch, so a bare self-push can only publish
        # that branch, never origin/main).
        _pre_spawn_sha_file = STATE_DIR / 'bridge_pre_spawn.sha'
        _pre_spawn_sha = _capture_pre_spawn_sha(_selfevo_repo, _pre_spawn_sha_file)

        # #678 F2: baseline test count at origin/main, captured via git blobs (no
        # checkout needed — the shared checkout already moved to the cycle branch
        # above). Used by _run_smoke_tests_with_shrink_guard to fail the gate if the
        # cycle's tree collects fewer tests than main had before this cycle.
        _baseline_test_count = _count_tests_at_ref(_selfevo_repo, 'origin/main')
        # #846: baseline test FUNCTION NAMES at the same ref — see
        # _run_smoke_tests_with_shrink_guard for why count alone is insufficient.
        _baseline_test_names = _test_function_names_at_ref(_selfevo_repo, 'origin/main')

        import subprocess as _sp
        files_changed: list[str] = []
        cycle_commit_count = 0
        commits_pushed = 0
        _auto_committed = False
        _cycle_tier = 'script'  # #812: 'script' | 'runtime' (set by surface classify below)
        _integrated = False
        _rollback_reason: 'str | None' = None
        # #678 F1/F3: mutation-surface / blocked-pattern violations across ALL cycle
        # commits (not just the auto-commit fallback) — populated below once
        # files_changed is known, enforced as a hard block in the gate decision.
        _mutation_violations: 'list[str]' = []
        _blocked_pattern_violations: 'list[str]' = []
        # #1119: deterministic test-weakening detector state — recomputed at the
        # same points as the mutation-surface violations above (initial commit,
        # every repair retry, final pre-gate recompute).
        _test_weakening_blocked = False
        _test_weakening_hard: 'list[str]' = []
        _test_weakening_soft: 'list[str]' = []
        main_sha_after = main_sha_before
        _repair_attempts = 0
        _smoke_passed = True
        _smoke_ran = False
        try:
            _max_repair_attempts = int(
                os.environ.get('SUBAGENT_BRIDGE_MAX_REVISIONS', str(REVISION_CAP_DEFAULT))
            )
        except ValueError:
            _max_repair_attempts = REVISION_CAP_DEFAULT
        _max_repair_attempts = max(0, _max_repair_attempts)

        # #789: names of fitness sidecars changed during the spawn window (empty =
        # clean). Populated by the pre/post hash compare below.
        _integrity_changed: 'list[str]' = []
        try:
            # #789: hash the fitness sidecars IMMEDIATELY before the spawn — every
            # bridge-own sidecar write (demand fold, exhaustion updates, scorecard
            # recompute on the proposer path) has already happened above, so any
            # post-spawn mismatch is attributable to code run inside the window.
            _integrity_pre = _fitness_sidecar_hashes(STATE_DIR)
            # #955/#966: dump the ACTUAL assembled system prompt (ContextBuilder
            # output + system_context) so the dump faithfully matches what the
            # executor receives — AGENTS.md, charter, and identity all included.
            # Small test doubles from older bridge tests do not implement the
            # private builder; keep their fail-open diagnostic behavior without
            # weakening the production path.
            _build_prompt = getattr(mgr, '_build_subagent_prompt', None)
            _dump_system = (
                _build_prompt()
                if callable(_build_prompt)
                else ('# Immutable operator charter\n\n' + charter_text if charter_text else '')
            )
            dump_spawn_prompts(STATE_DIR, _cycle_id, _dump_system, task)
            msg = await mgr.spawn(
                task=task,
                label=f'selfevo-{goal_id[:8]}',
                origin_channel='system',
                origin_chat_id='self-evolving-agent',
                profile=profile,
                mode_at_start=mode_at_start,
                approval_gate_open=gate_open,
                budget_class=budget_class,
                escalate_on_budget=True,
            )
            print(msg)
            # #1118: capture the fresh task_id NOW, while it is still a live key
            # in mgr._running_tasks (spawn's own done-callback pops it the moment
            # the background task finishes) — this is the only handle bridge.py
            # has back to the telemetry file the subagent will write its raw
            # final answer into (see _executor_reported_outcome below).
            _subagent_task_id = next(iter(mgr._running_tasks), None)
            if mgr._running_tasks:
                try:
                    # Limit subagent execution time to 3000s (50 minutes).
                    # Coordinator stale threshold is 3600s (60 minutes).
                    # This ensures the subagent terminates gracefully before coordinator marks it stale.
                    await asyncio.wait_for(
                        asyncio.gather(*list(mgr._running_tasks.values()), return_exceptions=True),
                        timeout=3000.0
                    )
                except asyncio.TimeoutError:
                    print("Subagent execution timed out (limit: 3000s). Cancelling running tasks...")
                    for task_obj in list(mgr._running_tasks.values()):
                        task_obj.cancel()
                    # Allow tasks to process CancelledError and write telemetry
                    await asyncio.gather(*list(mgr._running_tasks.values()), return_exceptions=True)
                    print("All timed-out subagent tasks cancelled.")

            # #1280: the handled_ marker is no longer written here unconditionally
            # — see _decide_handled_marker below, after the cycle's commits are
            # counted, so a request whose subagent died on the LLM call is
            # re-offered (bounded) instead of retired with nothing done.
            _executor_llm_error_text = _executor_llm_error(STATE_DIR, _subagent_task_id)
            latest = TARGET_WORKSPACE / '.nanobot' / 'subagents' / 'latest.json'
            print(latest)

            # Count commits the subagent made on the cycle branch (since pre-spawn
            # SHA). No push here — origin/main only advances once the gate passes.
            if _selfevo_repo.is_dir():
                _git_se = _git_cmd(_selfevo_repo)
                _new_commits = _count_commits_since(_selfevo_repo, _pre_spawn_sha)
                if _new_commits == 0:
                    print(f'cycle-branch: no new commits on {cycle_branch}')
                # Safety net (#666, unconditional since #717): the subagent may have
                # implemented real changes via edit_file/write_file but finished the
                # turn without running git commit — OR it may have made a real commit
                # and STILL left a new file untracked (e.g. only `git add`-ing some
                # paths). Previously this call was gated behind `_new_commits == 0`,
                # so the latter case skipped auto-commit entirely and the
                # finally-block _restore_to_main() (`git reset --hard && git clean
                # -fd`) discarded the untracked new file outright — greenfield new
                # files from a subagent could never integrate. Call unconditionally;
                # _auto_commit_uncommitted_work() is a no-op (via its own `git status
                # --porcelain` check) on an already-clean tree, so this is safe.
                _auto = _auto_commit_uncommitted_work(
                    _selfevo_repo,
                    cycle_branch,
                    backlog_title=backlog_title,
                    task_snippet=req.get('task_title') or request_id,
                )
                if _auto['excluded']:
                    print(
                        f"auto-commit: excluded {len(_auto['excluded'])} blocked-pattern file(s): "
                        f"{', '.join(_auto['excluded'][:5])}"
                    )
                if _auto['committed']:
                    _auto_committed = True
                    _new_commits = _count_commits_since(_selfevo_repo, _pre_spawn_sha)
                    print(
                        f"auto-commit: {_auto['files_committed']} file(s) committed on "
                        f'{cycle_branch} (#666)'
                    )
                if _new_commits > 0:
                    cycle_commit_count = _new_commits
                    # #678 F1/F3: initial changed-file set + violation split, for
                    # logging. This is RECOMPUTED after the repair loop (just before
                    # the gate decision) so the enforced lists reflect every commit,
                    # not only the first — see the recompute below.
                    files_changed, _blocked_pattern_violations, _mutation_violations, _cycle_tier = (
                        _changed_files_and_violations(_selfevo_repo, _pre_spawn_sha)
                    )
                    _violations = _blocked_pattern_violations + _mutation_violations
                    if _violations:
                        print(f'mutation surfaces: {len(_violations)} violation(s):')
                        for v in _violations:
                            print(f'  ! {v}')
                    else:
                        print(f'mutation surfaces: clean ({len(files_changed)} file(s) changed)')
                    print(f'cycle-branch: {cycle_commit_count} new commit(s) on {cycle_branch}')
                    # #1119: test-weakening check, same recompute point as the
                    # mutation-surface classification above.
                    _test_weakening_blocked, _test_weakening_hard, _test_weakening_soft = (
                        _check_test_weakening(_selfevo_repo, _pre_spawn_sha)
                    )
                    if _test_weakening_hard:
                        print(f'test-weakening: {len(_test_weakening_hard)} hard signal(s):')
                        for v in _test_weakening_hard:
                            print(f'  ! {v}')
                    if _test_weakening_soft:
                        print(f'test-weakening: {len(_test_weakening_soft)} soft signal(s) recorded (not blocking):')
                        for v in _test_weakening_soft:
                            print(f'  ~ {v}')
            else:
                print(f'cycle-branch: eeebot-self-evolving not found at {_selfevo_repo}')

            # #1280: decide the request's fate now that we know whether the
            # subagent produced anything. A subagent whose LLM call died
            # (`status: error`, "LLM execution failed") with NO commits is a
            # failed cycle: reason `executor_llm_error`, re-offered up to
            # LLM_ERROR_MAX_RETRIES times before the marker retires it. If the
            # subagent had already edited files before the call died, the
            # auto-commit above captured real work and the normal gate decides
            # (cycle 3cbcc1f77d25 on 2026-09-04 integrated exactly that way);
            # the error text still rides along in the result's learnings.
            if _executor_llm_error_text and cycle_commit_count == 0:
                _rollback_reason = _rollback_reason or 'executor_llm_error'
            _decide_handled_marker(
                handled_marker, req_path,
                llm_error=bool(_executor_llm_error_text and cycle_commit_count == 0),
            )

            # ── Closed-loop repair cycle (issue #526) ────────────────────────────
            # After the first commit, run smoke tests. If they fail, spawn a repair
            # subagent with the traceback injected, retrying up to the revision cap.
            # Repairs commit to the SAME cycle branch — the gate (and any resulting
            # integration to main) happens exactly once, after this loop ends.
            # Inspired by Darwin Mode LEARNINGS.md §1: closed-loop repair → 2× improvement.
            # R12: bounded revisions — cap configurable (default 3), never unbounded.
            if cycle_commit_count > 0 and _selfevo_repo.is_dir():
                _smoke_ran = True
                # #686: files_changed here reflects the initial commit(s) computed
                # above (lines ~1199-1218) — the bounded gate selects tests from it.
                _smoke_passed, _smoke_output = _run_smoke_tests_with_shrink_guard(
                    _selfevo_repo, _baseline_test_count, changed_files=files_changed,
                    baseline_test_names=_baseline_test_names,
                )
                print(f'smoke: {"PASS" if _smoke_passed else "FAIL"}')
                while not _smoke_passed and _repair_attempts < _max_repair_attempts:
                    _repair_attempts += 1
                    print(f'smoke: FAIL — spawning repair turn {_repair_attempts}/{_max_repair_attempts}')
                    # Build repair prompt with traceback injected
                    _repair_prompt = build_task(
                        req, goal_text, report_source,
                        state_dir=STATE_DIR,
                        repair_context=_smoke_output,
                    )
                    # Spawn repair subagent
                    from nanobot.agent.subagent import SubagentManager as _SM2
                    _repair_cfg = config
                    _repair_provider = _make_provider(_repair_cfg)
                    _repair_mgr = _SM2(
                        provider=_repair_provider,
                        workspace=_selfevo_repo,  # #718: repair turn also writes to the committed repo
                        bus=bus,
                        model=_repair_cfg.agents.defaults.model,
                        web_search_config=_repair_cfg.tools.web.search,
                        web_proxy=_repair_cfg.tools.web.proxy,
                        exec_config=_repair_cfg.tools.exec,
                        subagent_config=_repair_cfg.tools.subagent,
                        restrict_to_workspace=False,
                        max_running=_repair_cfg.tools.subagent.max_running,
                        # #906: same operator-preset override as the main spawn above.
                        max_iterations=resolve_max_tool_iterations(_repair_cfg.agents.defaults.max_tool_iterations),
                        system_context=(
                            "# Immutable operator charter\n\n" + charter_text
                            if charter_text else ""
                        ),
                        skill_fitness_state_dir=STATE_DIR,
                        skill_fitness_repo=_selfevo_repo,
                        skill_fitness_cycle_id=_cycle_id,
                        skill_fitness_cycle_base_sha=main_sha_before,
                        excluded_skill_names=_LOOP_EXCLUDED_SKILLS,
                    )
                    await _repair_mgr.spawn(
                        task=_repair_prompt,
                        task_id=f'selfevo-repair-{_repair_attempts}',
                    )
                    try:
                        await asyncio.wait_for(
                            asyncio.gather(*list(_repair_mgr._running_tasks.values()), return_exceptions=True),
                            timeout=1200.0,  # 20 min max for repair turn
                        )
                    except asyncio.TimeoutError:
                        print(f'repair turn {_repair_attempts} timed out')
                        break
                    # Merge repair-turn read receipts into the primary harness-owned
                    # accumulator; persistence still happens only after integration.
                    mgr._skill_reads_this_cycle.extend(_repair_mgr._skill_reads_this_cycle)
                    # Recount commits after repair — still relative to pre-spawn SHA,
                    # still on the same cycle branch (no push yet).
                    _repair_new = _count_commits_since(_selfevo_repo, _pre_spawn_sha)
                    if _repair_new > cycle_commit_count:
                        print(f'cycle-branch: {_repair_new - cycle_commit_count} additional commit(s) (repair {_repair_attempts})')
                        cycle_commit_count = _repair_new
                    # #686: recompute the changed-file set before EVERY gate re-run,
                    # not just once — a repair turn can add/rename files, and the
                    # bounded gate must select tests against the CURRENT diff, the
                    # same "recompute after repair" discipline #678 F1/F3 applies to
                    # the mutation-surface check. On git failure this keeps the
                    # last-known files_changed rather than gating on an empty set.
                    _rc_files, _rc_blocked, _rc_mut, _rc_tier = _changed_files_and_violations(
                        _selfevo_repo, _pre_spawn_sha,
                    )
                    if _rc_files or _rc_blocked or _rc_mut:
                        files_changed, _blocked_pattern_violations, _mutation_violations = (
                            _rc_files, _rc_blocked, _rc_mut
                        )
                        _cycle_tier = _rc_tier  # #812: tier follows the repaired diff
                    # #1119: re-run the test-weakening detector on every repair
                    # retry too, against the CURRENT diff — a repair turn MAY fix
                    # (or introduce) a weakening; either way this reflects the
                    # latest state, same discipline as the mutation-surface recompute.
                    _test_weakening_blocked, _test_weakening_hard, _test_weakening_soft = (
                        _check_test_weakening(_selfevo_repo, _pre_spawn_sha)
                    )
                    # Re-run smoke tests after repair. Re-applying the shrink guard
                    # on every retry (not just the first check) closes the path
                    # where a repair turn iteratively deletes/weakens tests to make
                    # the suite pass (#678 F2).
                    _smoke_passed, _smoke_output = _run_smoke_tests_with_shrink_guard(
                        _selfevo_repo, _baseline_test_count, changed_files=files_changed,
                        baseline_test_names=_baseline_test_names,
                    )
                    print(f'smoke (after repair {_repair_attempts}): {"PASS" if _smoke_passed else "FAIL"}')
            # ─────────────────────────────────────────────────────────────────────

            # #678 F1/F3: recompute the changed-file set and violation split across
            # ALL commits (initial + every repair turn) right before the gate
            # decides. Without this, a repair subagent editing core nanobot/,
            # .github/workflows, bridge.py, or committing a secret-shaped file would
            # slip past the surface/blocked-pattern check, which was computed only
            # from the FIRST commit above. files_changed also becomes the final set
            # used downstream (backlog-done message, structured lesson).
            if cycle_commit_count > 0 and _selfevo_repo.is_dir():
                _fc, _bpv, _mv, _tier = _changed_files_and_violations(_selfevo_repo, _pre_spawn_sha)
                if _fc or _bpv or _mv:
                    files_changed, _blocked_pattern_violations, _mutation_violations = _fc, _bpv, _mv
                    _cycle_tier = _tier  # #812: tier reflects the full commit set at gate time
                # #1119: final pre-gate recompute of the test-weakening verdict,
                # against the full commit set (initial + every repair turn) —
                # mirrors the mutation-surface final recompute directly above.
                _test_weakening_blocked, _test_weakening_hard, _test_weakening_soft = (
                    _check_test_weakening(_selfevo_repo, _pre_spawn_sha)
                )
                if _test_weakening_soft:
                    # Soft signals never block (v1 policy, issue #1119 non-goals) —
                    # recorded as a ledger event only, for future tightening.
                    append_event(
                        STATE_DIR,
                        {
                            'phase': 'test_weakening',
                            'cycle_id': _cycle_id,
                            'soft_signals': _test_weakening_soft,
                        },
                    )

            # ── #789: spawn-boundary fitness-sidecar tamper detection ────────────
            # Re-hash AFTER the subagent (and any repair turns) finished, BEFORE
            # the gate verdict is recorded. A mismatch is detected + recorded, not
            # restored (state may have legit concurrent writers; restore/rollback
            # is documented follow-up hardening) — the gate still runs, but the
            # ledger and the cycle's key_learnings carry the incident.
            _integrity_post = _fitness_sidecar_hashes(STATE_DIR)
            _integrity_changed = [
                rel for rel in _FITNESS_SIDECARS
                if _integrity_pre.get(rel) != _integrity_post.get(rel)
            ]
            if _integrity_changed:
                print(
                    f'integrity: fitness sidecar(s) written during spawn window: '
                    f'{", ".join(_integrity_changed)} (#789)'
                )
                append_event(
                    STATE_DIR,
                    {
                        'phase': 'integrity',
                        'reason': 'sidecar_write_during_spawn',
                        'cycle_id': _cycle_id,
                        'files': _integrity_changed,
                    },
                )

            # ── #846: out-of-band origin/main push detection ─────────────────────
            # Positive-only, fail-open (see _detect_out_of_band_main). The loop is
            # serial (one timer, lock at ~1228), so within this cycle origin/main
            # must not move except via THIS cycle's own _integrate_cycle_to_main
            # below — any other movement means a subagent (or something) pushed
            # directly to main, bypassing the smoke/deny-set gate entirely.
            # #877: compares against _origin_main_observed (the real origin/main
            # seen at setup), NOT main_sha_before — those two differ whenever a
            # line switch happened, and comparing against the switched-to
            # ancestor would always misfire as a false out-of-band drift.
            _main_drift = _detect_out_of_band_main(_selfevo_repo, _origin_main_observed) if _selfevo_repo.is_dir() else ''
            if _main_drift:
                print(f'integrity: origin/main moved out-of-band {main_sha_before[:12]}->{_main_drift[:12]} — a push bypassed the gate (#846)')
                append_event(
                    STATE_DIR,
                    {
                        'phase': 'integrity',
                        'reason': 'out_of_band_main_push',
                        'cycle_id': _cycle_id,
                        'main_sha_before': main_sha_before,
                        'main_sha_observed': _main_drift,
                    },
                )

            # ── Gate decision: integrate to main ONLY on green (R12-R15) ─────────
            if cycle_commit_count > 0:
                if _blocked_pattern_violations:
                    # #678 F3: a secret-shaped/blocked filename anywhere in the
                    # cycle's commits is a hard block, regardless of smoke result.
                    _rollback_reason = 'blocked_file_present'
                    print(
                        f'blocked-pattern check: {len(_blocked_pattern_violations)} blocked '
                        f'file(s) present — {cycle_branch} kept for forensics, main left unchanged (#678 F3)'
                    )
                    record_gate_decision(
                        STATE_DIR, _cycle_id, False, _rollback_reason, _blocked_pattern_violations,
                    )
                elif _mutation_violations:
                    # #678 F1: mutation-surface violations were previously print-only
                    # while integration was decided solely by the smoke gate. Now a
                    # hard block — same shape as the gate_failed branch below.
                    _rollback_reason = 'mutation_surface_violation'
                    print(
                        f'mutation surfaces: {len(_mutation_violations)} violation(s) — '
                        f'{cycle_branch} kept for forensics, main left unchanged (#678 F1)'
                    )
                    record_gate_decision(
                        STATE_DIR, _cycle_id, False, _rollback_reason, _mutation_violations,
                    )
                elif _test_weakening_blocked:
                    # #1119: a hard test-weakening signal (existing test file
                    # deleted alongside non-test changes, net loss of
                    # assert/pytest.raises in an existing test file, or a
                    # skip/xfail marker newly added to a previously-passing
                    # test) is a hard block — same placement/severity as the
                    # mutation-surface check directly above, and BEFORE the
                    # smoke gate is ever consulted (a weakened test could
                    # otherwise make smoke pass green).
                    _rollback_reason = 'test_weakening'
                    print(
                        f'test-weakening: {len(_test_weakening_hard)} hard signal(s) — '
                        f'{cycle_branch} kept for forensics, main left unchanged (#1119)'
                    )
                    record_gate_decision(
                        STATE_DIR, _cycle_id, False, _rollback_reason, _test_weakening_hard,
                    )
                elif _cycle_tier == 'runtime':
                    # #812: runtime-slice cycle. Surface is clean and includes at least
                    # one operator-approved nanobot/runtime slice module. STRICTER path:
                    # smoke MUST pass (import-smoke + full affected-test run; no
                    # repair-cap auto-integrate shortcut), and a green result does NOT
                    # touch main — it is recorded as a pending promotion candidate +
                    # rollback record for operator review, and the cycle branch is
                    # RETAINED (not cleaned up) as the apply/rollback artifact. This is
                    # what keeps runtime self-optimization reachable (Vector 1) while
                    # never letting the loop auto-mutate the live runtime (#603/#812).
                    if _smoke_passed:
                        record_gate_decision(
                            STATE_DIR, _cycle_id, True, 'runtime_slice_gate_passed', [],
                        )
                        # ── #822 (opus-review follow-up): harness-run causal
                        # micro-benchmark. Runs ONLY here — on the gate-PASS path,
                        # AFTER the #789 integrity re-hash above — never on
                        # blocked/violation/smoke-failed cycles (which would
                        # otherwise pay 2 worktrees + subprocesses and persist an
                        # AUTHORITATIVE entry for code that never lands), and
                        # never inside the pre/post integrity hash window (the
                        # entry this writes to state/heldout/microbench.json is
                        # itself a #789 fitness sidecar — writing it between
                        # _integrity_pre and _integrity_post fired a false
                        # 'sidecar_write_during_spawn' incident on EVERY measured
                        # cycle). Fail-open: a measurement failure must never
                        # affect the gate, which is already decided above.
                        _microbench_entry = None
                        try:
                            from nanobot.runtime.heldout.microbench import measure_cycle as _measure_cycle
                            _microbench_entry = _measure_cycle(
                                STATE_DIR, _selfevo_repo, _cycle_id, main_sha_before, cycle_branch, files_changed,
                            )
                            if _microbench_entry:
                                print(
                                    f"microbench: {_microbench_entry['module']} "
                                    f"{_microbench_entry['baseline_ms']:.2f}ms -> "
                                    f"{_microbench_entry['candidate_ms']:.2f}ms "
                                    f"({_microbench_entry['improvement_pct']:+.1f}%) (#822)"
                                )
                        except Exception:
                            _microbench_entry = None
                        _cand_id = _record_runtime_slice_candidate(
                            STATE_DIR, _selfevo_repo, _cycle_id, cycle_branch,
                            main_sha_before, files_changed, microbench=_microbench_entry,
                        )
                        print(
                            f'runtime-slice: gate green — promotion candidate {_cand_id} '
                            f'recorded for operator review; main left unchanged, '
                            f'{cycle_branch} retained (#812)'
                        )
                    else:
                        _rollback_reason = 'gate_failed'
                        print(
                            f'runtime-slice smoke: cap reached ({_repair_attempts}/'
                            f'{_max_repair_attempts}) without pass — {cycle_branch} kept '
                            f'for forensics (#812)'
                        )
                        record_gate_decision(STATE_DIR, _cycle_id, False, _rollback_reason, [])
                elif _smoke_passed:
                    if _main_drift:
                        # #846: origin/main already moved out-of-band during this
                        # cycle's spawn window — integrating onto the stale
                        # ``main_sha_before`` base would be wrong. The cycle's OWN
                        # work passed smoke, so we keep the gate decision True
                        # 'smoke_passed' — identical to the pre-#846 semantics of
                        # the ``push_rejected`` path this replaces (a mid-cycle push
                        # made the real push fail; main stayed unchanged, cycle not
                        # integrated). We skip the merge/push, leave the branch for
                        # forensics, and rely on the integrity incident recorded
                        # above to flag the out-of-band push. Metrics keyed on the
                        # gate bool see no new case.
                        record_gate_decision(STATE_DIR, _cycle_id, True, 'smoke_passed', [])
                        _rollback_reason = 'out_of_band_main_detected'
                        print(
                            f'integrate SKIPPED (out_of_band_main_detected); {cycle_branch} kept for '
                            'forensics, main left unchanged (#846)'
                        )
                    else:
                        # #877 RED-1/RED-2 (Opus adversarial review): classify the
                        # FULL surface from the real pre-switch origin/main through
                        # HEAD — not just the per-cycle delta the elif branches
                        # above already checked (that delta is base..HEAD, and on a
                        # switched cycle `base` IS the pre_spawn_sha, so it never
                        # sees whatever the switched-to base itself carries). A
                        # forged tree.json (instance-writable; tamper is DETECTED
                        # via the #789 sidecar hash but only LOGGED, never blocked)
                        # could otherwise point a switch at an EXISTENT forensic
                        # selfevo/cycle-* commit that itself carries a deny-set/
                        # mutation-surface violation OR a runtime-tier edit (#812:
                        # origin/main is script-tier-only by invariant — a retained
                        # runtime-tier commit was only ever a promotion CANDIDATE,
                        # never merged, and must never be smuggled onto origin/main
                        # via a switch) — cat-file -e only checks existence, never
                        # provenance or tier. A trivial script edit on top would
                        # then classify clean on the per-cycle delta alone. This
                        # check re-examines origin_main_observed..HEAD, which
                        # covers BOTH the base's own delta-from-real-main AND this
                        # cycle's edits in one pass, and hard-blocks on any
                        # deny-set / mutation-surface / runtime-tier base. It is a
                        # NO-OP on the non-switched path (base ==
                        # origin_main_observed, so this is exactly the same diff
                        # the elif branches above already cleared, always
                        # tier='script') and clean for any LEGITIMATE dormant
                        # line — a genuine ancestor + script edits reclassifies
                        # script-tier clean against real main.
                        #
                        # YELLOW fix: the shared classifier fails OPEN on a git
                        # error (returns clean/empty — see its docstring), which
                        # would read as "no violations -> integrate" here, a
                        # fail-open hole on a security-relevant check. On the
                        # SWITCHED path only (base != origin_main_observed; on the
                        # non-switched path this diff is exactly the
                        # already-cleared per-cycle delta, so an error here would
                        # be surprising and blocking it needlessly would just
                        # waste a cycle) we independently probe the same diff
                        # ourselves and fail CLOSED (block) if it errors, rather
                        # than trusting the shared helper's fail-open default.
                        _switched_base = main_sha_before != _origin_main_observed
                        _base_gate_error = False
                        if _switched_base:
                            import subprocess as _sp_basegate
                            try:
                                _base_diff_probe = _sp_basegate.run(
                                    _git_cmd(_selfevo_repo) + ['diff', '--name-only', _origin_main_observed, 'HEAD'],
                                    capture_output=True, text=True, timeout=30,
                                )
                                _base_gate_error = _base_diff_probe.returncode != 0
                            except Exception:
                                _base_gate_error = True

                        _base_files, _base_blocked, _base_mut, _base_tier = _changed_files_and_violations(
                            _selfevo_repo, _origin_main_observed,
                        )
                        _base_violations = _base_blocked + _base_mut
                        _base_runtime_tier = _base_tier == 'runtime'

                        if _base_gate_error:
                            _rollback_reason = 'switch_base_gate_error'
                            print(
                                f'switch-base gate: could not classify {_origin_main_observed[:12]}..HEAD '
                                f'— integration BLOCKED fail-closed, {cycle_branch} kept for forensics, '
                                'main left unchanged (#877)'
                            )
                            record_gate_decision(
                                STATE_DIR, _cycle_id, False, _rollback_reason,
                                ['base-surface classification error (fail-closed)'],
                            )
                            append_event(STATE_DIR, {
                                'phase': 'evolution_tree',
                                'reason': 'switch_base_gate_error',
                                'cycle_id': _cycle_id,
                                'base_sha': main_sha_before,
                                'origin_main_observed': _origin_main_observed,
                            })
                        elif _base_violations or _base_runtime_tier:
                            # deny-set / mutation-surface / runtime-tier base is caught.
                            _rollback_reason = 'switch_base_gate_blocked'
                            _reported_violations = list(_base_violations) or [
                                f'runtime-tier base (tier={_base_tier}) is not integrable to origin/main (#812)'
                            ]
                            print(
                                f'switch-base gate: {len(_reported_violations)} issue(s) found in '
                                f'{_origin_main_observed[:12]}..HEAD (tier={_base_tier}) — integration BLOCKED, '
                                f'{cycle_branch} kept for forensics, main left unchanged (#877)'
                            )
                            record_gate_decision(
                                STATE_DIR, _cycle_id, False, _rollback_reason, _reported_violations,
                            )
                            append_event(STATE_DIR, {
                                'phase': 'evolution_tree',
                                'reason': 'switch_base_gate_blocked',
                                'cycle_id': _cycle_id,
                                'base_sha': main_sha_before,
                                'origin_main_observed': _origin_main_observed,
                                'base_tier': _base_tier,
                                'violations': _base_violations,
                            })
                            # Never re-offer this poisoned/non-integrable sha as a
                            # switch target — otherwise should_switch keeps
                            # re-selecting it every cycle for as long as the
                            # archive stays stalled.
                            try:
                                from nanobot.runtime import evolution_tree as _evo_tree_blk
                                _evo_tree_blk.mark_switch_blocked(
                                    STATE_DIR, main_sha_before, reason=_rollback_reason,
                                )
                            except Exception:
                                pass
                        else:
                            record_gate_decision(STATE_DIR, _cycle_id, True, 'smoke_passed', [])
                            # #877: expected_origin_main is the real pre-cycle
                            # origin/main (differs from main_sha_before only on a
                            # switched line) — see _integrate_cycle_to_main's
                            # docstring for why this is the correct lease value.
                            if do_integration:
                                _integ = _integrate_cycle_to_main(
                                    _selfevo_repo, cycle_branch, main_sha_before,
                                    expected_origin_main=_origin_main_observed,
                                )
                            else:
                                _integ = {'ok': False, 'reason': 'explore_candidate_deferred'}
                                cand_score = float('-inf')
                                if meas_metric:
                                    _mcmd = meas_metric if " " in meas_metric else f"python {meas_metric}"
                                    import subprocess, re
                                    try:
                                        _p = subprocess.run(_mcmd, cwd=_selfevo_repo, shell=True, capture_output=True, text=True, timeout=120)
                                        if _p.returncode == 0:
                                            _nums = re.findall(r'[-+]?\d*\.\d+|\d+', _p.stdout)
                                            if _nums: cand_score = float(_nums[-1])
                                    except Exception: pass
                            if _integ['ok']:
                                _integrated = True
                                try:
                                    from nanobot.runtime import archive as _archive_mod
                                    _archive_mod.record_stepping_stone(
                                        STATE_DIR, _cycle_id, files_changed,
                                        (backlog_title or req.get('task_title') or '').strip(),
                                    )
                                except Exception:
                                    pass  # steering archive is non-blocking (#844)
                                main_sha_after = _integ['main_sha_after']
                                _cleanup_cycle_branch(_selfevo_repo, cycle_branch)
                                print(f'integrate: {cycle_branch} merged into main and pushed ({cycle_commit_count} commit(s))')
                                # #877: record this generation in the evolution tree
                                # (population = branches, generation = commit).
                                # reward is filled in later cycles from scorecard
                                # latest.json best-effort — kept simple for v1.
                                try:
                                    from nanobot.runtime import evolution_tree as _evo_tree
                                    _evo_tree.record_node(
                                        STATE_DIR, sha=main_sha_after, parent_sha=main_sha_before,
                                        branch=cycle_branch, cycle_id=_cycle_id, reward=None,
                                        repo_root=_selfevo_repo,
                                    )
                                except Exception:
                                    pass  # evolution tree bookkeeping is non-blocking (#877)
                            else:
                                _rollback_reason = _integ['reason']
                                main_sha_after = _integ.get('main_sha_after', main_sha_before)
                                print(
                                    f"integrate FAILED ({_rollback_reason}); {cycle_branch} kept for forensics, "
                                    'main left unchanged'
                                )
                else:
                    _rollback_reason = 'gate_failed'
                    print(
                        f'smoke: cap reached ({_repair_attempts}/{_max_repair_attempts}) without pass '
                        f'— leaving {cycle_branch} unintegrated (kept for forensics)'
                    )
                    record_gate_decision(STATE_DIR, _cycle_id, False, _rollback_reason, [])
            commits_pushed = cycle_commit_count if _integrated else 0

            # #939 Part C: persist skill-read fitness sidecar AFTER integration
            # outcome is known.  The bridge's spawn-boundary integrity check
            # (pre/post hash of FITNESS_SIDECARS) already completed above, so this
            # write is harness-side and lands OUTSIDE the protected window.  Only
            # call collect_skill_reads when the cycle actually integrated (success);
            # the birth-use guard inside skill_fitness.py sets confirmed=False when
            # the skill's last-edit commit differs from cycle_base_sha, so
            # authoring cycles always produce confirmed=False rows (recorded for
            # audit, never counted in fitness scoring).  A non-integrated cycle's
            # reads are discarded — the subagent did not ship, so no fitness credit.
            try:
                if _integrated:
                    _sf_count = mgr.collect_skill_reads()
                    if _sf_count:
                        print(f'skill-fitness: recorded {_sf_count} SKILL.md read(s) for cycle {_cycle_id}')
            except Exception:
                pass  # skill-fitness write errors are non-blocking

            if _integrated and backlog_title:
                marked = _try_mark_backlog_done(
                    repo_root=_selfevo_repo,
                    backlog_title=backlog_title,
                    what_was_done=f'bridge subagent committed {commits_pushed} commit(s): {", ".join(files_changed[:3])}',
                )
                if marked:
                    # #678 F6: defense-in-depth — this commit already ran with zero
                    # gate; refuse to push if the diff somehow touches anything
                    # beyond memory/MEMORY.md bookkeeping.
                    if _diff_against_remote_touches_only(
                        _selfevo_repo, 'origin/main', {'memory/MEMORY.md'},
                    ):
                        _git2 = _git_cmd(_selfevo_repo)
                        _sp.run(_git2 + ['push', 'origin', 'main'], capture_output=True)
                        print(f'bridge-memory: moved "{backlog_title[:60]}" to Completed in MEMORY.md')
                    else:
                        print(
                            'bridge-memory: backlog-done diff touched more than memory/MEMORY.md '
                            '— skipping ungated push (#678 F6)'
                        )

            # Memory archiver: run after each integrated commit if MEMORY.md is large or archive is stale
            if _integrated:
                try:
                    import importlib.util as _ilu
                    _arch_path = _selfevo_repo / 'scripts' / 'memory_archiver.py'
                    if _arch_path.exists():
                        _arch_spec = _ilu.spec_from_file_location('memory_archiver', _arch_path)
                        _arch_mod = _ilu.module_from_spec(_arch_spec)  # type: ignore[arg-type]
                        _arch_spec.loader.exec_module(_arch_mod)  # type: ignore[union-attr]
                        if _arch_mod.should_archive(_selfevo_repo):
                            _arch_result = _arch_mod.archive(
                                repo_root=_selfevo_repo,
                                state_root=STATE_DIR,
                                verbose=False,
                            )
                            if _arch_result.get('action') == 'archived':
                                _git3 = _git_cmd(_selfevo_repo)
                                _arch_declared_files = set(_arch_result.get('files_changed', []))
                                for _f in _arch_declared_files:
                                    _sp.run(_git3 + ['add', _f], capture_output=True)
                                _sp.run(_git3 + ['commit', '-m',
                                                 f'chore: archive {_arch_result.get("weeks_archived", 0)} week(s) to MEMORY_ARCHIVE.md'],
                                        capture_output=True)
                                # #678 F6: memory_archiver EXECUTES script code with
                                # zero gate; refuse to push if the commit's diff
                                # includes anything beyond the archiver's own
                                # declared output files.
                                if _diff_against_remote_touches_only(
                                    _selfevo_repo, 'origin/main', _arch_declared_files,
                                ):
                                    _sp.run(_git3 + ['push', 'origin', 'main'], capture_output=True)
                                    print(f'bridge-memory: archived {_arch_result.get("weeks_archived", 0)} week(s) to MEMORY_ARCHIVE.md')
                                else:
                                    print(
                                        'bridge-memory: archiver diff touched files outside its declared '
                                        'output set — skipping ungated push (#678 F6)'
                                    )
                except Exception:
                    pass  # never block on archiver failure
        except Exception as exc:
            print(f'bridge: unexpected error during cycle {cycle_branch}: {exc}')
            _rollback_reason = _rollback_reason or 'internal_error'
            commits_pushed = cycle_commit_count if _integrated else 0
        finally:
            # Never leave the shared checkout stranded on a cycle branch.
            if not _integrated:
                _restored = _restore_to_main(_selfevo_repo)
                if not _restored:
                    print(f'WARNING: failed to restore {_selfevo_repo} to main after cycle {cycle_branch}')
            try:
                _pre_spawn_sha_file.unlink(missing_ok=True)
            except Exception:
                pass
        return {
            'status': 1,
            'cycle_branch': locals().get('cycle_branch', ''),
            'main_sha_before': locals().get('main_sha_before', ''),
            'main_sha_after': locals().get('main_sha_after', locals().get('main_sha_before', '')),
            'files_changed': locals().get('files_changed', []),
            'cycle_commit_count': locals().get('cycle_commit_count', 0),
            'smoke_passed': locals().get('_smoke_passed', False),
            'smoke_output': locals().get('_smoke_output', ''),
            'smoke_ran': locals().get('_smoke_ran', False),
            'repair_attempts': locals().get('_repair_attempts', 0),
            'max_repair_attempts': locals().get('_max_repair_attempts', 0),
            'auto_committed': locals().get('_auto_committed', False),
            'integrity_changed': locals().get('_integrity_changed', []),
            'rollback_reason': locals().get('_rollback_reason', ''),
            'integrated': locals().get('_integrated', False),
            'score': locals().get('cand_score', float('-inf')),
            'cycle_tier': locals().get('_cycle_tier', 'script'),
            'subagent_task_id': locals().get('_subagent_task_id', None),
            'executor_llm_error': locals().get('_executor_llm_error_text', ''),
            'origin_main_observed': locals().get('_origin_main_observed', locals().get('main_sha_before', ''))
        }


    _explore_n, _explore_metric = _parse_explore_mode(req)
    if _explore_n > 1:
        if not _explore_metric:
            _explore_n = 1
        else:
            try:
                from nanobot.runtime.cycle_ledger import read_events, record_explore_started
                _today = __import__('datetime').datetime.now(__import__('datetime').timezone.utc).strftime('%Y-%m-%d')
                _daily_explores = sum(
                    1 for e in read_events(STATE_DIR)
                    if e.get('phase') == 'explore_started' and str(e.get('ts', '')).startswith(_today)
                )
                if _daily_explores >= 1:
                    _explore_n = 1
                else:
                    record_explore_started(STATE_DIR, _cycle_id, _explore_n, _explore_metric)
            except Exception:
                pass

    if _explore_n <= 1:
        _res = await _evaluate_candidate(_cycle_id, True)
    else:
        candidates_results = []
        for cand_idx in range(_explore_n):
            _cand_id = f"{_cycle_id}-{cand_idx+1}"
            _cres = await _evaluate_candidate(_cand_id, False, _explore_metric)
            if _cres.get('status') == 0:
                continue
            
            if _cres.get('smoke_passed'):
                try:
                    from nanobot.runtime.cycle_ledger import record_explore_candidate
                    record_explore_candidate(STATE_DIR, _cycle_id, _cand_id, _cres['score'])
                except Exception: pass
            candidates_results.append(_cres)
            
        if not candidates_results:
            _res = {'status': 0}
        else:
            _smoke_passers = [c for c in candidates_results if c.get('smoke_passed')]
            if not _smoke_passers:
                _res = candidates_results[-1]
            else:
                _smoke_passers.sort(key=lambda c: c['score'], reverse=True)
                _winner = _smoke_passers[0]
                try:
                    from nanobot.runtime.cycle_ledger import record_explore_selected
                    record_explore_selected(STATE_DIR, _cycle_id, _winner['cycle_branch'])
                except Exception: pass
                
                try:
                    _integ = _integrate_cycle_to_main(
                        STATE_DIR.parent / 'eeebot-self-evolving',
                        _winner['cycle_branch'],
                        _winner['main_sha_before'],
                        expected_origin_main=_winner['origin_main_observed']
                    )
                    if _integ['ok']:
                        _winner['integrated'] = True
                        _winner['rollback_reason'] = ''
                        _winner['main_sha_after'] = _integ.get('main_sha_after', _winner['main_sha_before'])
                        try:
                            from nanobot.runtime import archive as _archive_mod
                            _archive_mod.record_stepping_stone(
                                STATE_DIR, _cycle_id, _winner['files_changed'],
                                (backlog_title or req.get('task_title') or '').strip()
                            )
                        except Exception: pass
                    else:
                        _winner['integrated'] = False
                        _winner['rollback_reason'] = _integ['reason']
                except Exception: pass
                
                _res = _winner

    if _res.get('status') == 0:
        return 0

    _selfevo_repo = STATE_DIR.parent / 'eeebot-self-evolving'
    cycle_branch = _res['cycle_branch']
    main_sha_before = _res['main_sha_before']
    main_sha_after = _res['main_sha_after']
    files_changed = _res['files_changed']
    cycle_commit_count = _res['cycle_commit_count']
    _smoke_passed = _res['smoke_passed']
    _smoke_output = _res['smoke_output']
    _smoke_ran = _res['smoke_ran']
    _repair_attempts = _res['repair_attempts']
    _max_repair_attempts = _res['max_repair_attempts']
    _auto_committed = _res['auto_committed']
    _integrity_changed = _res['integrity_changed']
    _rollback_reason = _res['rollback_reason']
    _integrated = _res['integrated']
    _cycle_tier = _res.get('cycle_tier', 'script')
    _subagent_task_id = _res.get('subagent_task_id')
    _executor_llm_error_text = str(_res.get('executor_llm_error') or '')
    commits_pushed = cycle_commit_count if _integrated else 0
    import subprocess as _sp

    # Write a real completed result to state/subagents/results/ so the coordinator
    # can see that the subagent actually ran (not just a blocked stub).
    # This prevents the coordinator from marking subagents_unused=true every cycle.
    # R12: summarise the smoke-gate repair loop; a capped failure ends "blocked".
    _revision_record = revision_outcome(
        revisions=_repair_attempts,
        smoke_passed=_smoke_passed,
        cap=_max_repair_attempts,
        last_smoke_output=_smoke_output,
    ) if _smoke_ran else None
    _bridge_status = 'completed'
    if _revision_record and _revision_record['outcome'] == 'blocked':
        _bridge_status = 'blocked'
    if _rollback_reason == 'executor_llm_error':
        # #1280: the executor's LLM call never returned and nothing was
        # committed — this is `blocked`, not a new spelling. `blocked` is one
        # of the two statuses `_recent_failure_match` already treats as a
        # failure (with rollback.reason set it matches on either test), so
        # the 24 h suppression window and the proposer's "recent failures"
        # context see the outage cycle; a fresh status would have been
        # invisible to every reader, which is how twelve such cycles read
        # `completed` on 2026-09-04.
        _bridge_status = 'blocked'
    _rollback = {
        'integrated': _integrated,
        'cycle_branch': cycle_branch,
        'main_sha_before': main_sha_before,
        'main_sha_after': main_sha_after,
        'reason': _rollback_reason,
        # #666: bridge committed uncommitted subagent work itself (see
        # _auto_commit_uncommitted_work) because the subagent finished without
        # a git commit despite having a dirty working tree.
        'auto_committed': _auto_committed,
    }
    _write_bridge_completed_result(
        state_dir=STATE_DIR,
        req=req,
        request_id=request_id,
        cycle_id=req.get('cycle_id') or '',
        goal_id=goal_id,
        files_changed=files_changed,
        commits_pushed=commits_pushed,
        result_status=_bridge_status,
        revisions=_revision_record,
        backlog_title=backlog_title,
        rollback=_rollback,
        # #789: a spawn-window fitness-sidecar write is surfaced in the
        # cycle's own learnings so the gate/history reflects the incident.
        extra_learnings=(
            (
                [
                    'INTEGRITY WARNING (#789): fitness sidecar(s) '
                    f'{", ".join(_integrity_changed)} were written during the '
                    'subagent spawn window — only the harness may write fitness '
                    'inputs; recorded as an integrity ledger incident.'
                ]
                if _integrity_changed else []
            )
            + (
                # #1280: the provider error the executor died on, so the
                # proposer's "recent failures" context and a reader of the
                # result see WHY nothing was produced, not just that it wasn't.
                [f'EXECUTOR LLM ERROR (#1280): {_executor_llm_error_text}']
                if _executor_llm_error_text else []
            )
            or None
        ),
    )
    # #720: terminal ledger row, written in the SAME step as the result/merge
    # above (never deferred) so the ledger and git state can't diverge.
    # Outcome mapping: integrated -> success; an unexpected exception ->
    # failed even with zero commits (not a clean no-op); zero commits
    # otherwise -> partial (verify-materialized no-op, nothing to gate);
    # anything else with commits but not integrated (gate/mutation-surface
    # rejection, integrate failure) -> failed. Timeouts (subagent-spawn,
    # repair-turn asyncio.wait_for) fold into the normal commit/gate
    # accounting above rather than a distinct outcome.
    if _integrated:
        _cycle_outcome = 'success'
    elif _rollback_reason in ('internal_error', 'executor_llm_error'):
        # #1280: an executor whose LLM call never returned is a failure with
        # zero commits, not a `partial` no-op — `partial` is what eleven
        # outage cycles read on 2026-09-04 and it kept every reader calm.
        _cycle_outcome = 'failed'
    elif _cycle_tier == 'runtime' and _smoke_passed and cycle_commit_count > 0:
        # #812: a green runtime-slice cycle produced a valid promotion candidate.
        # It never touches main (never 'success'), but it is NOT a failure — mark
        # it distinctly so fitness/analytics don't read it as a failed cycle.
        _cycle_outcome = 'promotion_candidate'
    elif cycle_commit_count == 0:
        _cycle_outcome = 'partial'
    else:
        _cycle_outcome = 'failed'
    # #1118: 'partial'/'failed' rows are the only ambiguous case (see
    # _derive_cycle_verdict's docstring) — attempt to resolve them further by
    # reading the executor's OWN structured final answer for this cycle's
    # subagent (best-effort, fail-open, no new LLM call). A confirmed
    # ``outcome: skipped`` self-report upgrades the verdict reason so a
    # verified already-done skip records 'reject', not 'inconclusive'.
    _verdict_reason_hint = _rollback_reason
    if _cycle_outcome in ('partial', 'failed') and not _rollback_reason:
        if _executor_reported_skipped(STATE_DIR, _subagent_task_id):
            _verdict_reason_hint = 'executor_reported_skipped'
    _verdict, _verdict_reason = _derive_cycle_verdict(_cycle_outcome, _verdict_reason_hint)
    record_cycle_outcome(
        STATE_DIR, _cycle_id, _cycle_outcome, _rollback_reason, files_changed, cycle_branch,
        verdict=_verdict, verdict_reason=_verdict_reason,
    )
    # #721: post-cycle tag at the terminal HEAD, same outcome value as the
    # ledger row above. Integrated -> main_sha_after (shared checkout stayed on
    # main, now at the merge commit). Not integrated -> the cycle branch's own
    # tip if it still exists (the finally block above only restores the shared
    # checkout to main; it does not delete the cycle branch ref except on a
    # successful integrate), falling back to main_sha_before when there were no
    # commits at all (or the branch ref lookup itself fails).
    _post_tag_sha = (
        main_sha_after if _integrated
        else (_safe_rev_parse(_selfevo_repo, cycle_branch) or main_sha_before)
    )
    _tag_cycle_post(_selfevo_repo, _cycle_id, _cycle_outcome, _post_tag_sha)

    # Reporting-only citation signal from bounded proposal/transcript fields.
    try:
        from nanobot.runtime.lesson_v2 import record_citations as _record_lesson_citations
        _record_lesson_citations(
            STATE_DIR,
            _cycle_id,
            [
                str(_artifact_data.get('proposal') or ''),
                str(_artifact_data.get('transcript') or ''),
                str(_artifact_data.get('response') or ''),
            ],
        )
    except Exception:
        pass

    # Structured lesson recording after a successful integrated commit (#1070)
    # Only record if the cycle generated genuine lesson/insight content, not plain protocol details
    if _integrated and _has_meaningful_lesson(_artifact_data) and _has_delta_evidence(
        _artifact_data, repo_root=_selfevo_repo, backlog_title=backlog_title,
    ):
        try:
            _written_lesson = _write_structured_lesson(
                repo_root=_selfevo_repo,
                cycle_id=req.get('cycle_id') or '',
                backlog_title=backlog_title,
                files_changed=files_changed,
                commits_pushed=commits_pushed,
                artifact_data=_artifact_data,
            )
            if _written_lesson:
                _git4 = _git_cmd(_selfevo_repo)
                _sp.run(_git4 + ['add', 'lessons/lessons.yaml', 'lessons/archive/'], capture_output=True)
                _sp.run(
                    _git4 + ['commit', '-m', f'chore: record structured lesson for [{req.get("cycle_id","")[:12]}]'],
                    capture_output=True,
                )
                # #678 F6 / #985: defense-in-depth — refuse to push if the
                # commit's diff touches anything beyond lessons/lessons.yaml
                # or lessons/archive/ (rotation archives added in #985).
                _lesson_allowed: set[str] = {'lessons/lessons.yaml'}
                # Dynamically include any archive paths staged in this commit.
                try:
                    import subprocess as _sp_diff985
                    _diff_out = _sp_diff985.run(
                        _git4 + ['diff', '--name-only', 'origin/main', 'HEAD'],
                        capture_output=True, text=True,
                    )
                    for _f in _diff_out.stdout.splitlines():
                        _f = _f.strip()
                        if _f.startswith('lessons/archive/'):
                            _lesson_allowed.add(_f)
                except Exception:
                    pass
                if _diff_against_remote_touches_only(
                    _selfevo_repo, 'origin/main', _lesson_allowed,
                ):
                    _sp.run(_git4 + ['push', 'origin', 'main'], capture_output=True)
                    print('bridge-lesson: recorded structured lesson to lessons/lessons.yaml')
                else:
                    print(
                        'bridge-lesson: lesson diff touched more than lessons/lessons.yaml '
                        '— skipping ungated push (#678 F6)'
                    )
        except Exception:
            pass  # never block on lesson recording failure
    elif _rollback_reason:
        # #1041 Part 2: record structured error on gate rejection/rollback into lessons/errors.yaml
        try:
            _written_error = _write_structured_error(
                repo_root=_selfevo_repo,
                cycle_id=req.get('cycle_id') or '',
                reason=_rollback_reason,
                violated_check=_rollback_reason,
                budget_used={},
                backlog_title=backlog_title,
            )
            if _written_error:
                _git4 = _git_cmd(_selfevo_repo)
                _sp.run(_git4 + ['add', 'lessons/errors.yaml', 'lessons/archive/'], capture_output=True)
                _sp.run(
                    _git4 + ['commit', '-m', f'chore: record structured error for [{req.get("cycle_id","")[:12]}]'],
                    capture_output=True,
                )
                _error_allowed: set[str] = {'lessons/errors.yaml'}
                try:
                    import subprocess as _sp_diff1041
                    _diff_out = _sp_diff1041.run(
                        _git4 + ['diff', '--name-only', 'origin/main', 'HEAD'],
                        capture_output=True, text=True,
                    )
                    for _f in _diff_out.stdout.splitlines():
                        _f = _f.strip()
                        if _f.startswith('lessons/archive/'):
                            _error_allowed.add(_f)
                except Exception:
                    pass
                if _diff_against_remote_touches_only(
                    _selfevo_repo, 'origin/main', _error_allowed,
                ):
                    _sp.run(_git4 + ['push', 'origin', 'main'], capture_output=True)
                    print('bridge-error: recorded structured error to lessons/errors.yaml')
                else:
                    print(
                        'bridge-error: error diff touched more than lessons/errors.yaml '
                        '— skipping ungated push (#678 F6)'
                    )
        except Exception:
            pass  # fail-open

    if _rollback_reason == 'executor_llm_error':
        # #1280: say it with the exit status. The __main__ guard records any
        # non-zero code as a `failure` in bridge/exit_streak.json, so an
        # outage moves consecutive_failures; systemd shows the unit failed;
        # and the deploy health gate — which reads both — will not certify a
        # release whose executor cannot reach its model. That last effect is
        # deliberate: a deploy during a model outage cannot be verified, and
        # "cannot certify" must not read as "green".
        return EXIT_EXECUTOR_LLM_ERROR
    return 0


# #943: bounded mutation and smoke gate helpers are extracted into nanobot.runtime.gate.
from nanobot.runtime import gate as _gate
from nanobot.runtime.gate import _git_cmd, _is_runtime_deny

# Compatibility mirrors retained for AST/external callers. These are the effective
# values used by the production wrappers; a sync regression pins equality with gate.py.
_BLOCKED_FILE_PATTERNS = ('.env', '.git', '.npmrc', 'package-lock', 'yarn.lock', 'id_rsa', 'private_key')
_BLOCKED_WORD_PATTERNS = frozenset({'secret', 'credential', 'token'})
_SENSITIVE_WORDS = _BLOCKED_WORD_PATTERNS
_ALLOWED_SENSITIVE_BASENAMES = frozenset({'token_report.py', 'summarize_token_costs.py', 'token_budget_check.py', 'analyze_token_usage.py', 'check_token_budget.py', 'validate_no_secrets.py', 'count_tokens.py'})
_BLOCKED_EXACT_PATHS = frozenset({'goals.md', 'IDENTITY.md'})
_ALLOWED_PATH_PREFIXES = ('surfaces/', 'scripts/', 'memory/', 'lessons/', 'docs/', 'tests/', 'skills/')
_ALLOWED_EXACT_PATHS = frozenset()
_GATE_EXT_ALLOWLIST = frozenset(('.py', '.md', '.json', '.yaml', '.yml', '.toml', '.txt', '.sh', '.service', '.timer', '.conf', '.cron', '.html', '.css', '.ts', '.js', '.example'))
_GATE_BASENAME_ALLOWLIST = frozenset(('Makefile', 'Dockerfile'))
_RUNTIME_SLICE_ENV = 'SELFEVO_RUNTIME_SLICE'
_SMOKE_ENV_STRIP_PREFIXES = ('STATE_DIR', 'NANOBOT_', 'SUBAGENT_', 'EEEBOT_', 'TARGET_WORKSPACE', 'LITELLM_', 'GOAL_', 'SOURCE_', 'SELFEVO_')
_CORE_SMOKE_TESTS = ('tests/test_import_hygiene.py', 'tests/test_config_schema.py', 'tests/test_config_paths.py')
_RUNTIME_DENY_ALWAYS_FILES = _gate._RUNTIME_DENY_ALWAYS_FILES
_RUNTIME_DENY_TOKENS = _gate._RUNTIME_DENY_TOKENS


def _is_blocked_filename(f: str) -> bool:
    import re as _re_blk
    lower = f.lower().replace(chr(92), '/')
    basename = lower.rsplit('/', 1)[-1]
    stem = basename.rsplit('.', 1)[0]
    if basename in _ALLOWED_SENSITIVE_BASENAMES:
        return False
    structural_blocked = (
        '.git' in lower.split('/') or basename == '.env' or basename.startswith('.env.')
        or basename == '.npmrc' or basename.startswith('.npmrc.')
        or basename == 'package-lock.json' or basename.startswith('package-lock.')
        or basename == 'yarn.lock' or basename.startswith('yarn.lock.')
        or stem == 'id_rsa' or stem.startswith('id_rsa_')
        or 'private_key' in stem or 'secret_key' in stem
    )
    if structural_blocked:
        return True
    segments = [part for part in _re_blk.split(r'[._-]', stem) if part]
    if not segments:
        return False
    last = segments[-1]
    if last.endswith('s') and last[:-1] in _SENSITIVE_WORDS:
        last = last[:-1]
    return last in _SENSITIVE_WORDS


def _validate_mutation_surfaces(changed_files: 'list[str]') -> 'list[str]':
    violations: list[str] = []
    for f in changed_files:
        fname = f.rsplit('/', 1)[-1] if '/' in f else f
        if fname in _BLOCKED_EXACT_PATHS or f in _BLOCKED_EXACT_PATHS:
            violations.append(f'immutable file blocked from mutation: {f}')
            continue
        if f == 'AGENTS.md':
            violations.append(f'operator_owned_path: {f}')
            continue
        if f in _ALLOWED_EXACT_PATHS:
            continue
        if _is_blocked_filename(f):
            violations.append(f'blocked filename pattern in: {f}')
        elif not any(f.startswith(prefix) for prefix in _ALLOWED_PATH_PREFIXES):
            violations.append(f'file outside allowed paths {_ALLOWED_PATH_PREFIXES}: {f}')
    return violations


def _runtime_slice_paths() -> 'set[str]':
    from nanobot.runtime import gate as _gate
    return _gate._runtime_slice_paths(runtime_slice_env=_RUNTIME_SLICE_ENV)


def _classify_mutation_surface(changed_files: 'list[str]') -> 'tuple[list[str], list[str], str]':
    from nanobot.runtime import gate as _gate
    return _gate._classify_mutation_surface(changed_files, runtime_slice_paths=_runtime_slice_paths, is_blocked_filename=_is_blocked_filename, is_runtime_deny=_is_runtime_deny, blocked_exact_paths=_BLOCKED_EXACT_PATHS, allowed_exact_paths=_ALLOWED_EXACT_PATHS, allowed_path_prefixes=_ALLOWED_PATH_PREFIXES, gate_basename_allowlist=_GATE_BASENAME_ALLOWLIST, gate_ext_allowlist=_GATE_EXT_ALLOWLIST)


def _select_gate_tests(repo_root: 'Path', changed_files: 'list[str]') -> 'tuple[list[str], list[str]]':
    from nanobot.runtime import gate as _gate
    return _gate._select_gate_tests(repo_root, changed_files, core_smoke_tests=_CORE_SMOKE_TESTS)


def _sanitized_smoke_env() -> dict:
    from nanobot.runtime import gate as _gate
    return _gate._sanitized_smoke_env(smoke_env_strip_prefixes=_SMOKE_ENV_STRIP_PREFIXES)


def _run_smoke_tests(repo_root: 'Path', changed_files: 'list[str] | None' = None, timeout: int = 300) -> 'tuple[bool, str]':
    from nanobot.runtime import gate as _gate
    return _gate._run_smoke_tests(repo_root, changed_files=changed_files, timeout=timeout, select_gate_tests=_select_gate_tests, sanitized_smoke_env=_sanitized_smoke_env)


def _count_tests(repo_root: 'Path') -> int:
    from nanobot.runtime import gate as _gate
    return _gate._count_tests(repo_root)


def _count_tests_at_ref(repo_root: 'Path', ref: str) -> int:
    from nanobot.runtime import gate as _gate
    return _gate._count_tests_at_ref(repo_root, ref, git_cmd=_git_cmd)


def _test_function_names(repo_root: 'Path') -> 'set[str]':
    from nanobot.runtime import gate as _gate
    return _gate._test_function_names(repo_root)


def _test_function_names_at_ref(repo_root: 'Path', ref: str) -> 'set[str]':
    from nanobot.runtime import gate as _gate
    return _gate._test_function_names_at_ref(repo_root, ref, git_cmd=_git_cmd)


def _run_smoke_tests_with_shrink_guard(repo_root: 'Path', baseline_test_count: int, changed_files: 'list[str] | None' = None, timeout: int = 300, baseline_test_names: 'set[str] | None' = None) -> 'tuple[bool, str]':
    from nanobot.runtime import gate as _gate
    return _gate._run_smoke_tests_with_shrink_guard(repo_root, baseline_test_count, changed_files=changed_files, timeout=timeout, baseline_test_names=baseline_test_names, smoke_runner=_run_smoke_tests, count_tests=_count_tests, test_function_names=_test_function_names)


def _record_runtime_slice_candidate(
    state_dir: 'Path', repo_root: 'Path', cycle_id: str, cycle_branch: str,
    base_sha: 'str | None', changed_files: 'list[str]',
    microbench: 'dict | None' = None,
) -> str:
    """Record a green runtime-slice cycle as a pending promotion candidate. #812.

    Runtime-slice changes never touch the live release via the loop. On a green
    stricter gate this writes a durable candidate under
    ``state/promotions/{id}.json`` with ``review_status='not_ready_for_policy_review'``
    plus a rollback record (the retained cycle branch + base sha + captured diff),
    so an operator can review and, if accepted, carry it into the live release via
    a product PR. Best-effort: never raises into the gate (a candidate-write
    failure must not crash the loop). Returns the candidate id.

    ``microbench`` (#822) is the optional harness-run causal micro-benchmark
    entry for this cycle (``heldout/microbench.py``'s ``measure_cycle``
    return value, or ``None`` if no registered spec matched or a measurement
    failed) — embedded verbatim into the candidate record so an operator
    reviewing a runtime-slice promotion can see a real before/after number,
    not just the diff. Optional so existing callers are unaffected.
    """
    candidate_id = f'promotion-runtime-{_safe_ref_id(cycle_id)}'
    head_sha = _safe_rev_parse(repo_root, 'HEAD') or None
    diff_text = ''
    try:
        import subprocess as _sp_diff
        _base = base_sha or head_sha
        if _base:
            _d = _sp_diff.run(
                _git_cmd(repo_root) + ['diff', _base, 'HEAD'],
                capture_output=True, text=True, timeout=30,
            )
            if _d.returncode == 0:
                diff_text = _d.stdout[:200_000]  # cap so a huge diff can't bloat state
    except Exception:
        pass
    record = {
        'schema_version': 'runtime-slice-promotion-candidate-v1',
        'promotion_candidate_id': candidate_id,
        'origin_cycle_id': cycle_id,
        'tier': 'runtime',
        'review_status': 'not_ready_for_policy_review',
        'decision': 'not_ready_for_policy_review',
        'changed_files': changed_files,
        'microbench': microbench,
        'rollback_record': {
            'cycle_branch': cycle_branch,
            'base_sha': base_sha,
            'head_sha': head_sha,
            'retained_branch': True,
        },
        'diff': diff_text,
        'recorded_at_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'recommended_next_action': 'operator_review_then_product_pr',
    }
    try:
        from nanobot.runtime._io import write_json_atomic

        path = state_dir / 'promotions' / f'{candidate_id}.json'
        latest_path = state_dir / 'promotions' / 'latest.json'
        write_json_atomic(path, record)
        write_json_atomic(latest_path, record)
        try:
            from nanobot.runtime.promotions_rotation import rotate_promotions
            rotate_promotions(state_dir / 'promotions')
        except Exception:
            pass
    except Exception:
        pass
    return candidate_id




# Commit subject prefixes that are maintenance-only and should not count as
# "task done" evidence. A chore-move commit just marks bookkeeping — the task
# keyword appearing in it does NOT mean the real implementation was done.
_ALREADY_DONE_SKIP_PREFIXES = (
    'chore: move ',
    'chore: auto-seed ',
    'chore: auto-mark ',
)


def _task_already_done(backlog_title: str, repo_root: 'Path') -> bool:
    """Return True if backlog_title appears in recent real git commits (last 7 days).

    Checks git log in eeebot-self-evolving for commit messages mentioning the
    backlog title keywords. Maintenance/bookkeeping commits (chore: move,
    chore: auto-seed) are excluded — only substantive commits count.

    Changed from 14 → 7 days to reduce false-positive matches from historical
    chore commits that embed task titles.
    """
    if not backlog_title or not repo_root.is_dir():
        return False

    import re as _re
    import subprocess as _sp2

    _git = ['git', '-c', f'safe.directory={repo_root}', '-C', str(repo_root)]
    result = _sp2.run(
        _git + ['log', '--since=7 days ago', '--pretty=%H %s'],
        capture_output=True, text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return False

    # Extract keywords from backlog_title for fuzzy matching
    words = [w.lower() for w in _re.findall(r'[A-Za-z]{4,}', backlog_title)]
    if not words:
        return False

    for line in result.stdout.strip().splitlines():
        # Skip the hash prefix (first 41 chars) to get subject only
        subject = line[41:].strip() if len(line) > 41 else line
        # Exclude maintenance commits — they embed task titles but aren't implementations
        if any(subject.lower().startswith(skip.lower()) for skip in _ALREADY_DONE_SKIP_PREFIXES):
            continue
        subject_lower = subject.lower()
        # Require at least 3 distinct keywords to match (was 2 — too many false positives)
        matches = sum(1 for w in words if w in subject_lower)
        if matches >= min(3, len(words)):
            return True

    return False


def _request_serves_demand(req: dict) -> bool:
    """True iff the request's task text carries a ``Serves: demand <id>``
    marker line (#760 follow-up) — written by
    :func:`nanobot.runtime.llm_proposer.write_request` via the same
    task-text marker mechanism as ``Target path:`` (#736), because the C1
    schema-equality invariant keeps ``serves`` out of the payload keys.

    Fail-open: any error reads as False, falling back to the pre-existing
    already-done heuristics."""
    try:
        import re as _re

        text = req.get('task')
        if not text or not isinstance(text, str):
            return False
        m = _re.search(r'^Serves:\s*(.+)$', text, _re.MULTILINE)
        return bool(m) and m.group(1).strip().lower().startswith('demand ')
    except Exception:
        return False


# #1118: outcome -> (verdict, reason) mapping, keyed on the SAME string
# already recorded as this row's own ``outcome`` — no new signal, no new
# LLM call, just a deterministic read of a value the ledger already writes.
# ``outcome`` itself is completely untouched by this table (byte-identical
# values/semantics preserved for every existing consumer).
_OUTCOME_TO_VERDICT: dict[str, str] = {
    'success': 'accept',
    'promotion_candidate': 'accept',
    # A duplicate/already-done/recently-rejected skip is a HEALTHY negative
    # result — the loop correctly declined to redo settled work. This is
    # exactly the issue's "reject = verified already-done" case.
    'skipped-duplicate': 'reject',
    # 'partial' (the bridge's catch-all for "no commits landed, no dedup
    # match either") and 'failed' are both ambiguous on their own — could be
    # a genuine gate/infra failure (inconclusive) or a deliberate, honest
    # skip the executor reported in its structured final answer (reject).
    # Resolved by ``reason`` below when available; a bare/unknown reason
    # falls through to inconclusive (fail-closed toward the least confident
    # verdict, never a false accept/reject).
}

# Rollback/dedup reasons that represent a CLEAN, deterministic negative
# result (the loop correctly declined already-settled or already-rejected
# work) rather than an operational failure — these upgrade an otherwise
# ambiguous 'partial'/'failed' outcome to verdict 'reject'.
_REJECT_REASONS = frozenset({
    'already_done_tag', 'already_done', 'recent_duplicate_failure',
    'existence_index_duplicate', 'executor_reported_skipped',
    # #1119: a detected test-weakening attempt is a CONFIRMED, deterministic
    # negative result (the gate correctly caught a reward-hack attempt
    # before it could integrate) — not an ambiguous infra/gate failure, so
    # it upgrades to 'reject' rather than 'inconclusive' like the plain
    # mutation-surface/blocked-file violations below.
    'test_weakening',
})

# Rollback/reason codes that represent infra/harness trouble or an ambiguous
# result — always 'inconclusive', never upgraded to accept/reject even when
# they co-occur with an otherwise-terminal outcome.
_INCONCLUSIVE_REASONS = frozenset({
    'gate_failed', 'mutation_surface_violation', 'blocked_file_present',
    'out_of_band_main_detected', 'switch_base_gate_error',
    'switch_base_gate_blocked', 'head_on_main_precondition_failed',
    'no_commit', 'internal_error', 'executor_llm_error',
})


def _executor_llm_error(state_dir: Path, task_id: 'str | None') -> str:
    """#1280: the executor's own verdict that its LLM call never returned.

    ``nanobot.agent.subagent._run_subagent`` writes the telemetry payload with
    ``status: "error"`` and ``summary: "Error: LLM execution failed: …"`` when
    the provider raised (``litellm.InternalServerError … Connection error``,
    ``litellm.NotFoundError``, timeouts). Until #1280 nothing in the bridge
    read that field: twelve such cycles on 2026-09-04 were recorded
    ``result_status: completed``, ledger ``partial``, and the request was
    retired by the handled_ marker with nothing done.

    Returns the error text (bounded) when the payload says so, else ``""``.
    Fail-open to ``""`` on any read problem — a miss here leaves the old
    behaviour in place, never a false failure.
    """
    if not task_id:
        return ''
    try:
        payload = json.loads((Path(state_dir) / 'subagents' / f'{task_id}.json').read_text(encoding='utf-8'))
        if not isinstance(payload, dict) or str(payload.get('status') or '').lower() != 'error':
            return ''
        text = str(payload.get('summary') or payload.get('result') or '')
        if 'LLM execution failed' not in text:
            return ''
        return text.strip()[:400]
    except Exception:
        return ''


def _decide_handled_marker(handled_marker: Path, req_path: 'Path | str', *, llm_error: bool) -> str:
    """#1280: write the ``handled_`` marker — retiring the request forever —
    unless the subagent died on its LLM call without producing anything, in
    which case re-offer the request up to :data:`LLM_ERROR_MAX_RETRIES`
    times (a sibling ``retry_<id>.json`` counts them) and only then retire
    it. Bounded on purpose: an unbounded re-offer turns a permanently-bad
    request into an infinite loop, a worse failure than the one being fixed.

    Returns ``"handled"``, ``"retry"`` or ``"retired_after_retries"`` for the
    journal. Fail-open: any error writes the marker, the pre-#1280 behaviour.
    """
    try:
        if not llm_error:
            handled_marker.write_text(str(req_path), encoding='utf-8')
            return 'handled'
        retry_path = handled_marker.with_name(handled_marker.name.replace('handled_', 'retry_', 1)).with_suffix('.json')
        count = 0
        if retry_path.exists():
            try:
                count = int((json.loads(retry_path.read_text(encoding='utf-8')) or {}).get('count') or 0)
            except Exception:
                count = 0
        count += 1
        retry_path.write_text(
            json.dumps({
                'count': count,
                'max': LLM_ERROR_MAX_RETRIES,
                'last_ts': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            }),
            encoding='utf-8',
        )
        if count >= LLM_ERROR_MAX_RETRIES:
            handled_marker.write_text(str(req_path), encoding='utf-8')
            print(f'executor_llm_error: request retired after {count} failed LLM attempts (cap {LLM_ERROR_MAX_RETRIES})')
            return 'retired_after_retries'
        print(f'executor_llm_error: request left pending for retry ({count}/{LLM_ERROR_MAX_RETRIES})')
        return 'retry'
    except Exception:
        try:
            handled_marker.write_text(str(req_path), encoding='utf-8')
        except Exception:
            pass
        return 'handled'


def _executor_reported_skipped(state_dir: Path, task_id: 'str | None') -> bool:
    """#1118: best-effort read of the executor's OWN structured final answer
    (the JSON contract in ``build_task()``'s prompt: ``{"outcome": "completed"
    | "skipped" | "blocked", ...}``) to tell "executor verified already-done,
    honestly reported skipped" apart from "ran out of budget / crashed" for a
    zero-commit cycle — both currently collapse into the same ``outcome:
    partial``/``failed`` ledger value (#1118's problem statement).

    Reads the subagent telemetry file the SAME cycle's spawn already wrote
    (``nanobot.agent.subagent``'s ``_write_subagent_telemetry`` —
    ``state_root/subagents/{task_id}.json``, fields ``summary``/``result``
    carry the raw LLM reply text) — no new LLM call, purely a local read of
    data this cycle's own spawn already produced.

    Fail-open to False on ANY problem (missing task_id, missing/unreadable
    file, unparseable JSON, wrong shape) — this only ever UPGRADES an
    otherwise-ambiguous verdict to 'reject'; a miss here just leaves the
    existing fail-closed 'inconclusive' default in place, never a false
    'accept'/'reject'.
    """
    if not task_id:
        return False
    try:
        telemetry_path = Path(state_dir) / 'subagents' / f'{task_id}.json'
        if not telemetry_path.exists():
            return False
        payload = json.loads(telemetry_path.read_text(encoding='utf-8'))
        raw_reply = payload.get('result') or payload.get('summary') or ''
        if not isinstance(raw_reply, str) or not raw_reply.strip():
            return False
        from nanobot.runtime.llm_proposer import _extract_json_object
        parsed = _extract_json_object(raw_reply)
        if not isinstance(parsed, dict):
            return False
        return str(parsed.get('outcome') or '').strip().lower() == 'skipped'
    except Exception:
        return False


def _derive_cycle_verdict(outcome: str, reason: 'str | None') -> tuple[str, 'str | None']:
    """#1118: deterministically derive the tri-state ``verdict`` from the
    SAME ``outcome``/``reason`` values already computed and about to be
    written to the terminal ledger row — no new LLM call, no new parsing of
    the executor's raw reply text (bridge.py has no such parser today, and
    adding one purely to re-derive a signal the ledger already carries in
    ``outcome``/``reason`` would be needless duplication).

    Returns ``(verdict, verdict_reason)``. ``verdict_reason`` mirrors
    ``reason`` when it drove the decision, else is ``None`` (the bare
    ``outcome`` value was enough, e.g. plain ``success``).
    Fail-closed: any unrecognized combination lands on ``'inconclusive'``
    with no reason, never a false ``accept``/``reject``.
    """
    outcome = (outcome or '').strip()
    reason = (reason or '').strip() or None

    if reason in _REJECT_REASONS:
        return 'reject', reason
    if reason in _INCONCLUSIVE_REASONS:
        return 'inconclusive', reason

    mapped = _OUTCOME_TO_VERDICT.get(outcome)
    if mapped:
        return mapped, None

    # 'partial' / 'failed' / anything else with no recognized reason: the
    # ledger cannot tell "executor honestly reported skipped" from "ran out
    # of budget"/"crashed" without a signal this module does not have
    # (see docstring) — inconclusive is the honest, fail-closed answer.
    return 'inconclusive', None


def _extract_target_path(req: dict) -> 'str | None':
    """Return the ``Target path: <path>`` value embedded in a request's task
    text, or None if absent (#736).

    LLM-proposed requests (:func:`nanobot.runtime.llm_proposer.write_request`)
    always carry a target path as a literal ``Target path: <path>`` line in
    the ``task`` field (and it is echoed in ``recommended_next_action`` as
    ``(target: <path>)``, which this also tolerates as a fallback). This is
    used by the pre-spawn dedup gate to distinguish "genuinely new proposal
    whose target file doesn't exist yet" from "keyword-saturated git log
    false positive" (#736 live evidence).

    Fail-open: any exception (missing/malformed fields, regex issues) returns
    None, which falls back to the pre-#736 keyword-only behavior.
    """
    try:
        import re as _re

        for field in ('task', 'recommended_next_action'):
            text = req.get(field)
            if not text or not isinstance(text, str):
                continue
            m = _re.search(r'Target path:\s*(\S+)', text)
            if m:
                return m.group(1).strip().rstrip(').,;')
            m = _re.search(r'\(target:\s*(\S+?)\)', text)
            if m:
                return m.group(1).strip()
        return None
    except Exception:
        return None


def _task_already_done_for_path(
    backlog_title: str, repo_root: 'Path', target_path: str,
) -> bool:
    """Same keyword heuristic as :func:`_task_already_done`, but the git log
    is scoped to commits that touched ``target_path`` (#736).

    When a request carries a target path that already exists in the repo,
    the plain keyword heuristic over the WHOLE 7-day log is prone to false
    positives once history accumulates enough overlapping words (e.g.
    "memory"/"json"/"script"). Scoping the log to the specific path with
    ``git log -- <target_path>`` makes a match mean the file that matters
    was actually touched, not just that similar words appear somewhere in
    unrelated commits.
    """
    if not backlog_title or not repo_root.is_dir() or not target_path:
        return False

    import re as _re
    import subprocess as _sp2

    _git = ['git', '-c', f'safe.directory={repo_root}', '-C', str(repo_root)]
    result = _sp2.run(
        _git + ['log', '--since=7 days ago', '--pretty=%H %s', '--', target_path],
        capture_output=True, text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return False

    words = [w.lower() for w in _re.findall(r'[A-Za-z]{4,}', backlog_title)]
    if not words:
        return False

    for line in result.stdout.strip().splitlines():
        subject = line[41:].strip() if len(line) > 41 else line
        if any(subject.lower().startswith(skip.lower()) for skip in _ALREADY_DONE_SKIP_PREFIXES):
            continue
        subject_lower = subject.lower()
        matches = sum(1 for w in words if w in subject_lower)
        if matches >= min(3, len(words)):
            return True

    return False


#: rollback.reason values written by the pre-spawn dedup SKIP branches in
#: _main_impl (recent-failure suppression and the #750 existence index).
#: These result rows are bookkeeping for proposals that were never spawned —
#: suppressions, not failed execution — so _recent_failure_match must never
#: count them as failure history (#798: one existence-index false positive
#: fed the recent-failure matcher, which then suppressed EVERY later decay
#: proposal off the previous skip's title).
_SKIP_ROLLBACK_REASONS = frozenset({
    'recent_duplicate_failure',
    'existence_index_duplicate',
    'out_of_band_main_detected',
})


def _recent_failure_match(
    dup_check_title: str,
    state_dir: 'Path',
    window_hours: 'float | None' = None,
    max_scan: int = 10,
    target_path: 'str | None' = None,
    entries: 'list[tuple[Path, dict, float]] | None' = None,
) -> 'str | None':
    """Return the title of a recently-failed/rejected result that
    ``dup_check_title`` matches, or None (#716; #757 return type — the
    matched HISTORICAL title, so the ledger's ``matched_against`` can record
    what was actually matched instead of echoing the proposal's own title).

    #713's pre-spawn dedup (``_task_already_done``) only catches proposals that
    already landed as a real git commit. A proposal that was blocked, produced
    no commit, or was rolled back can still be re-proposed and re-spawned every
    cycle — this is a separate, narrower gate: a bounded-recency scan (default
    ``window_hours=24``, via ``SUBAGENT_BRIDGE_FAILURE_SUPPRESS_HOURS``) of
    ``state_dir/subagents/results/*.json`` for entries that failed/never
    integrated, reusing the same failure-proxy criteria as
    :func:`_recent_activity_context` (``rollback.reason`` set, or
    ``result_status`` in ``{'blocked', 'no_commit'}``) and the same
    keyword-overlap threshold as :func:`_task_already_done` (>=3 matched
    ``[A-Za-z]{4,}`` words, or all of them when fewer than 3 exist).

    Intent-keyed precision (#757): word bags alone cascade — one skipped
    "Create test suite for X script" suppressed EVERY later "Create unit
    tests for Y script" title (they share create/unit/tests/script). So
    before the word-overlap check, both the proposal (title +
    ``target_path``) and the historical title are run through
    :func:`nanobot.runtime.existence_index.derive_intent`; if BOTH derive a
    structured (action-class, target) and the targets differ, that entry is
    NOT a match. Same derived intent IS a match (a retry of the same
    (action, target) with different wording). If derivation fails on either
    side, the pre-#757 word-overlap behavior applies unchanged (fail-open
    discipline, consistent with the rest of this module).

    Skips are not failures (#798): the dedup skip branches themselves write
    result rows (``result_status='blocked'`` with ``rollback.reason`` in
    :data:`_SKIP_ROLLBACK_REASONS`) — counting those as failure history let
    one false-positive skip become the "recent failure" that suppressed
    every later same-vocabulary proposal (the 2026-07-18 decay cascade: 5
    candidates, 11 wasted proposals, zero spawns). Such rows — and any
    ``skipped*`` result_status — are excluded from the scan entirely.

    Cross-target precision (#798): result rows also carry the historical
    entry's own ``target_path`` (see :func:`_write_bridge_completed_result`),
    which is fed into :func:`derive_intent` alongside the title. When BOTH
    the proposal and the historical entry name a concrete target path and
    they differ, that entry is never a match — shared verb vocabulary alone
    (the decay lane's archive/unused/script words) must not chain across
    different artifacts, and the word-bag fallback is NOT consulted for that
    pair. When either side lacks a target path, the existing fail-open
    word-bag fallback applies unchanged.

    Only the ``max_scan`` most-recently-modified matching-status result files
    are scanned (mtime is also how the bounded time window is enforced).
    Fail-open: any exception (missing dir, unreadable file, bad JSON) causes
    that entry (or the whole scan) to be skipped/return None — this gate must
    never raise, and must never block a proposal it failed to evaluate.
    """
    try:
        if not dup_check_title:
            return None
        hours = FAILURE_SUPPRESS_HOURS if window_hours is None else window_hours
        # #1176: a missing results/ is no longer a reason to give up. Results
        # migrate to archive/ within the hour while this window is 24h, so the
        # history can live entirely in the archive; the scan below covers both.
        if entries is None and not (state_dir / 'subagents').exists():
            return None

        import re as _re_fail
        import time as _time_fail

        words = [w.lower() for w in _re_fail.findall(r'[A-Za-z]{4,}', dup_check_title)]
        if not words:
            return None

        proposal_intent = derive_intent(dup_check_title, target_path)

        now = _time_fail.time()
        cutoff = now - (hours * 3600.0)

        # #1176: read live results/ AND the rotated archive/. Results migrate
        # out of results/ within the hour, while this window is 24h by
        # default, so the live directory alone held 1 of the 9 failures
        # inside its own window when measured on the host (2026-09-02) —
        # an 89% blind spot that looked exactly like "no recent failure".
        # state_access.artifacts unifies both dirs newest-first and applies
        # the age cutoff itself; #1040's cached-entries path is preserved for
        # callers that supply their own list.
        if entries is not None:
            scanned = [
                (data, mtime)
                for _, data, mtime in sorted(entries, key=lambda x: x[2], reverse=True)
                if mtime >= cutoff
            ]
        else:
            subagents = state_dir / 'subagents'
            merged = _iter_result_entries(subagents / 'results')
            merged = merged + _iter_archive_entries(
                subagents / 'archive', _FAILURE_SCAN_CANDIDATES,
            )
            scanned = [
                (data, mtime)
                for _, data, mtime in sorted(merged, key=lambda x: x[2], reverse=True)
                if mtime >= cutoff
            ]

        # #1176: select FAILURES first, then apply max_scan. Bounding before
        # the status filter was harmless while results/ held ~6 files, but
        # against the archive's 3,000+ the newest `max_scan` entries are
        # mostly successes, so the bound alone would return "no recent
        # failure" every time — the same silence this gate is meant to break.
        failures = []
        for data, _mtime in scanned:
            reason = (data.get('rollback') or {}).get('reason')
            status = data.get('result_status')
            # #798: skip-path bookkeeping rows are suppressions, not failures
            # — the dedup skip branches write result_status='blocked' with a
            # rollback.reason naming the skip. Never let them seed the
            # failure history (see docstring: the decay cascade).
            if reason in _SKIP_ROLLBACK_REASONS:
                continue
            if str(status or '').lower().startswith('skipped'):
                continue
            if not reason and status not in ('blocked', 'no_commit'):
                continue
            failures.append(data)
            if len(failures) >= max_scan:
                break

        for data in failures:
            title = data.get('backlog_title') or data.get('task_title') or ''
            if not title:
                continue
            # #798: the entry's own recorded target path (written since #798
            # by _write_bridge_completed_result; absent on older rows).
            hist_target = str(data.get('target_path') or '').strip() or None
            # #757: when both sides derive a structured (action, target),
            # decide on the intent, not the word bag — different targets are
            # never a match, same target always is.
            candidate_intent = derive_intent(title, hist_target)
            if proposal_intent is not None and candidate_intent is not None:
                if intents_match(proposal_intent, candidate_intent):
                    return title
                continue
            # #798: both sides name a concrete target path but intent
            # derivation failed on a side — two different concrete artifacts
            # are still never a match, and the word bag is not consulted.
            # The fallback below remains only for pairs where a side has no
            # target path at all (fail-open, pre-#798 behavior).
            if (
                target_path and hist_target
                and str(target_path).strip().replace('\\', '/')
                != hist_target.replace('\\', '/')
            ):
                continue
            candidate_words = [w.lower() for w in _re_fail.findall(r'[A-Za-z]{4,}', title)]
            if not candidate_words:
                continue
            matches = sum(1 for w in words if w in candidate_words)
            if matches >= min(3, len(words)):
                return title

        return None
    except Exception:
        return None


def _extract_meaningful_insight(artifact_data: dict | None) -> str | None:
    """Extract genuine, explicit reusable insight from artifact data (#1070).

    Plain integrated success cycles that only contain protocol details
    (commits/files/backlog titles/hypotheses) must not create noise in
    lessons/lessons.yaml. Only explicit insight fields provided in the artifact
    are accepted. No static/boilerplate metric insights are generated.
    """
    if not isinstance(artifact_data, dict):
        return None

    # Check top-level lesson-like containers first, then top-level artifact
    containers: list[dict] = []
    if isinstance(artifact_data.get('lesson'), dict):
        containers.append(artifact_data['lesson'])
    if isinstance(artifact_data.get('structured_lesson'), dict):
        containers.append(artifact_data['structured_lesson'])
    containers.append(artifact_data)

    keys = (
        'reusable_insight',
        'generalized_insight',
        'key_insight',
        'concrete_improvement_statement',
    )
    for d in containers:
        for k in keys:
            val = d.get(k)
            if isinstance(val, str) and val.strip():
                s = val.strip()
                if s.lower() not in {'none', 'n/a', 'na', 'null', 'nil', '{}', '[]'}:
                    return s
    return None


def _has_meaningful_lesson(artifact_data: dict | None) -> bool:
    """Check if artifact_data contains a genuine, explicit insight (#1070)."""
    return _extract_meaningful_insight(artifact_data) is not None


def _extract_reflector_solution(artifact_data: dict | None) -> str | None:
    """Return concrete reflector advice, preferring it over generic insight text."""
    if not isinstance(artifact_data, dict):
        return None
    containers: list[dict] = [artifact_data]
    for key in ("lesson", "structured_lesson", "reflector_finding", "reflector"):
        value = artifact_data.get(key)
        if isinstance(value, dict):
            containers.append(value)
    for container in containers:
        for key in ("reflector_recommendation", "recommendation", "recommendation_detail", "detail"):
            value = container.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:500]
    return None


def _has_delta_evidence(
    artifact_data: dict | None,
    *,
    repo_root: Path | None = None,
    backlog_title: str = "",
) -> bool:
    if not isinstance(artifact_data, dict):
        return False
    containers = [artifact_data]
    for key in ("lesson", "structured_lesson"):
        if isinstance(artifact_data.get(key), dict):
            containers.append(artifact_data[key])
    if any(
        value.get("delta_evidence") or value.get("reflector_delta")
        or value.get("curator_delta") or value.get("repeat_failure")
        for value in containers
    ):
        return True
    if not repo_root or not backlog_title:
        return False
    wanted = _normalize_lesson_problem(backlog_title)
    return bool(wanted and any(
        isinstance(entry, dict)
        and _normalize_lesson_problem(
            entry.get("demand") or entry.get("task_id") or entry.get("backlog_title") or entry.get("title") or ""
        ) == wanted
        for entry in _bounded_lesson_load(Path(repo_root) / "lessons" / "errors.yaml")
    ))


def _write_structured_lesson(
    *,
    repo_root: Path,
    cycle_id: str,
    backlog_title: str,
    files_changed: list[str],
    commits_pushed: int,
    artifact_data: dict,
    budget_used: dict | None = None,
) -> bool:
    """Write a structured lesson entry to lessons/lessons.yaml in eeebot-self-evolving.

    Returns True if lesson was written.
    """
    import datetime as _dt

    insight = _extract_meaningful_insight(artifact_data)
    if not insight or not _has_delta_evidence(
        artifact_data, repo_root=repo_root, backlog_title=backlog_title,
    ):
        return False

    lessons_path = repo_root / 'lessons' / 'lessons.yaml'
    lessons_path.parent.mkdir(parents=True, exist_ok=True)

    # #985: rotate before load so the file is within bounds before we append.
    # Fail-open: rotate_lessons_file() never raises.
    try:
        from nanobot.runtime.lessons_rotation import rotate_lessons_directory as _rotate
        _rotate(lessons_path.parent)
    except Exception:
        pass

    # Load existing lessons (supports YAML or JSON fallback)
    existing: dict = {'lessons': []}
    if lessons_path.exists():
        try:
            raw_text = lessons_path.read_text(encoding='utf-8')
            try:
                import yaml as _yaml  # type: ignore[import-untyped]
                existing = _yaml.safe_load(raw_text) or {'lessons': []}
            except ImportError:
                existing = json.loads(raw_text) if raw_text.strip().startswith('{') else {'lessons': []}
            if not isinstance(existing.get('lessons'), list):
                existing['lessons'] = []
        except Exception:
            existing = {'lessons': []}

    date_str = _dt.date.today().isoformat()
    short_cycle = (cycle_id or '')[-12:].replace('cycle-', '')
    lesson_id = f'LESS-{date_str.replace("-", "")}-{short_cycle[:8]}'

    # Skip if already recorded for this cycle
    if any(e.get('id') == lesson_id for e in existing['lessons']):
        return False

    hypothesis = (
        artifact_data.get('hypothesis')
        or artifact_data.get('concrete_improvement_statement', '')
        or f'Implementing "{backlog_title}" improves operator value.'
    )
    problem = str(artifact_data.get('problem') or hypothesis).strip()
    reflector_solution = _extract_reflector_solution(artifact_data)
    solution = str(reflector_solution or artifact_data.get('solution') or insight).strip()
    raw_tags = artifact_data.get('tags') or ['runtime']
    tags = [str(tag).lower() for tag in raw_tags] if isinstance(raw_tags, list) else []
    if not problem or not solution or not tags or any(tag not in CONTROLLED_LESSON_TAGS for tag in tags):
        return False
    severity = str(artifact_data.get('severity') or 'medium').lower()
    if severity not in {'low', 'medium', 'high', 'critical'}:
        return False
    evidence = artifact_data.get('evidence') or [cycle_id]
    if isinstance(evidence, str):
        evidence = [evidence]
    if not isinstance(evidence, list) or not all(isinstance(item, (str, dict)) for item in evidence):
        return False
    lesson: dict = {
        'schema_version': 2,
        'id': lesson_id,
        'title': str(artifact_data.get('title') or backlog_title)[:200],
        'problem': problem[:400],
        'solution': solution[:400],
        'tags': tags,
        'severity': severity,
        'seen_count': 1,
        'first_seen': date_str,
        'last_seen': date_str,
        'evidence': evidence[:20],
        # Legacy fields retained for fail-open readers.
        'date': date_str,
        'cycle_id': cycle_id,
        'task_id': backlog_title[:80] if backlog_title else 'unknown',
        'hypothesis': str(hypothesis)[:300],
        'result': f'Committed {commits_pushed} commit(s): ' + ', '.join(files_changed[:5]),
        'approach': solution[:400],
        'generalized_insight': insight,
        'reusable_insight': insight,
        'files_changed': files_changed[:10],
    }
    if not _validate_lesson_for_mint(lesson):
        return False
    duplicate = _find_lesson_duplicate(problem, existing['lessons'])
    if duplicate is not None:
        duplicate['seen_count'] = int(duplicate.get('seen_count') or 1) + 1
        duplicate['last_seen'] = date_str
    else:
        existing['lessons'].insert(0, lesson)  # newest-first

    # Fill lateral related links mechanically before writing (#1095).
    # Fail-open: any error in fill_related_links must never block the write.
    try:
        from nanobot.runtime.lesson_v2 import fill_related_links as _fill_related
        existing['lessons'], _ = _fill_related(existing['lessons'])
    except Exception:
        pass

    try:
        try:
            import yaml as _yaml  # type: ignore[import-untyped]
            lessons_path.write_text(_yaml.dump(existing, allow_unicode=True, sort_keys=False), encoding='utf-8')
        except ImportError:
            # Fallback to JSON when PyYAML not installed
            lessons_path.write_text(json.dumps(existing, indent=2, ensure_ascii=False), encoding='utf-8')
        return True
    except Exception:
        return False


def _write_structured_error(
    repo_root: Path,
    cycle_id: str,
    reason: str,
    violated_check: str = "",
    budget_used: dict | None = None,
    backlog_title: str = "",
) -> bool:
    """Record a failed cycle / gate rejection into lessons/errors.yaml and rotate (#1041).

    Mirrors _write_structured_lesson, storing errors with cycle_id, reason, and violated check.
    Fail-open: failures never raise out of the cycle outcome flow.
    """
    import datetime as _dt
    import json

    from nanobot.runtime.lessons_rotation import rotate_lessons_file

    lessons_dir = repo_root / 'lessons'
    lessons_dir.mkdir(exist_ok=True)
    errors_path = lessons_dir / 'errors.yaml'

    try:
        rotate_lessons_file(errors_path)
    except Exception:
        pass

    wrapper_key: str | None = None
    existing_list: list[dict] = []

    if errors_path.exists():
        try:
            raw_text = errors_path.read_text(encoding='utf-8')
            parsed = None
            try:
                import yaml as _yaml  # type: ignore[import-untyped]
                parsed = _yaml.safe_load(raw_text)
            except ImportError:
                stripped = raw_text.strip()
                if stripped.startswith('{') or stripped.startswith('['):
                    parsed = json.loads(raw_text)

            if isinstance(parsed, list):
                existing_list = [e for e in parsed if isinstance(e, dict)]
                wrapper_key = None
            elif isinstance(parsed, dict):
                if isinstance(parsed.get('errors'), list):
                    existing_list = [e for e in parsed['errors'] if isinstance(e, dict)]
                    wrapper_key = 'errors'
                elif isinstance(parsed.get('lessons'), list):
                    existing_list = [e for e in parsed['lessons'] if isinstance(e, dict)]
                    wrapper_key = 'lessons'
                else:
                    existing_list = []
                    wrapper_key = None
            else:
                existing_list = []
                wrapper_key = None
        except Exception:
            existing_list = []
            wrapper_key = None

    date_str = _dt.date.today().isoformat()
    short_cycle = (cycle_id or '')[-12:].replace('cycle-', '')
    error_id = f'ERR-{date_str.replace("-", "")}-{short_cycle[:8]}'

    if any(e.get('id') == error_id for e in existing_list):
        return False

    b_used = budget_used or {}
    tool_calls = int(b_used.get('tool_calls', 0))
    elapsed = int(b_used.get('elapsed_seconds', 0))

    check_str = violated_check or reason or "gate_check"
    error_entry: dict = {
        'id': error_id,
        'date': date_str,
        'cycle_id': cycle_id,
        'task_id': backlog_title[:80] if backlog_title else 'unknown',
        'hypothesis': f'Cycle failed due to {reason or "gate error"}.',
        'result': f'Failed/rejected: {check_str}',
        'reason': reason or 'unknown',
        'violated_check': check_str,
        'tool_calls': tool_calls,
        'elapsed_seconds': elapsed,
        'generalized_insight': f'Avoid {reason or check_str}: cycle gate verification or execution failed.',
    }

    existing_list.insert(0, error_entry)

    to_write = {wrapper_key: existing_list} if wrapper_key is not None else existing_list

    try:
        try:
            import yaml as _yaml  # type: ignore[import-untyped]
            errors_path.write_text(_yaml.dump(to_write, allow_unicode=True, sort_keys=False), encoding='utf-8')
        except ImportError:
            errors_path.write_text(json.dumps(to_write, indent=2, ensure_ascii=False), encoding='utf-8')
        return True
    except Exception:
        return False


# #1219: ``_auto_seed_backlog_from_research`` (and its only caller-side
# helper ``_active_backlog_is_empty``) (seed two MEMORY.md priorities
# from ``state/research/feed.json`` when the Active backlog is empty) was
# removed. Its input had no writer since ``cycle_planning`` was deleted (#924)
# and froze on 2026-08-22 with two entries; it fired twice in the repository's
# history (2760a525 2026-06-24, 1520d8e7 2026-06-22) and never in the 70 days
# since. The demand lanes, not the research feed, drive the loop now.


def _move_priority_to_completed(
    text: str,
    title_escaped: str,
    backlog_title: str,
    what_was_done: str,
) -> str:
    """Move a [Done] priority block from Active backlog to ## Completed section.

    Finds the priority block in Active backlog, extracts it, removes it from
    Active backlog, and appends a compact entry to ## Completed.
    Returns updated text (unchanged if block not found or no Completed section).
    """
    import re as _re

    # Find the priority block to move (between ### Priority N: ... and next ### or end-of-section)
    block_match = _re.search(
        rf'(###\s+Priority\s+(\d+):\s+{title_escaped}[^\n]*(?:\n(?!###)[^\n]*)*)',
        text,
        _re.MULTILINE,
    )
    if not block_match:
        return text

    priority_num = block_match.group(2)
    full_block = block_match.group(1)

    # Remove the block from Active backlog
    text_without_block = text[:block_match.start()] + text[block_match.end():]
    # Clean up double blank lines left behind
    text_without_block = _re.sub(r'\n{3,}', '\n\n', text_without_block)

    # Build compact completed entry
    short_done = what_was_done[:200].strip() if what_was_done else 'Completed.'
    compact_entry = f'\n### Priority {priority_num}: {backlog_title.strip()} [Done]\n{short_done}\n'

    # Append to ## Completed section
    if '## Completed' in text_without_block:
        # Insert after the ## Completed header line
        text_updated = _re.sub(
            r'(## Completed\n(?:<!-- [^\n]* -->\n)?)',
            rf'\1{compact_entry}',
            text_without_block,
            count=1,
        )
    else:
        # No Completed section — just append at end
        text_updated = text_without_block.rstrip() + f'\n\n## Completed\n{compact_entry}'

    return text_updated


def _try_mark_backlog_done(
    *,
    repo_root: Path,
    backlog_title: str,
    what_was_done: str,
) -> bool:
    """Safety-net: if subagent forgot to mark its task [Done] in MEMORY.md, do it now.

    Moves the completed priority block from Active backlog to ## Completed section.
    Returns True if MEMORY.md was updated and committed.
    """
    import re as _re
    import subprocess as _sp

    memory_path = repo_root / 'memory' / 'MEMORY.md'
    if not memory_path.exists() or not backlog_title:
        return False
    try:
        text = memory_path.read_text(encoding='utf-8')
    except Exception:
        return False

    title_escaped = _re.escape(backlog_title.strip())

    # Check if already in Completed section
    completed_section_match = _re.search(r'## Completed(.*)', text, _re.DOTALL)
    if completed_section_match:
        completed_text = completed_section_match.group(1)
        if _re.search(rf'{title_escaped}.*\[Done\]', completed_text, _re.IGNORECASE):
            return False  # already moved to Completed

    # Check if marked [Done] inline in Active backlog (old-style — move it)
    in_active_done = _re.search(
        rf'###\s+Priority\s+\d+:\s+{title_escaped}.*\[Done\]', text, _re.IGNORECASE
    )
    # Check if title exists at all in Active backlog (not yet Done)
    in_active = _re.search(rf'###\s+Priority\s+\d+:\s+{title_escaped}', text, _re.IGNORECASE)

    if not in_active:
        return False  # title not found anywhere active

    # If not yet marked Done, mark it first
    if not in_active_done:
        text = _re.sub(
            rf'(###\s+Priority\s+\d+:\s+{title_escaped})',
            r'\1 [Done]',
            text,
            count=1,
        )

    # Move block to ## Completed section
    updated = _move_priority_to_completed(
        text=text,
        title_escaped=title_escaped,
        backlog_title=backlog_title,
        what_was_done=what_was_done,
    )
    if updated == text:
        return False

    try:
        memory_path.write_text(updated, encoding='utf-8')
    except Exception:
        return False

    _repo = str(repo_root)
    _git = ['git', '-c', f'safe.directory={_repo}', '-C', _repo]
    _sp.run(_git + ['add', 'memory/MEMORY.md'], capture_output=True)
    result = _sp.run(
        _git + ['commit', '-m', f'chore: move "{backlog_title[:60]}" to Completed (bridge safety-net)'],
        capture_output=True, text=True,
    )
    committed = result.returncode == 0
    # #1219: the auto-seed of MEMORY.md priorities from state/research/feed.json
    # that used to follow here is gone with its writer-less input (see
    # _move_priority_to_completed's neighbour comment above).
    return committed


def _write_bridge_completed_result(
    *,
    state_dir: Path,
    req: dict,
    request_id: str,
    cycle_id: str,
    goal_id: str,
    files_changed: list[str],
    commits_pushed: int,
    result_status: str = 'completed',
    key_learnings: list[str] | None = None,
    revisions: dict | None = None,
    backlog_title: str = '',
    rollback: dict | None = None,
    extra_learnings: list[str] | None = None,
) -> None:
    """Write a real subagent-result-v1 artifact after bridge LLM execution.

    Overwrites any blocked stub left by the coordinator materializer so that
    the coordinator's _ambition_underutilization_reasons() sees a completed
    result instead of always flagging subagents_unused=true.

    Args:
        result_status: 'completed', 'already_done', 'no_commit', or 'blocked'
            (R12: smoke-gate revision cap reached without passing).
        key_learnings: override default learnings list.
        extra_learnings: appended AFTER the (default or overridden) learnings
            — used for the #789 spawn-window integrity warning.
        revisions: smoke-gate repair-loop record from stop_guards.revision_outcome.
        rollback: cycle-branch integration record (R8-R15) —
            ``{"integrated": bool, "cycle_branch": str, "main_sha_before": str,
            "main_sha_after": str, "reason": str | None, "auto_committed": bool}``.
            ``main_sha_before`` and ``main_sha_after`` are equal whenever
            ``integrated`` is False — a git-verifiable guarantee that a
            non-integrated cycle never moved ``origin/main``. ``auto_committed``
            is True when the bridge itself committed uncommitted subagent work
            before the gate (#666); see :func:`_auto_commit_uncommitted_work`.
    """
    import datetime as _dt
    results_dir = state_dir / 'subagents' / 'results'
    results_dir.mkdir(parents=True, exist_ok=True)

    safe_id = request_id.replace('/', '_')[:120]
    result_path = results_dir / f'result-{safe_id}.json'

    if result_status == 'already_done':
        summary = 'Task already done — detected in git log; skipped re-execution.'
    elif commits_pushed:
        summary = (
            f'Bridge subagent committed {commits_pushed} change(s): '
            f'{", ".join(files_changed[:3]) if files_changed else "(unknown)"}'
        )
    else:
        summary = 'Bridge subagent ran but produced no new commits.'

    if key_learnings is None:
        if commits_pushed > 0:
            _files_str = ', '.join(files_changed[:3]) if files_changed else '(unknown)'
            key_learnings = [
                f'Committed {commits_pushed} change(s) to: {_files_str}. '
                'Reward signal will be upgraded by coordinator.',
            ]
        elif result_status == 'already_done':
            key_learnings = [
                'Task was detected as already done in git log. '
                'Marked [Done] in MEMORY.md. No subagent spawn needed.',
            ]
        else:
            key_learnings = [
                'Subagent completed without new commits. '
                'Possible causes: (1) task already done, (2) instructions unclear, '
                '(3) subagent explored but did not implement. '
                'Next cycle: check if task is already done before spawning.',
            ]
    if extra_learnings:
        key_learnings = list(key_learnings) + list(extra_learnings)

    payload = {
        'schema_version': 'subagent-result-v1',
        'request_id': request_id,
        'request_path': str(state_dir / 'subagents' / 'requests' / f'request-{cycle_id}.json'),
        'cycle_id': cycle_id,
        'goal_id': goal_id,
        'task_id': req.get('task_id') or req.get('semantic_task_id') or 'subagent-verify-materialized-improvement',
        'semantic_task_id': req.get('semantic_task_id') or req.get('task_id') or 'subagent-verify-materialized-improvement',
        'verification_task_id': request_id,
        'result_status': result_status,
        'status': result_status,
        'materialized_from': 'bridge_llm_execution',
        'executor': 'bridge',
        'created_at': _dt.datetime.now(_dt.timezone.utc).isoformat(),
        'files_changed': files_changed,
        'commits_pushed': commits_pushed,
        'summary': summary,
        'key_learnings': key_learnings,
        'learning_classification': (
            'completed_with_evidence' if files_changed
            else 'already_done' if result_status == 'already_done'
            else 'completed_no_commit'
        ),
        'profile': req.get('profile') or 'bounded_execution',
        'source_artifact': req.get('source_artifact') or '',
        'revisions': revisions,
        'backlog_title': backlog_title,
        # #798: the request's own target path (None when it carries no
        # ``Target path:`` marker) — lets _recent_failure_match compare a new
        # proposal's target against this entry's instead of matching on
        # shared verb vocabulary across different artifacts.
        # _extract_target_path is fail-open by construction.
        'target_path': _extract_target_path(req),
        'rollback': rollback,
    }

    try:
        from nanobot.runtime._io import write_json_atomic

        write_json_atomic(result_path, payload)
        print(f'bridge-result: wrote {result_status} result to {result_path.name}')
    except Exception as exc:
        print(f'bridge-result: failed to write result: {exc}')


def _ensure_line_buffered_streams() -> None:
    """Force line-buffered stdout/stderr so journald timestamps reflect real event time.

    Under systemd, this process's stdout/stderr are a pipe to the journal, which
    Python (and libc) will fully-buffer by default. That delays flush of
    ``print()`` output — sometimes by minutes — so journal timestamps drift from
    the actual event, which previously misled an incident investigation (see
    docs/specs/subagent-bridge/spec.md). ``reconfigure`` is Python 3.7+; guard
    against streams that don't support it (e.g. when stdout/stderr are replaced
    by a non-reconfigurable object in tests).
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True)
        except (AttributeError, ValueError):
            pass


def cli_main() -> int:
    """Synchronous entry point used by the ``scripts/`` wrapper and console script."""
    _ensure_line_buffered_streams()
    return asyncio.run(main())


def _parse_explore_mode(req: dict) -> tuple[int, str]:
    task = req.get('task') or req.get('task_title') or ""
    try:
        import re
        m = re.search(r'(?i)^\s*explore:\s*(\d+)', task, flags=re.MULTILINE)
        if m:
            n = int(m.group(1))
            n = max(1, min(n, 3))

            meas_m = re.search(r'(?i)^\s*measurement:\s*([a-zA-Z0-9_\-]+)', task, flags=re.MULTILINE)
            metric = meas_m.group(1) if meas_m else ""
            return n, metric
        return 1, ""
    except Exception:
        return 1, ""


# NOTE: keep this guard the LAST statement in the module. Under
# ``python -m nanobot.runtime.bridge`` the module body executes top-to-bottom
# with ``__name__ == '__main__'``, so any def placed below this block does not
# exist yet when cli_main() starts the loop — the live bridge then dies with a
# NameError that module-importing tests can never catch (incident 2026-09-01:
# _parse_explore_mode defined below this guard killed every cycle for 8 hours).
if __name__ == '__main__':
    _exit_code = cli_main()
    # #1197: every ordinary exit of a live bridge run is recorded durably
    # (uncaught exceptions are recorded by the sys.excepthook armed in
    # nanobot/__init__). A disabled bridge still never touches STATE_DIR.
    if BRIDGE_ENABLED:
        try:
            from nanobot import crash_record as _crash_record

            _crash_record.record_exit(
                STATE_DIR,
                outcome="success" if _exit_code == 0 else "failure",
                exit_status=_exit_code,
            )
        except Exception as _record_exc:  # already printed by the recorder; keep the run's own code
            print(f"bridge: exit record not written: {_record_exc!r}", file=sys.stderr)
    raise SystemExit(_exit_code)
