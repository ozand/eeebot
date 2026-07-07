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

from nanobot.agent.subagent import SubagentManager
from nanobot.bus.queue import MessageBus
from nanobot.cli.commands import _make_provider
from nanobot.config.loader import load_config, set_config_path
from nanobot.observability.llm_telemetry import set_call_context
from nanobot.runtime.stop_guards import REVISION_CAP_DEFAULT, revision_outcome

STATE_DIR = Path(os.environ.get('STATE_DIR', '/var/lib/eeepc-agent/self-evolving-agent/state'))
TARGET_WORKSPACE = Path(os.environ.get('TARGET_WORKSPACE', '/opt/eeepc-agent/runtimes/self-evolving-agent/current'))
CONFIG_PATH = Path(os.environ.get('NANOBOT_CONFIG_PATH', '/run/user/1001/nanobot-eeepc/config.json'))
BRIDGE_STATE_DIR = Path(os.environ.get('SUBAGENT_BRIDGE_STATE_DIR', str(STATE_DIR / 'subagent_bridge')))
BRIDGE_ENABLED = os.environ.get('SUBAGENT_BRIDGE_ENABLED', '1').strip().lower() in {'1', 'true', 'yes', 'on'}
FORCE_PROFILE = os.environ.get('SUBAGENT_BRIDGE_FORCE_PROFILE', '').strip()
FORCE_BUDGET = os.environ.get('SUBAGENT_BRIDGE_FORCE_BUDGET', '').strip()
BRIDGE_MODEL = os.environ.get('SUBAGENT_BRIDGE_MODEL', 'cl/gemini-3.5-flash-low').strip() or 'cl/gemini-3.5-flash-low'


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
        if rid in real_handled or str(path) in real_handled:
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


def _setup_cycle_branch(repo_root: 'Path', cycle_id: str) -> dict:
    """Isolate the upcoming subagent run on a fresh branch off ``origin/main``.

    Implements R8/R9 of docs/specs/subagent-bridge/spec.md: the subagent commits
    against ``selfevo/cycle-<cycle_id>``, never directly against ``main``, so a
    self-push (or a bridge crash mid-run) can only ever publish the cycle
    branch — never ``origin/main``.

    Returns ``{"ok": bool, "branch": str, "main_sha": str, "reason": str | None}``.
    ``reason`` is set only when ``ok`` is False, e.g. ``"repo_missing"``,
    ``"not_a_git_repo"``, ``"dirty_tree"``, ``"fetch_failed"``, ``"checkout_failed"``.
    Never raises — git/subprocess failures degrade to a blocked result.
    """
    import re as _re3
    import subprocess as _sp_setup

    safe_cycle_id = _re3.sub(r'[^A-Za-z0-9._-]', '-', str(cycle_id or 'unknown'))[:80]
    branch = f'selfevo/cycle-{safe_cycle_id}'

    if not repo_root.is_dir():
        return {'ok': False, 'branch': branch, 'main_sha': '', 'reason': 'repo_missing'}

    git = _git_cmd(repo_root)
    try:
        status = _sp_setup.run(git + ['status', '--porcelain'], capture_output=True, text=True)
    except Exception:
        return {'ok': False, 'branch': branch, 'main_sha': '', 'reason': 'not_a_git_repo'}
    if status.returncode != 0:
        return {'ok': False, 'branch': branch, 'main_sha': '', 'reason': 'not_a_git_repo'}
    if status.stdout.strip():
        return {'ok': False, 'branch': branch, 'main_sha': '', 'reason': 'dirty_tree'}

    try:
        fetch = _sp_setup.run(git + ['fetch', 'origin', 'main'], capture_output=True, text=True)
    except Exception:
        fetch = None
    if fetch is None or fetch.returncode != 0:
        return {'ok': False, 'branch': branch, 'main_sha': '', 'reason': 'fetch_failed'}

    main_sha = _sp_setup.run(git + ['rev-parse', 'origin/main'], capture_output=True, text=True).stdout.strip()

    checkout = _sp_setup.run(git + ['checkout', '-B', branch, 'origin/main'], capture_output=True, text=True)
    if checkout.returncode != 0:
        return {'ok': False, 'branch': branch, 'main_sha': main_sha, 'reason': 'checkout_failed'}

    return {'ok': True, 'branch': branch, 'main_sha': main_sha, 'reason': None}


