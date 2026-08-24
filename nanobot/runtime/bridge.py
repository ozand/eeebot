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
from nanobot.runtime.promoted_overlay import effective_runtime_slice, install_promoted_overlay

install_promoted_overlay()

from nanobot.runtime import llm_proposer  # noqa: E402
from nanobot.runtime.backlog_snapshot import write_backlog_snapshot  # noqa: E402
from nanobot.runtime.cycle_ledger import (  # noqa: E402
    VALID_OUTCOMES,
    append_event,
    record_cycle_outcome,
    record_cycle_started,
    record_dedup_decision,
    record_gate_decision,
)
from nanobot.runtime.goal_review import read_charter_text  # noqa: E402
from nanobot.runtime.goal_text_utils import filter_completed_priorities_from_goal_text  # noqa: E402
from nanobot.runtime.existence_index import derive_intent, find_duplicate_script, intents_match  # noqa: E402
from nanobot.runtime.model_registry import resolve_max_tool_iterations, resolve_model  # noqa: E402

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

STATE_DIR = Path(os.environ.get('STATE_DIR', '/var/lib/eeepc-agent/self-evolving-agent/state'))
TARGET_WORKSPACE = Path(os.environ.get('TARGET_WORKSPACE', '/opt/eeepc-agent/runtimes/self-evolving-agent/current'))
CONFIG_PATH = Path(os.environ.get('NANOBOT_CONFIG_PATH', '/run/user/1001/nanobot-eeepc/config.json'))
BRIDGE_STATE_DIR = Path(os.environ.get('SUBAGENT_BRIDGE_STATE_DIR', str(STATE_DIR / 'subagent_bridge')))
BRIDGE_ENABLED = os.environ.get('SUBAGENT_BRIDGE_ENABLED', '1').strip().lower() in {'1', 'true', 'yes', 'on'}
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


def find_pending_request() -> tuple[Path | None, dict]:
    """Find the oldest queued subagent request not yet handled by a real executor."""
    req_dir = STATE_DIR / 'subagents' / 'requests'
    if not req_dir.exists():
        return None, {}

    # Collect request_ids/paths that have REAL results (not blocked stubs)
    real_handled: set[str] = set()
    result_dir = STATE_DIR / 'subagents' / 'results'
    if result_dir.exists():
        for rp in result_dir.glob('*.json'):
            rd = load_json(rp) or {}
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
    if not results_dir.exists():
        return []

    import os as _os
    import json as _json
    import re as _re2
    candidates: list[tuple[float, dict]] = []
    title_words = [w.lower() for w in _re2.findall(r'[A-Za-z]{4,}', backlog_title)] if backlog_title else []

    for entry in _os.scandir(str(results_dir)):
        if not entry.name.endswith('.json') or not entry.is_file():
            continue
        try:
            data = _json.loads(Path(entry.path).read_text(encoding='utf-8'))
        except Exception:
            continue
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
            candidates.append((entry.stat().st_mtime, data))

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


def _migrate_backlog_title_in_results(results_dir: 'Path') -> int:
    """One-time migration: backfill backlog_title into existing bridge result files.

    Iterates bridge_llm_execution results that lack backlog_title and reads the
    title from source_artifact → next_bounded_candidate.title. Idempotent.
    Returns count of files updated.
    """
    if not results_dir.exists():
        return 0
    import json as _json
    updated = 0
    for f in results_dir.glob('*.json'):
        if not f.is_file():
            continue
        try:
            data = _json.loads(f.read_text(encoding='utf-8'))
        except Exception:
            continue
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


def _git_cmd(repo_root: 'Path') -> list[str]:
    """Build the common ``git -C <repo>`` argv prefix used by the cycle-branch helpers."""
    return ['git', '-c', f'safe.directory={repo_root}', '-C', str(repo_root)]


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


