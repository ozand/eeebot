from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import DashboardConfig
from .storage import insert_collection, upsert_event


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def _safe_json_load(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return None


def _build_ssh_command(cfg: DashboardConfig, remote_command: str) -> list[str]:
    if cfg.eeepc_sudo_password:
        remote_command = f"printf '%s\\n' '{cfg.eeepc_sudo_password}' | sudo -S -p '' {remote_command}"
    return [
        'ssh', '-F', '/home/ozand/.ssh/config', '-i', str(cfg.eeepc_ssh_key), '-o', 'IdentitiesOnly=yes',
        cfg.eeepc_ssh_host,
        remote_command,
    ]



def _run_ssh_json(cfg: DashboardConfig, remote_path: str) -> dict[str, Any] | None:
    cmd = _build_ssh_command(cfg, f"cat {remote_path}")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=True)
        return json.loads(proc.stdout)
    except Exception:
        return None



def _run_ssh_lines(cfg: DashboardConfig, command: str) -> list[str]:
    cmd = _build_ssh_command(cfg, command)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=True)
        return [line for line in proc.stdout.splitlines() if line.strip()]
    except Exception:
        return []

def _normalize_repo_state(repo_root: Path) -> dict[str, Any]:
    workspace = repo_root / 'workspace'
    state_root = workspace / 'state'
    if not state_root.exists():
        git_head = None
        try:
            proc = subprocess.run(
                ['git', '-C', str(repo_root), 'rev-parse', '--short', 'HEAD'],
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            )
            git_head = proc.stdout.strip() or None
        except Exception:
            git_head = None
        events = []
        if git_head:
            events.append({
                'event_type': 'deployment',
                'identity_key': git_head,
                'title': f'repo HEAD {git_head}',
                'status': 'present',
                'detail': {'repo_root': str(repo_root)},
            })
        return {
            'source': 'repo',
            'status': 'unknown',
            'active_goal': None,
            'approval_gate': None,
            'gate_state': None,
            'report_source': None,
            'outbox_source': None,
            'artifact_paths': [],
            'promotion_summary': None,
            'promotion_candidate_path': None,
            'promotion_decision_record': None,
            'promotion_accepted_record': None,
            'events': events,
            'raw': {'repo_root': str(repo_root), 'git_head': git_head},
        }
    from nanobot.runtime.state import load_runtime_state
    runtime = load_runtime_state(workspace)
    return {
        'source': 'repo',
        'status': runtime.get('runtime_status'),
        'active_goal': runtime.get('active_goal'),
        'approval_gate': json.dumps(runtime.get('approval_gate')) if runtime.get('approval_gate') is not None else None,
        'gate_state': runtime.get('approval_gate_state'),
        'report_source': runtime.get('report_path'),
        'outbox_source': runtime.get('outbox_path'),
        'artifact_paths': runtime.get('artifact_paths') or [],
        'promotion_summary': runtime.get('promotion_summary'),
        'promotion_candidate_path': runtime.get('promotion_candidate_path'),
        'promotion_decision_record': runtime.get('promotion_decision_record'),
        'promotion_accepted_record': runtime.get('promotion_accepted_record'),
        'events': _repo_events(runtime),
        'raw': runtime,
    }


def _repo_events(runtime: dict[str, Any]) -> list[dict[str, Any]]:
    events = []
    if runtime.get('report_path'):
        events.append({
            'event_type': 'cycle',
            'identity_key': runtime.get('report_path'),
            'title': runtime.get('active_goal') or 'unknown goal',
            'status': runtime.get('runtime_status') or 'unknown',
            'detail': {
                'report_source': runtime.get('report_path'),
                'artifact_paths': runtime.get('artifact_paths') or [],
                'promotion_summary': runtime.get('promotion_summary'),
            },
        })
    if runtime.get('promotion_candidate_id'):
        events.append({
            'event_type': 'promotion',
            'identity_key': runtime.get('promotion_candidate_id'),
            'title': runtime.get('promotion_summary') or runtime.get('promotion_candidate_id'),
            'status': runtime.get('decision') or runtime.get('review_status') or 'unknown',
            'detail': {
                'candidate_path': runtime.get('promotion_candidate_path'),
                'decision_record': runtime.get('promotion_decision_record'),
                'accepted_record': runtime.get('promotion_accepted_record'),
            },
        })
    return events