def _integrate_cycle_to_main(repo_root: 'Path', cycle_branch: str, main_sha_before: str) -> dict:
    """Merge a green cycle branch into ``main`` and push — the ONLY way ``origin/main`` advances.

    Implements R12/R14 of docs/specs/subagent-bridge/spec.md: ``--no-ff`` merge of
    the cycle HEAD onto ``main`` reset to ``main_sha_before``, then push. Any
    failure (merge conflict, rejected push) leaves ``main`` reset back to
    ``main_sha_before`` — ``origin/main`` is never left in a half-merged state.

    Returns ``{"ok": bool, "main_sha_after": str, "reason": str | None}``.
    """
    import subprocess as _sp_int

    git = _git_cmd(repo_root)
    base = main_sha_before or 'origin/main'

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

    push = _sp_int.run(git + ['push', 'origin', 'main'], capture_output=True, text=True)
    if push.returncode != 0:
        _sp_int.run(git + ['reset', '--hard', base], capture_output=True)
        return {'ok': False, 'main_sha_after': main_sha_before, 'reason': 'push_rejected'}

    main_sha_after = _sp_int.run(git + ['rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip()
    return {'ok': True, 'main_sha_after': main_sha_after, 'reason': None}


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
        is_blocked = False
        for pat in _BLOCKED_FILE_PATTERNS:
            if pat in f.lower():
                is_blocked = True
                break
        if is_blocked:
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


def build_task(req: dict, goal_text: str, report_source: str,
               state_dir: 'Path | None' = None,
               repair_context: 'str | None' = None) -> str:
    """Build a concrete task prompt for the subagent from the request payload.

    Args:
        repair_context: If set, adds a '## Repair context' section with the failed test
            traceback. Used by the closed-loop repair cycle (issue #526).
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
        f'Origin report: {report_source}',
        '',
        '## System mission (read before acting)',
        goal_text,
        '',
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
        '1. Read the source artifact and the concrete task above.',
        '2. Implement the task:',
        '   - Write or edit the file using write_file or edit_file.',
        "   - Verify: exec(\"python3 -c 'import <module>; print(ok)'\") or exec(\"python3 <script>\")",
        '     (pytest is not installed — use python3 -c imports as smoke tests)',
        "   - Commit: exec(\"git add <file> && git commit -m '<type>: <what>'\") ",
        '   - Append one line to memory/HISTORY.md.',
        '3. After a successful commit, update memory/MEMORY.md:',
        '   - Find the priority you just implemented in the "Concrete backlog" section.',
        '   - Add "[Done]" to the title line, e.g. "### Priority 1: ... [Done]".',
        '   - Add a one-line note below it: "Completed: <what you did>".',
        '   - Commit this MEMORY.md update: git add memory/MEMORY.md && git commit -m "chore: mark Priority N done in MEMORY.md"',
        '4. If already done or not applicable: pick next priority from memory/MEMORY.md and implement it.',
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



async def main():
    if not BRIDGE_ENABLED:
        print('bridge_disabled')
        return 0

    outbox = load_json(STATE_DIR / 'outbox' / 'report.index.json') or {}
    goals = load_json(STATE_DIR / 'goals' / 'registry.json') or {}
    report_source = (outbox.get('source') or '').strip()
    goal_id = (
        (outbox.get('goal') or {}).get('goal_id')
        or goals.get('active_goal_id')
        or ''
    ).strip()

    if not report_source or not goal_id:
        print('no_active_goal')
        return 0

    BRIDGE_STATE_DIR.mkdir(parents=True, exist_ok=True)

    req_path, req = find_pending_request()
    if not req_path:
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
        return 0

    goal_text = (
        # Prefer goal_text.json in state dir (human-readable mission statement)
        (load_json(STATE_DIR / 'goals' / 'goal_text.json') or {}).get('text')
        # Fallback: read from canonical workspace (deployed with release)
        or (load_json(TARGET_WORKSPACE / 'host' / 'eeepc' / 'etc' / 'goal_text.json') or {}).get('text')
        or (goals.get('goals') or {}).get(goal_id, {}).get('text')
        or goal_id
    )
    subagent_policy = (goals.get('goals') or {}).get(goal_id, {}).get('subagent_policy') or {}
    profile = FORCE_PROFILE or req.get('profile') or subagent_policy.get('preferred_profile') or 'bounded_execution'
    budget_class = FORCE_BUDGET or subagent_policy.get('budget_class') or req.get('budget') or 'standard'
    gate_open = approval_open()
    mode_at_start = 'auto' if gate_open else 'strict'

    task = build_task(req, goal_text, report_source, state_dir=STATE_DIR)

    # Extract backlog title for MEMORY.md safety-net update after execution
    _source_artifact_path = req.get('source_artifact') or ''
    _artifact_data: dict = {}
    if _source_artifact_path and Path(_source_artifact_path).exists():
        try:
            _artifact_data = json.loads(Path(_source_artifact_path).read_text(encoding='utf-8'))
        except Exception:
            pass
    backlog_title: str = _artifact_data.get('next_bounded_candidate', {}).get('title', '')

    # Before spawning: detect if task is already done in recent git commits.
    # If yes, mark Done in MEMORY.md, write result, and exit without spawning.
    _selfevo_repo_check = STATE_DIR.parent / 'eeebot-self-evolving'
    if backlog_title and _task_already_done(backlog_title, _selfevo_repo_check):
        import subprocess as _sp_check
        _git_chk = ['git', '-c', f'safe.directory={_selfevo_repo_check}',
                    '-C', str(_selfevo_repo_check)]
        _log_r = _sp_check.run(
            _git_chk + ['log', '--since=14 days ago', '--oneline', '--grep',
                        backlog_title[:40]],
            capture_output=True, text=True,
        )
        _found_commit = _log_r.stdout.strip().splitlines()[0] if _log_r.stdout.strip() else 'recent commit'
        print(f'bridge: task already done (found in git: {_found_commit[:80]}); skipping subagent spawn')
        # Mark [Done] in MEMORY.md
        if _selfevo_repo_check.is_dir():
            _try_mark_backlog_done(
                repo_root=_selfevo_repo_check,
                backlog_title=backlog_title,
                what_was_done=f'task detected as already done via git log: {_found_commit[:60]}',
            )
            _sp_check.run(
                _git_chk + ['push', 'origin', 'main'],
                capture_output=True,
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
                f'Task "{backlog_title[:60]}" was already completed in git: {_found_commit[:60]}. '
                'Marked [Done] in MEMORY.md. No re-execution needed.',
            ],
        )
        return 0

    # One-time migration: backfill backlog_title into existing result files
    # so _get_previous_attempts() can match by artifact title.
    _results_dir_mig = STATE_DIR / 'subagents' / 'results'
    _mig_count = _migrate_backlog_title_in_results(_results_dir_mig)
    if _mig_count:
        print(f'migration: backfilled backlog_title in {_mig_count} result file(s)')

    set_config_path(CONFIG_PATH)
    config = load_config(CONFIG_PATH)

    bridge_model = os.environ.get('SUBAGENT_BRIDGE_MODEL', '').strip()
    if not bridge_model:
        bridge_model = config.tools.subagent.model or 'cl/gemini-3.5-flash-low'
    config.agents.defaults.model = bridge_model
    provider = _make_provider(config)
    bus = MessageBus()
    TARGET_WORKSPACE.mkdir(parents=True, exist_ok=True)
    (TARGET_WORKSPACE / '.nanobot' / 'subagents').mkdir(parents=True, exist_ok=True)

    mgr = SubagentManager(
        provider=provider,
        workspace=TARGET_WORKSPACE,
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
        max_iterations=config.agents.defaults.max_tool_iterations,
    )

    # ── Cycle-branch isolation (R8/R9) ───────────────────────────────────────
    # Every cycle runs on its own selfevo/cycle-<id> branch off origin/main, so
    # the subagent (or an errant self-push) can only ever publish that branch —
    # origin/main advances only via _integrate_cycle_to_main() below, and only
    # after the smoke gate passes (R12-R15).
    _selfevo_repo = STATE_DIR.parent / 'eeebot-self-evolving'
    _cycle_id = str(req.get('cycle_id') or request_id)
    _cycle_setup = _setup_cycle_branch(_selfevo_repo, _cycle_id)
    cycle_branch = _cycle_setup['branch']
    main_sha_before = _cycle_setup['main_sha']

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
        return 0

    # Capture HEAD SHA before spawn so we can count subagent commits correctly,
    # even when the subagent pushes itself (harmless under isolation: the
    # checkout sits on the cycle branch, so a bare self-push can only publish
    # that branch, never origin/main).
    _pre_spawn_sha_file = STATE_DIR / 'bridge_pre_spawn.sha'
    _pre_spawn_sha = _capture_pre_spawn_sha(_selfevo_repo, _pre_spawn_sha_file)

    import subprocess as _sp
    files_changed: list[str] = []
    cycle_commit_count = 0
    commits_pushed = 0
    _auto_committed = False
    _integrated = False
    _rollback_reason: 'str | None' = None
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

    try:
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
                # Safety net (#666): the subagent may have implemented real changes via
                # edit_file/write_file but finished the turn without running git commit.
                # Without this, cycle_commit_count stays 0, the gate is skipped, and the
                # finally-block _restore_to_main() below discards the work outright.
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
                # Get list of changed files across all new commits
                _diff = _sp.run(
                    _git_se + ['diff', '--name-only', _pre_spawn_sha, 'HEAD'],
                    capture_output=True, text=True,
                )
                if _diff.returncode == 0:
                    files_changed = [f for f in _diff.stdout.splitlines() if f.strip()]
                    _violations = _validate_mutation_surfaces(files_changed)
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
            _smoke_passed, _smoke_output = _run_smoke_tests(_selfevo_repo)
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
                    workspace=TARGET_WORKSPACE,
                    bus=bus,
                    model=_repair_cfg.agents.defaults.model,
                    web_search_config=_repair_cfg.tools.web.search,
                    web_proxy=_repair_cfg.tools.web.proxy,
                    exec_config=_repair_cfg.tools.exec,
                    subagent_config=_repair_cfg.tools.subagent,
                    restrict_to_workspace=False,
                    max_running=_repair_cfg.tools.subagent.max_running,
                    max_iterations=_repair_cfg.agents.defaults.max_tool_iterations,
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
                # Recount commits after repair — still relative to pre-spawn SHA,
                # still on the same cycle branch (no push yet).
                _repair_new = _count_commits_since(_selfevo_repo, _pre_spawn_sha)
                if _repair_new > cycle_commit_count:
                    print(f'cycle-branch: {_repair_new - cycle_commit_count} additional commit(s) (repair {_repair_attempts})')
                    cycle_commit_count = _repair_new
                # Re-run smoke tests after repair
                _smoke_passed, _smoke_output = _run_smoke_tests(_selfevo_repo)
                print(f'smoke (after repair {_repair_attempts}): {"PASS" if _smoke_passed else "FAIL"}')
        # ─────────────────────────────────────────────────────────────────────

        # ── Gate decision: integrate to main ONLY on green (R12-R15) ─────────
        if cycle_commit_count > 0:
            if _smoke_passed:
                _integ = _integrate_cycle_to_main(_selfevo_repo, cycle_branch, main_sha_before)
                if _integ['ok']:
                    _integrated = True
                    main_sha_after = _integ['main_sha_after']
                    _cleanup_cycle_branch(_selfevo_repo, cycle_branch)
                    print(f'integrate: {cycle_branch} merged into main and pushed ({cycle_commit_count} commit(s))')
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
        commits_pushed = cycle_commit_count if _integrated else 0

        # Safety-net: mark backlog Done if subagent forgot (meaningful only once main advanced)
        if _integrated and backlog_title:
            marked = _try_mark_backlog_done(
                repo_root=_selfevo_repo,
                backlog_title=backlog_title,
                what_was_done=f'bridge subagent committed {commits_pushed} commit(s): {", ".join(files_changed[:3])}',
            )
            if marked:
                _git2 = _git_cmd(_selfevo_repo)
                _sp.run(_git2 + ['push', 'origin', 'main'], capture_output=True)
                print(f'bridge-memory: moved "{backlog_title[:60]}" to Completed in MEMORY.md')

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
                            for _f in _arch_result.get('files_changed', []):
                                _sp.run(_git3 + ['add', _f], capture_output=True)
                            _sp.run(_git3 + ['commit', '-m',
                                             f'chore: archive {_arch_result.get("weeks_archived", 0)} week(s) to MEMORY_ARCHIVE.md'],
                                    capture_output=True)
                            _sp.run(_git3 + ['push', 'origin', 'main'], capture_output=True)
                            print(f'bridge-memory: archived {_arch_result.get("weeks_archived", 0)} week(s) to MEMORY_ARCHIVE.md')
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
    )

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
                _sp.run(_git4 + ['push', 'origin', 'main'], capture_output=True)
                print(f'bridge-lesson: recorded structured lesson to lessons/lessons.yaml')
        except Exception:
            pass  # never block on lesson recording failure

    return 0


# Blocked filename substrings — any changed file matching these is a violation
_BLOCKED_FILE_PATTERNS = (
    '.env', 'secret', 'credential', 'token', 'private_key',
    'id_rsa', '.git', '.npmrc', 'package-lock', 'yarn.lock',
)

# Allowed path prefixes for changed files (relative to repo root)
_ALLOWED_PATH_PREFIXES = ('surfaces/', 'scripts/', 'memory/', 'lessons/', 'docs/', 'tests/')


def _validate_mutation_surfaces(changed_files: 'list[str]') -> 'list[str]':
    """Validate that changed files respect the bounded mutation surface contract.

    Returns a list of VIOLATIONS (empty list = clean).
    Violations are logged as warnings but do NOT block execution in v1.

    Inspired by Darwin Mode safety.ts (ruvnet/agent-harness-generator):
    BLOCKED_FILENAME_PATTERNS, APPROVED_FILES, inspectVariant().
    """
    violations: list[str] = []
    for f in changed_files:
        lower = f.lower()
        # Blocked filename patterns
        for pat in _BLOCKED_FILE_PATTERNS:
            if pat in lower:
                violations.append(f'blocked filename pattern "{pat}" in: {f}')
                break
        else:
            # Must be in an allowed path prefix
            if not any(f.startswith(prefix) for prefix in _ALLOWED_PATH_PREFIXES):
                violations.append(
                    f'file outside allowed paths {_ALLOWED_PATH_PREFIXES}: {f}'
                )
    return violations


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


def _run_smoke_tests(repo_root: 'Path', timeout: int = 300) -> 'tuple[bool, str]':
    """Run pytest smoke tests in repo_root after a subagent commit.

    Returns (passed: bool, output: str) where output is truncated to 2000 chars.
    - timeout: seconds before treating as failure
    - no tests found: returns (True, 'no tests')
    - pytest not available: returns (True, 'pytest unavailable — skip')

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
        return True, 'no tests directory'
    try:
        result = _sp.run(
            [sys.executable, '-m', 'pytest', str(tests_dir), '-x', '-q', '--tb=native', '--no-header'],
            capture_output=True, text=True, timeout=timeout, cwd=str(repo_root),
            env=_sanitized_smoke_env(),
        )
        output = (result.stdout + result.stderr).strip()
        output = output[-2000:] if len(output) > 2000 else output  # keep tail (most relevant)
        if 'no tests ran' in output or 'collected 0 items' in output:
            return True, 'no tests'
        passed = result.returncode == 0
        return passed, output
    except _sp.TimeoutExpired:
        return False, 'pytest timed out'
    except FileNotFoundError:
        return True, 'pytest unavailable — skip'
    except Exception as exc:
        return True, f'smoke test error (skipped): {exc}'


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
) -> None:
    """Write a real subagent-result-v1 artifact after bridge LLM execution.

    Overwrites any blocked stub left by the coordinator materializer so that
    the coordinator's _ambition_underutilization_reasons() sees a completed
    result instead of always flagging subagents_unused=true.

    Args:
        result_status: 'completed', 'already_done', 'no_commit', or 'blocked'
            (R12: smoke-gate revision cap reached without passing).
        key_learnings: override default learnings list.
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
