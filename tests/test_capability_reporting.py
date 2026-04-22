import json
from pathlib import Path

from nanobot.runtime.state import load_runtime_state


def test_runtime_state_exposes_capabilities_snapshot(tmp_path: Path):
    state = tmp_path / 'state'
    (state / 'goals').mkdir(parents=True)
    (state / 'goals' / 'active.json').write_text(json.dumps({'active_goal': 'goal-bootstrap'}), encoding='utf-8')
    (state / 'reports').mkdir(parents=True)
    (state / 'reports' / 'evolution-20260422T000000Z.json').write_text(json.dumps({'result_status': 'BLOCK'}), encoding='utf-8')
    (state / 'outbox').mkdir(parents=True)
    (state / 'outbox' / 'latest.json').write_text(json.dumps({'approval_gate': {'state': 'missing'}, 'next_hint': 'approval gate missing; refresh manually'}), encoding='utf-8')
    (state / 'experiments').mkdir(parents=True)
    (state / 'experiments' / 'latest.json').write_text(json.dumps({'budget': {'max_requests': 2, 'max_tool_calls': 12, 'max_timeout_seconds': 10}, 'budget_used': {'requests': 2, 'tool_calls': 12, 'elapsed_seconds': 10}}), encoding='utf-8')
    (state / 'subagents').mkdir(parents=True)
    (state / 'subagents' / 'sub-1.json').write_text(json.dumps({'goal_id': 'goal-bootstrap', 'cycle_id': 'cycle-1', 'current_task_id': 'record-reward', 'report_path': '/workspace/state/reports/evolution-1.json', 'status': 'ok', 'task_reward_signal': {'value': 1.0}, 'task_feedback_decision': {'mode': 'stable'}}), encoding='utf-8')

    runtime = load_runtime_state(tmp_path)
    runtime['memory_discipline'] = {'state': 'active', 'reason': 'system_prompt_cap_and_media_guard'}
    runtime['operator_boost'] = {'enabled': True, 'model': 'boost-model', 'reasoning_effort': 'high', 'max_tokens': 999}
    caps = runtime['capabilities']
    assert caps['bounded_apply']['state'] == 'blocked'
    assert caps['bounded_apply']['reason'] == 'approval_gate_missing'
    assert caps['runtime_state']['state'] == 'available'
    assert caps['runtime_state']['reason'] == 'loaded'
    assert caps['memory_discipline']['state'] == 'active'
    assert caps['memory_discipline']['reason'] == 'system_prompt_cap_and_media_guard'
    assert caps['cycle_budget']['state'] == 'degraded'
    assert caps['cycle_budget']['reason'] == 'requests_at_limit,tool_calls_at_limit,timeout_at_limit'
    corr = runtime['subagent_correlation']
    assert corr['goal_id'] == 'goal-bootstrap'
    assert corr['cycle_id'] == 'cycle-1'
    assert corr['current_task_id'] == 'record-reward'