def _normalize_eeepc_payloads(cfg: DashboardConfig, outbox: dict[str, Any], goals: dict[str, Any]) -> dict[str, Any]:
    active_goal = (outbox.get('goal') or {}).get('goal_id') or goals.get('active_goal_id')
    approval = ((outbox.get('capability_gate') or {}).get('approval')) if isinstance(outbox.get('capability_gate'), dict) else None
    artifact_paths = (((outbox.get('goal') or {}).get('follow_through') or {}).get('artifact_paths')) or []
    events = []
    source_report = outbox.get('source')
    if source_report:
        events.append({
            'event_type': 'cycle',
            'identity_key': source_report,
            'title': active_goal or 'unknown goal',
            'status': outbox.get('status') or 'unknown',
            'detail': {
                'report_source': source_report,
                'artifact_paths': artifact_paths,
                'approval': approval,
            },
        })
    return {
        'source': 'eeepc',
        'status': outbox.get('status'),
        'active_goal': active_goal,
        'approval_gate': json.dumps(approval) if approval is not None else None,
        'gate_state': (approval or {}).get('reason') if isinstance(approval, dict) else None,
        'report_source': source_report,
        'outbox_source': f"{cfg.eeepc_state_root}/outbox/report.index.json",
        'artifact_paths': artifact_paths,
        'promotion_summary': None,
        'promotion_candidate_path': None,
        'promotion_decision_record': None,
        'promotion_accepted_record': None,
        'events': events,
        'raw': {'outbox': outbox, 'goals': goals},
    }



def _normalize_eeepc_state(cfg: DashboardConfig) -> dict[str, Any]:
    state_root = cfg.eeepc_state_root
    outbox = _run_ssh_json(cfg, f"{state_root}/outbox/report.index.json") or {}
    goals = _run_ssh_json(cfg, f"{state_root}/goals/registry.json") or {}
    return _normalize_eeepc_payloads(cfg, outbox, goals)


def _persist(cfg: DashboardConfig, normalized: dict[str, Any]) -> None:
    collected_at = _utc_now()
    insert_collection(cfg.db_path, {
        'collected_at': collected_at,
        'source': normalized['source'],
        'status': normalized.get('status'),
        'active_goal': normalized.get('active_goal'),
        'approval_gate': normalized.get('approval_gate'),
        'gate_state': normalized.get('gate_state'),
        'report_source': normalized.get('report_source'),
        'outbox_source': normalized.get('outbox_source'),
        'artifact_paths_json': json.dumps(normalized.get('artifact_paths') or []),
        'promotion_summary': normalized.get('promotion_summary'),
        'promotion_candidate_path': normalized.get('promotion_candidate_path'),
        'promotion_decision_record': normalized.get('promotion_decision_record'),
        'promotion_accepted_record': normalized.get('promotion_accepted_record'),
        'raw_json': json.dumps(normalized.get('raw') or {}, ensure_ascii=False),
    })
    for event in normalized.get('events', []):
        upsert_event(cfg.db_path, {
            'collected_at': collected_at,
            'source': normalized['source'],
            'event_type': event['event_type'],
            'identity_key': event['identity_key'],
            'title': event.get('title'),
            'status': event.get('status'),
            'detail_json': json.dumps(event.get('detail') or {}, ensure_ascii=False),
        })


def collect_once(cfg: DashboardConfig) -> dict[str, Any]:
    repo = _normalize_repo_state(cfg.nanobot_repo_root)
    eeepc = _normalize_eeepc_state(cfg)
    _persist(cfg, repo)
    _persist(cfg, eeepc)
    return {
        'repo_status': repo.get('status'),
        'repo_goal': repo.get('active_goal'),
        'eeepc_status': eeepc.get('status'),
        'eeepc_goal': eeepc.get('active_goal'),
    }



def run_poll_loop(cfg: DashboardConfig, iterations: int | None = None) -> None:
    count = 0
    while True:
        collect_once(cfg)
        count += 1
        if iterations is not None and count >= iterations:
            return
        time.sleep(cfg.poll_interval_seconds)