def _recent_activity_context(
    state_dir: 'Path | None', selfevo_repo_root: 'Path | None'
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
            if results_dir.exists():
                import json as _json
                rejected: list[tuple[float, str]] = []
                for entry in results_dir.glob('*.json'):
                    if not entry.is_file():
                        continue
                    try:
                        data = _json.loads(entry.read_text(encoding='utf-8'))
                    except Exception:
                        continue
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
                    rejected.append((entry.stat().st_mtime, f'{title}: {note}'))
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
               selfevo_repo_root: 'Path | None' = None) -> str:
    """Build a concrete task prompt for the subagent from the request payload.

    Args:
        repair_context: If set, adds a '## Repair context' section with the failed test
            traceback. Used by the closed-loop repair cycle (issue #526).
        selfevo_repo_root: If set (with state_dir), used to inject a
            '## Recent activity (do not repeat)' section (#713 novelty pressure).
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

    # Build lessons context block from coordinator-injected cards
    lessons_context = req.get('lessons_context') or {}
    lessons_lines: list[str] = []
    if lessons_context.get('relevant_error'):
        err = lessons_context['relevant_error']
        lessons_lines += [
            '## Known pitfall for this task (from lessons/errors.yaml)',
            f"ID: {err.get('id')}  Title: {err.get('title')}",
            f"Root cause: {err.get('root_cause', '')}",
            f"Prevention: {err.get('prevention', '')}",
            '',
        ]
    if lessons_context.get('relevant_lesson'):
        less = lessons_context['relevant_lesson']
        lessons_lines += [
            '## Proven approach for this task (from lessons/lessons.yaml)',
            f"ID: {less.get('id')}  Title: {less.get('title')}",
            f"Approach: {less.get('approach', '')}",
            f"Reusable insight: {less.get('reusable_insight', '')}",
            '',
        ]

    lines = [
        'You are an autonomous improvement subagent for the eeepc self-evolving runtime.',
        '',
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

    # Inject previous attempts section so subagent knows what prior sessions did
    if state_dir is not None and backlog_title:
        _prev = _get_previous_attempts(
            state_dir=state_dir,
            backlog_title=backlog_title,
            cycle_id=str(cycle_id),
        )
        if _prev:
            import datetime as _dt2
            prev_lines = ['## Previous attempts for this task']
            for i, _p in enumerate(_prev, 1):
                _ts = str(_p.get('created_at', ''))[:16].replace('T', ' ')
                _c = _p.get('commits_pushed', 0) or 0
                _kl = (_p.get('key_learnings') or ['(no detail)'])[0][:120]
                _status = _p.get('result_status', 'completed')
                if _c > 0:
                    _outcome_str = f'{_c} commit(s) pushed ✓'
                else:
                    _outcome_str = f'no commits ({_status})'
                prev_lines.append(f'- Attempt {i} ({_ts} UTC): {_outcome_str}. {_kl}')
            # Action instruction only when prior attempts had no commits
            _all_no_commit = all((p.get('commits_pushed') or 0) == 0 for p in _prev)
            if _all_no_commit:
                prev_lines += [
                    '',
                    'IMPORTANT: All previous attempts ended without a git commit.',
                    'You MUST produce at least one commit this session.',
                    'If the task is already done: write one line to memory/HISTORY.md',
                    'confirming it (e.g. "[Done] <task title> verified") and commit that.',
                    'Do not exit without committing.',
                ]
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
        'Work you do not commit is discarded when this turn ends — git commit MUST',
        'be the final step of your session, not an afterthought.',
        '',
        '## Your instructions',
        'You MUST take a concrete action in this session. Do not return a review only.',
        '',
        '1. Before implementing, check the "Recent activity" section above and',
        '   the codebase — if this task is already done, do NOT re-implement it;',
        '   report outcome: skipped.',
        '2. Read the source artifact and the concrete task above.',
        '3. Implement the task:',
        '   - Write or edit the file using write_file or edit_file.',
        "   - Verify: exec(\"python3 -c 'import <module>; print(ok)'\") or exec(\"python3 <script>\")",
        '     (pytest is not installed — use python3 -c imports as smoke tests)',
        "   - Commit: exec(\"git add <file> && git commit -m '<type>: <what>'\") ",
        '   - Append one line to memory/HISTORY.md.',
        '4. After a successful commit, update memory/MEMORY.md:',
        '   - Find the priority you just implemented in the "Concrete backlog" section.',
        '   - Add "[Done]" to the title line, e.g. "### Priority 1: ... [Done]".',
        '   - Add a one-line note below it: "Completed: <what you did>".',
        '   - Commit this MEMORY.md update: git add memory/MEMORY.md && git commit -m "chore: mark Priority N done in MEMORY.md"',
        '5. If already done or not applicable: pick next priority from memory/MEMORY.md and implement it.',
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
        'You have up to 15 iterations. Use them.',
    ]

    # Mutation surfaces: list the 7 approved surfaces for structured evolution
    lines += [
        '',
        '## Mutation surfaces (preferred targets for improvements)',
        'These 7 files define evolvable aspects of agent behaviour.',
        'Prefer editing files in surfaces/ for clean, bounded changes:',
        '  surfaces/task_selector.json    — how coordinator picks the next task',
        '  surfaces/prompt_template.md    — main subagent instruction template',
        '  surfaces/retry_policy.json     — max retries, backoff, give-up criteria',
        '  surfaces/tool_policy.json      — which tools subagent may use',
        '  surfaces/memory_policy.json    — what to read from MEMORY.md',
        '  surfaces/score_weights.json    — reward component weights',
        '  surfaces/lesson_policy.json    — when and what to record as a lesson',
        'You may also edit scripts/ and memory/ files when the task requires it.',
        'Do NOT modify: state/, .env files, tokens, secrets, or systemd units.',
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


async def _main_impl_body():
    # #721: bounded, fail-open tag pruning — run once per bridge invocation
    # (this function runs exactly once per process, per `main()`'s docstring),
    # right after the concurrency lock in `main()` is held, before anything
    # else touches the shared checkout.
    _prune_cycle_tags(STATE_DIR.parent / 'eeebot-self-evolving')

    outbox = load_json(STATE_DIR / 'outbox' / 'report.index.json') or {}
    goals = load_json(STATE_DIR / 'goals' / 'registry.json') or {}
    report_source = (outbox.get('source') or '').strip()
    # #913: drop the outbox bootstrap dependency — the live goal machinery
    # maintains goals/registry.json every cycle, so it is now the PRIMARY
    # source; the outbox's goal_id is kept only as a legacy fallback for
    # hosts still coasting on a frozen outbox snapshot. report_source is no
    # longer required (see build_task above) — a fresh/rebuilt state dir
    # with no outbox/ at all can still bootstrap a cycle as long as a goal
    # id is resolvable from somewhere.
    goal_id = (
        goals.get('active_goal_id')
        or (outbox.get('goal') or {}).get('goal_id')
        or ''
    ).strip()

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
                record_cycle_outcome(
                    STATE_DIR, _cycle_id, 'failed', 'head_on_main_precondition_failed', [], None,
                )
                # #721: no cycle branch exists yet on this path — tag at current HEAD.
                _tag_cycle_post(_selfevo_repo_check, _cycle_id, 'failed')
                return 0

        # #944: read executor mission from immutable goals.md at the release
        # root when available; fall back to the legacy goal_text.json chain.
        # Derived priorities (derived_priorities.json) are always folded in.
        try:
            from nanobot.runtime.goal_review import merged_goal_text
            _charter = read_charter_text(TARGET_WORKSPACE)
        except Exception:
            _charter = ''
        if _charter:
            _base_goal_text = _charter
        else:
            _base_goal_text = (
                # Prefer goal_text.json in state dir
                (load_json(STATE_DIR / 'goals' / 'goal_text.json') or {}).get('text')
                # Fallback: read from canonical workspace (deployed with release)
                or (load_json(TARGET_WORKSPACE / 'host' / 'eeepc' / 'etc' / 'goal_text.json') or {}).get('text')
                or (goals.get('goals') or {}).get(goal_id, {}).get('text')
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
        subagent_policy = (goals.get('goals') or {}).get(goal_id, {}).get('subagent_policy') or {}
        profile = FORCE_PROFILE or req.get('profile') or subagent_policy.get('preferred_profile') or 'bounded_execution'
        budget_class = FORCE_BUDGET or subagent_policy.get('budget_class') or req.get('budget') or 'standard'
        gate_open = approval_open()
        mode_at_start = 'auto' if gate_open else 'strict'

        task = build_task(
            req, goal_text, report_source, state_dir=STATE_DIR,
            selfevo_repo_root=_selfevo_repo_check,
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
            record_cycle_outcome(STATE_DIR, _cycle_id, 'skipped-duplicate', 'already_done_tag', [], None)
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
            record_cycle_outcome(STATE_DIR, _cycle_id, 'skipped-duplicate', 'already_done', [], None)
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
            record_cycle_outcome(STATE_DIR, _cycle_id, 'skipped-duplicate', 'recent_duplicate_failure', [], None)
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
            record_cycle_outcome(STATE_DIR, _cycle_id, 'skipped-duplicate', 'existence_index_duplicate', [], None)
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

    set_config_path(CONFIG_PATH)
    config = load_config(CONFIG_PATH)

    bridge_model = resolve_model('executor', config_fallback=config.tools.subagent.model)
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
        record_cycle_outcome(
            STATE_DIR, _cycle_id, 'failed', _cycle_setup['reason'], [], cycle_branch,
        )
        # #721: cycle branch setup itself failed — tag at main_sha_before
        # (may be '' if even the pre-checkout rev-parse failed; _tag_cycle_post
        # falls back to current HEAD in that case).
        _tag_cycle_post(_selfevo_repo, _cycle_id, 'failed', main_sha_before)
        return 0

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
    charter_text = read_charter_text(TARGET_WORKSPACE)
    # #939 Part E: builtins irrelevant to the self-evolving loop are excluded
    # from the subagent skills summary to reduce context noise.  The list is
    # closed here (bridge-side, not instance-controlled) — instance code cannot
    # widen or override it.
    _LOOP_EXCLUDED_SKILLS = ["weather", "tmux", "clawhub"]
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
        max_iterations=resolve_max_tool_iterations(config.agents.defaults.max_tool_iterations),
        system_context=(
            "# Immutable operator charter\n\n" + charter_text
            if charter_text
            else ""
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

        handled_marker.write_text(str(req_path), encoding='utf-8')
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
        else:
            print(f'cycle-branch: eeebot-self-evolving not found at {_selfevo_repo}')

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
                        _integ = _integrate_cycle_to_main(
                            _selfevo_repo, cycle_branch, main_sha_before,
                            expected_origin_main=_origin_main_observed,
                        )
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
            [
                'INTEGRITY WARNING (#789): fitness sidecar(s) '
                f'{", ".join(_integrity_changed)} were written during the '
                'subagent spawn window — only the harness may write fitness '
                'inputs; recorded as an integrity ledger incident.'
            ]
            if _integrity_changed else None
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
    elif _rollback_reason == 'internal_error':
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
    record_cycle_outcome(
        STATE_DIR, _cycle_id, _cycle_outcome, _rollback_reason, files_changed, cycle_branch,
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

    # Structured lesson recording after a successful integrated commit
    if _integrated:
        try:
            _written_lesson = _write_structured_lesson(
                repo_root=_selfevo_repo,
                cycle_id=req.get('cycle_id') or '',
                backlog_title=backlog_title,
                files_changed=files_changed,
                commits_pushed=commits_pushed,
                artifact_data=_artifact_data,
                budget_used={},  # not available at this point; set via subagent result
            )
            if _written_lesson:
                _git4 = _git_cmd(_selfevo_repo)
                _sp.run(_git4 + ['add', 'lessons/lessons.yaml'], capture_output=True)
                _sp.run(
                    _git4 + ['commit', '-m', f'chore: record structured lesson for [{req.get("cycle_id","")[:12]}]'],
                    capture_output=True,
                )
                # #678 F6: defense-in-depth — refuse to push if the commit's
                # diff touches anything beyond lessons/lessons.yaml.
                if _diff_against_remote_touches_only(
                    _selfevo_repo, 'origin/main', {'lessons/lessons.yaml'},
                ):
                    _sp.run(_git4 + ['push', 'origin', 'main'], capture_output=True)
                    print(f'bridge-lesson: recorded structured lesson to lessons/lessons.yaml')
                else:
                    print(
                        'bridge-lesson: lesson diff touched more than lessons/lessons.yaml '
                        '— skipping ungated push (#678 F6)'
                    )
        except Exception:
            pass  # never block on lesson recording failure

    return 0


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
_BLOCKED_EXACT_PATHS = frozenset({'goals.md'})


def _is_blocked_filename(f: str) -> bool:
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
    if basename in _ALLOWED_SENSITIVE_BASENAMES:
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
    if last.endswith('s') and last[:-1] in _SENSITIVE_WORDS:
        last = last[:-1]
    if last in _SENSITIVE_WORDS:
        return True

    return False


# Allowed path prefixes for changed files (relative to repo root).
# 'skills/' — workspace/instance skill directories (SKILL.md + bundled resources).
# 'AGENTS.md' is the repo-root operator instruction file and may be updated by the
# instance; goals.md remains explicitly denied via _BLOCKED_EXACT_PATHS.
_ALLOWED_PATH_PREFIXES = ('surfaces/', 'scripts/', 'memory/', 'lessons/', 'docs/', 'tests/', 'skills/')
_ALLOWED_EXACT_PATHS = frozenset({'AGENTS.md'})

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
_GATE_BASENAME_ALLOWLIST = frozenset(("Makefile", "Dockerfile", "AGENTS.md"))


def _validate_mutation_surfaces(changed_files: 'list[str]') -> 'list[str]':
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
        if fname in _BLOCKED_EXACT_PATHS or f in _BLOCKED_EXACT_PATHS:
            violations.append(f'immutable file blocked from mutation: {f}')
            continue
        # Allowed exact paths (root AGENTS.md only) bypass the prefix check.
        if f in _ALLOWED_EXACT_PATHS:
            continue
        # Blocked filename patterns
        if _is_blocked_filename(f):
            violations.append(f'blocked filename pattern in: {f}')
        else:
            # Must be in an allowed path prefix
            if not any(f.startswith(prefix) for prefix in _ALLOWED_PATH_PREFIXES):
                violations.append(
                    f'file outside allowed paths {_ALLOWED_PATH_PREFIXES}: {f}'
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


def _runtime_slice_paths() -> 'set[str]':
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
    return effective_runtime_slice(os.environ.get(_RUNTIME_SLICE_ENV))


def _classify_mutation_surface(
    changed_files: 'list[str]',
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
    slice_paths = _runtime_slice_paths()
    blocked: 'list[str]' = []
    violations: 'list[str]' = []
    tier = 'script'
    for f in changed_files:
        lower = f.lower()
        # #944: explicitly blocked exact paths (immutable files).
        fname = f.rsplit('/', 1)[-1] if '/' in f else f
        if fname in _BLOCKED_EXACT_PATHS or f in _BLOCKED_EXACT_PATHS:
            blocked.append(f'immutable file blocked from mutation: {f}')
            continue
        # Allowed exact paths (root AGENTS.md) bypass prefix and pattern checks.
        if f in _ALLOWED_EXACT_PATHS:
            basename2 = Path(f).name
            suffix2 = Path(f).suffix.lower()
            if basename2 not in _GATE_BASENAME_ALLOWLIST and suffix2 not in _GATE_EXT_ALLOWLIST:
                violations.append(
                    f'file extension not gate-exercisable (auto-integration denied): {f}'
                )
            continue
        if _is_blocked_filename(f):
            blocked.append(f'blocked filename pattern in: {f}')
            continue
        if _is_runtime_deny(f):
            violations.append(f'runtime deny-set path (immutable safety shell): {f}')
            continue
        if any(f.startswith(prefix) for prefix in _ALLOWED_PATH_PREFIXES):
            # #863: prefix rules bound WHERE; this bounds WHAT KIND of file can
            # auto-integrate. A file in an allowed prefix but of an unknown/
            # non-exercisable extension (e.g. scripts/foo.rs, scripts/blob.so)
            # would otherwise pass the smoke gate (py_compile + pytest only
            # touch .py) and integrate with zero content verification.
            basename = Path(f).name
            suffix = Path(f).suffix.lower()
            if basename not in _GATE_BASENAME_ALLOWLIST and suffix not in _GATE_EXT_ALLOWLIST:
                violations.append(
                    f'file extension not gate-exercisable (auto-integration denied): {f}'
                )
            continue
        if f.replace('\\', '/') in slice_paths:
            tier = 'runtime'
            continue
        violations.append(
            f'file outside allowed paths {_ALLOWED_PATH_PREFIXES} and not in runtime slice: {f}'
        )
    return blocked, violations, tier


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
        path = state_dir / 'promotions' / f'{candidate_id}.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, indent=2), encoding='utf-8')
    except Exception:
        pass
    return candidate_id


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

    for core in _CORE_SMOKE_TESTS:
        if (repo_root / core).exists():
            test_paths.add(core)

    return sorted(test_paths), sorted(import_targets)


def _sanitized_smoke_env() -> dict:
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
        if not key.startswith(_SMOKE_ENV_STRIP_PREFIXES)
    }


def _run_smoke_tests(
    repo_root: 'Path', changed_files: 'list[str] | None' = None, timeout: int = 300,
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
    test_paths, import_targets = _select_gate_tests(repo_root, changed_files)

    # Phase 1: import-smoke via py_compile — catches syntax/compile breakage
    # in every changed .py file before pytest collection even starts.
    if import_targets:
        try:
            compile_result = _sp.run(
                [sys.executable, '-m', 'py_compile', *import_targets],
                capture_output=True, text=True, timeout=timeout, cwd=str(repo_root),
                env=_sanitized_smoke_env(),
            )
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
            env=_sanitized_smoke_env(),
        )
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


def _count_tests_at_ref(repo_root: 'Path', ref: str) -> int:
    """Count ``def test_`` occurrences across ``tests/**/*.py`` at a git ref, without checkout.

    Reads blobs via ``git show <ref>:<path>`` so it works while the working tree
    is checked out to a different branch (e.g. capturing the pre-cycle baseline
    for origin/main right after ``_setup_cycle_branch`` has already moved the
    checkout to the cycle branch). Returns 0 on any git failure (missing ref, no
    tests/ tree at that ref, ...) — a 0 baseline is treated as "nothing to compare
    against" by :func:`_run_smoke_tests_with_shrink_guard`, never as a violation.
    """
    import subprocess as _sp
    git = _git_cmd(repo_root)
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


def _test_function_names_at_ref(repo_root: 'Path', ref: str) -> 'set[str]':
    """Like :func:`_test_function_names` but reads blobs at a git ``ref`` via
    ``git show``/``ls-tree``, without touching the working tree — mirrors
    :func:`_count_tests_at_ref` (#846). Returns an empty set on any git
    failure (missing ref, no tests/ tree at that ref, ...): "unknown
    baseline" to callers, never a violation.
    """
    import re as _re_names
    import subprocess as _sp
    git = _git_cmd(repo_root)
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
        current = _count_tests(repo_root)
        if current < baseline_test_count:
            return False, (
                f'suite-shrink guard (#678): test count dropped from '
                f'{baseline_test_count} to {current} vs main baseline'
            )
    if baseline_test_names:
        current_names = _test_function_names(repo_root)
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
    return _run_smoke_tests(repo_root, changed_files=changed_files, timeout=timeout)


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

    import subprocess as _sp2
    import re as _re

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
})


def _recent_failure_match(
    dup_check_title: str,
    state_dir: 'Path',
    window_hours: 'float | None' = None,
    max_scan: int = 10,
    target_path: 'str | None' = None,
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
        results_dir = state_dir / 'subagents' / 'results'
        if not results_dir.exists():
            return None

        import re as _re_fail
        import time as _time_fail

        words = [w.lower() for w in _re_fail.findall(r'[A-Za-z]{4,}', dup_check_title)]
        if not words:
            return None

        proposal_intent = derive_intent(dup_check_title, target_path)

        now = _time_fail.time()
        cutoff = now - (hours * 3600.0)

        candidates: list[tuple[float, Path]] = []
        for entry in results_dir.glob('*.json'):
            try:
                if not entry.is_file():
                    continue
                mtime = entry.stat().st_mtime
            except Exception:
                continue
            candidates.append((mtime, entry))
        # Most-recently-modified first, bounded to max_scan before any content check.
        candidates.sort(key=lambda x: x[0], reverse=True)

        for mtime, entry in candidates[:max_scan]:
            if mtime < cutoff:
                continue
            try:
                data = json.loads(entry.read_text(encoding='utf-8'))
            except Exception:
                continue
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


def _derive_insight(
    files_changed: list[str],
    tool_calls: int,
    elapsed_seconds: int,
) -> str:
    """Rules-based insight derivation — no LLM required.

    Returns a reusable insight string based on observable metrics.
    """
    if any('scripts/' in f and f.endswith('.py') for f in files_changed) and tool_calls < 20:
        return f'Short utility scripts implementable in single bridge session ({tool_calls} tool calls).'
    if elapsed_seconds > 0 and elapsed_seconds < 120:
        return f'Fast task: completed under 2 minutes ({elapsed_seconds}s), suitable for micro budget.'
    if any('memory/MEMORY.md' in f for f in files_changed):
        return 'Memory updates should be paired with code commits to avoid metadata-only cycles.'
    if any('memory/HISTORY.md' in f for f in files_changed) and not any(
        f.endswith('.py') or f.endswith('.yaml') for f in files_changed
    ):
        return 'HISTORY.md-only cycles provide no reward signal; pair with code changes.'
    if tool_calls > 30:
        return f'Complex task ({tool_calls} tool calls): consider splitting into smaller priorities.'
    return f'Task completed with {tool_calls} tool calls in {elapsed_seconds}s.'


def _write_structured_lesson(
    *,
    repo_root: Path,
    cycle_id: str,
    backlog_title: str,
    files_changed: list[str],
    commits_pushed: int,
    artifact_data: dict,
    budget_used: dict,
) -> bool:
    """Write a structured lesson entry to lessons/lessons.yaml in eeebot-self-evolving.

    Returns True if lesson was written.
    """
    import datetime as _dt
    import yaml as _yaml  # type: ignore[import-untyped]

    lessons_path = repo_root / 'lessons' / 'lessons.yaml'
    lessons_path.parent.mkdir(parents=True, exist_ok=True)

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

    tool_calls = int(budget_used.get('tool_calls', 0))
    elapsed = int(budget_used.get('elapsed_seconds', 0))
    hypothesis = (
        artifact_data.get('hypothesis')
        or artifact_data.get('concrete_improvement_statement', '')
        or f'Implementing "{backlog_title}" improves operator value.'
    )

    lesson: dict = {
        'id': lesson_id,
        'date': date_str,
        'cycle_id': cycle_id,
        'task_id': backlog_title[:80] if backlog_title else 'unknown',
        'hypothesis': str(hypothesis)[:300],
        'result': f'Committed {commits_pushed} commit(s): ' + ', '.join(files_changed[:5]),
        'tool_calls': tool_calls,
        'elapsed_seconds': elapsed,
        'generalized_insight': _derive_insight(files_changed, tool_calls, elapsed),
        'files_changed': files_changed[:10],
    }

    existing['lessons'].insert(0, lesson)  # newest-first

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


def _active_backlog_is_empty(memory_text: str) -> bool:
    """Return True if ## Active backlog section has no undone Priority blocks."""
    import re as _re
    # Extract Active backlog section (between BACKLOG_START and BACKLOG_END comments, or between headers)
    section_match = _re.search(
        r'## Active backlog.*?(?=\n## |\Z)',
        memory_text,
        _re.DOTALL,
    )
    if not section_match:
        return True  # no active section → treat as empty
    section = section_match.group(0)
    # Check for any Priority block NOT marked Done
    undone = _re.findall(r'###\s+Priority\s+\d+:(?!.*\[Done\])', section)
    return len(undone) == 0


def _auto_seed_backlog_from_research(
    repo_root: Path,
    memory_text: str,
    memory_path: Path,
) -> bool:
    """If Active backlog is empty, add 2 new priorities from state/research/feed.json.

    Returns True if MEMORY.md was updated.
    """
    import re as _re
    import json as _json

    if not _active_backlog_is_empty(memory_text):
        return False

    # Find research/feed.json — state root is ../state relative to repo
    # Convention: repo at .../eeebot-self-evolving, state at .../state
    state_root = repo_root.parent / 'state'
    feed_path = state_root / 'research' / 'feed.json'
    if not feed_path.exists():
        return False

    try:
        feed = _json.loads(feed_path.read_text(encoding='utf-8'))
    except Exception:
        feed = {}

    # Support both list-at-root and {entries: [...]} formats
    if isinstance(feed, list):
        entries = feed
    else:
        entries = feed.get('entries') if isinstance(feed.get('entries'), list) else []

    # Fallback: if feed.json has no entries, try hypotheses.json
    if not entries:
        hyp_path = state_root / 'research' / 'hypotheses.json'
        if hyp_path.exists():
            try:
                hyp_data = _json.loads(hyp_path.read_text(encoding='utf-8'))
                if isinstance(hyp_data, list):
                    # Each element is {cycle_id, candidates:[{title, acceptance}]}
                    for hyp_entry in hyp_data[:5]:
                        cands = hyp_entry.get('candidates') or []
                        for c in cands:
                            title = str(c.get('title') or '').strip()
                            acceptance = str(c.get('acceptance') or '').strip()
                            if title and title not in memory_text:
                                entries.append({'title': title, 'acceptance': acceptance})
                        if len(entries) >= 5:
                            break
            except Exception:
                pass

    if not entries:
        return False

    # Find next priority number
    existing_nums = [int(m) for m in _re.findall(r'###\s+Priority\s+(\d+):', memory_text)]
    next_num = max(existing_nums, default=8) + 1

    new_blocks: list[str] = []
    added = 0
    for entry in entries:
        if added >= 2:
            break
        title = str(entry.get('title') or entry.get('hypothesis') or '').strip()
        if not title or title in memory_text:
            continue
        # Skip tasks that _task_already_done would immediately reject
        if _task_already_done(title, repo_root):
            print(f'bridge-memory: skipping already-done feed entry: {title[:60]}')
            continue
        acceptance = str(entry.get('acceptance') or entry.get('action') or '').strip()
        instructions = acceptance or f'Research candidate from synthesize cycle: {title}'
        block = (
            f'### Priority {next_num}: {title}\n'
            f'{instructions}\n'
            f'Test: verify the change runs without errors.\n'
            f'Commit: git add <file> && git commit -m "feat: {title[:50]}"\n'
        )
        new_blocks.append(block)
        next_num += 1
        added += 1

    if not new_blocks:
        return False

    # Insert new blocks before BACKLOG_END comment (or before ## Completed)
    insertion = '\n'.join(new_blocks)
    if '<!-- BACKLOG_END -->' in memory_text:
        updated = memory_text.replace(
            '<!-- BACKLOG_END -->',
            insertion + '\n<!-- BACKLOG_END -->',
            1,
        )
    elif '## Completed' in memory_text:
        updated = memory_text.replace(
            '## Completed',
            insertion + '\n---\n\n## Completed',
            1,
        )
    else:
        updated = memory_text.rstrip() + '\n\n' + insertion

    if updated == memory_text:
        return False

    memory_path.write_text(updated, encoding='utf-8')
    print(f'bridge-memory: auto-seeded {added} priorities from research/feed.json')
    return True


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
            rf'\1 [Done]',
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

    # Auto-seed: if Active backlog is now empty, add new priorities from research feed
    if committed:
        try:
            fresh_text = memory_path.read_text(encoding='utf-8')
            seeded = _auto_seed_backlog_from_research(repo_root, fresh_text, memory_path)
            if seeded:
                _sp.run(_git + ['add', 'memory/MEMORY.md'], capture_output=True)
                _sp.run(
                    _git + ['commit', '-m', 'chore: auto-seed backlog from research/feed.json (backlog empty)'],
                    capture_output=True,
                )
        except Exception:
            pass  # never block on auto-seed failure

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
        summary = f'Task already done — detected in git log; skipped re-execution.'
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
                f'Task was detected as already done in git log. '
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
        result_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding='utf-8')
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


if __name__ == '__main__':
    raise SystemExit(cli_main())
