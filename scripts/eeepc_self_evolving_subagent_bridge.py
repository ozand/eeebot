#!/usr/bin/env python3
"""eeepc self-evolving subagent bridge.

Reads the latest queued subagent request from state/subagents/requests/,
builds a concrete task prompt from the source_artifact, and spawns a
bounded subagent via the nanobot SubagentManager.

Treats "blocked/local_executor_unavailable" results as NOT handled —
those are created by the coordinator materializer when no executor is
configured, and should be superseded by a real LLM bridge run.
"""
import asyncio
import json
import os
import time
from pathlib import Path

from nanobot.agent.subagent import SubagentManager
from nanobot.bus.queue import MessageBus
from nanobot.cli.commands import _make_provider
from nanobot.config.loader import load_config, set_config_path

STATE_DIR = Path(os.environ.get('STATE_DIR', '/var/lib/eeepc-agent/self-evolving-agent/state'))
TARGET_WORKSPACE = Path(os.environ.get('TARGET_WORKSPACE', '/opt/eeepc-agent/runtimes/self-evolving-agent/current'))
CONFIG_PATH = Path(os.environ.get('NANOBOT_CONFIG_PATH', '/run/user/1001/nanobot-eeepc/config.json'))
BRIDGE_STATE_DIR = Path(os.environ.get('SUBAGENT_BRIDGE_STATE_DIR', str(STATE_DIR / 'subagent_bridge')))
BRIDGE_ENABLED = os.environ.get('SUBAGENT_BRIDGE_ENABLED', '1').strip().lower() in {'1', 'true', 'yes', 'on'}
FORCE_PROFILE = os.environ.get('SUBAGENT_BRIDGE_FORCE_PROFILE', '').strip()
FORCE_BUDGET = os.environ.get('SUBAGENT_BRIDGE_FORCE_BUDGET', '').strip()
BRIDGE_MODEL = os.environ.get('SUBAGENT_BRIDGE_MODEL', 'cl/gpt-5.4-mini').strip() or 'cl/gpt-5.4-mini'


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


def build_task(req: dict, goal_text: str, report_source: str) -> str:
    """Build a concrete task prompt for the subagent from the request payload."""
    task_title = req.get('task_title') or req.get('semantic_task_id') or 'subagent review task'
    request_id = req.get('request_id') or req.get('verification_task_id') or '?'
    cycle_id = req.get('cycle_id') or '?'
    goal_id = req.get('goal_id') or '?'
    source_artifact = req.get('source_artifact') or ''

    # Read the source artifact content inline so subagent has concrete data
    artifact_content = ''
    if source_artifact and Path(source_artifact).exists():
        try:
            raw = Path(source_artifact).read_text(encoding='utf-8')
            artifact_content = raw[:4000]
            if len(raw) > 4000:
                artifact_content += '\n... [truncated]'
        except Exception as e:
            artifact_content = f'[could not read artifact: {e}]'
    else:
        artifact_content = '[source artifact not found or not specified]'

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
        '## Your instructions',
        'You MUST take a concrete action in this session. Do not return a review only.',
        '',
        '1. Read the source artifact above.',
        '2. If it is metadata-only (no real file change, no commit, no measurable improvement):',
        '   - Pick the smallest concrete action that advances Vector 1 or Vector 2.',
        '   - Write or edit the file now using write_file or edit_file.',
        '   - Run a smoke test: exec("python3 -m pytest tests/ -x -q") — ignore if tests dir absent.',
        '   - Commit: exec("git add <file> && git commit -m \"<message>\"")',
        '   - Append one line to memory/HISTORY.md using edit_file or write_file.'
        '3. If the artifact contains a real improvement: verify it, confirm it works, log to HISTORY.md.',
        '4. Return a structured summary with: findings[], action_taken (what you actually did), files_changed[], concrete_next_action.',
        '',
        'Use your tools: read_file, write_file, edit_file, list_dir, exec.',
        'You have up to 15 iterations. Use them.',
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
    safe_id = request_id.replace('/', '_')[:120]
    handled_marker = BRIDGE_STATE_DIR / f'handled_{safe_id}.txt'
    if handled_marker.exists():
        print('already_handled')
        return 0

    goal_text = (
        # Prefer goal_text.json (human-readable mission statement)
        (load_json(STATE_DIR / 'goals' / 'goal_text.json') or {}).get('text')
        or (goals.get('goals') or {}).get(goal_id, {}).get('text')
        or goal_id
    )
    subagent_policy = (goals.get('goals') or {}).get(goal_id, {}).get('subagent_policy') or {}
    profile = FORCE_PROFILE or req.get('profile') or subagent_policy.get('preferred_profile') or 'bounded_execution'
    budget_class = FORCE_BUDGET or subagent_policy.get('budget_class') or req.get('budget') or 'standard'
    gate_open = approval_open()
    mode_at_start = 'auto' if gate_open else 'strict'

    task = build_task(req, goal_text, report_source)

    set_config_path(CONFIG_PATH)
    config = load_config(CONFIG_PATH)
    config.agents.defaults.model = BRIDGE_MODEL
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
    )

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
        await asyncio.gather(*list(mgr._running_tasks.values()), return_exceptions=True)

    handled_marker.write_text(str(req_path), encoding='utf-8')
    latest = TARGET_WORKSPACE / '.nanobot' / 'subagents' / 'latest.json'
    print(latest)

    # Auto-push any new commits in TARGET_WORKSPACE to origin
    import subprocess as _sp
    _repo = str(TARGET_WORKSPACE)
    _git = ['git', '-c', f'safe.directory={_repo}', '-C', _repo]
    _ahead = _sp.run(_git + ['rev-list', '--count', 'origin/main..HEAD'],
                     capture_output=True, text=True).stdout.strip()
    if _ahead and _ahead != '0':
        _push = _sp.run(_git + ['push', 'origin', 'main'],
                        capture_output=True, text=True)
        if _push.returncode == 0:
            print(f'auto-push: pushed {_ahead} commit(s) to origin/main')
        else:
            print(f'auto-push failed: {_push.stderr.strip()[:200]}')

    return 0


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
