from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / 'scripts' / 'eeepc_privileged_rollout_preflight.py'


def _load_module():
    spec = importlib.util.spec_from_file_location('eeepc_privileged_rollout_preflight', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_preflight_reports_blocked_privileged_access_with_latest_readable_report(monkeypatch):
    module = _load_module()
    state_root = '/var/lib/eeepc-agent/self-evolving-agent/state'
    latest_report = f'{state_root}/reports/evolution-20260426T172155Z-cycle-18a55ab2ec15.json'
    report_payload = {
        'result_status': 'PASS',
        'goal_id': 'goal-bootstrap',
        'feedback_decision': None,
        'selected_tasks': 'Record cycle reward [task_id=record-reward]',
        'task_selection_source': 'recorded_current_task',
    }

    def fake_run(args, timeout=20):
        command = args[-1]
        if command == 'hostname; whoami; id':
            return subprocess.CompletedProcess(args, 0, 'debian\nozand\nuid=1000(ozand)\n', '')
        if command == 'sudo -n true':
            return subprocess.CompletedProcess(args, 1, '', 'sudo: a password is required\n')
        if command.startswith('test -x '):
            return subprocess.CompletedProcess(args, 1, '', '')
        if 'outbox/report.index.json' in command:
            return subprocess.CompletedProcess(args, 1, '', '')
        if 'goals/registry.json' in command:
            return subprocess.CompletedProcess(args, 1, '', '')
        if '/reports/evolution-*.json' in command:
            return subprocess.CompletedProcess(args, 0, latest_report + '\n', '')
        if command == f'cat {latest_report}' or command == f'sudo -n cat {latest_report}':
            return subprocess.CompletedProcess(args, 0, json.dumps(report_payload), '')
        raise AssertionError(command)

    monkeypatch.setattr(module, 'run_command', fake_run)

    payload = module.build_preflight(host='eeepc', state_root=state_root, key='/tmp/key')

    assert payload['schema_version'] == 'eeepc-privileged-rollout-preflight-v1'
    assert payload['state'] == 'blocked_privileged_access'
    assert payload['ready'] is False
    assert payload['does_not_mutate_host'] is True
    assert payload['available_partial_proof'] == 'latest_readable_report'
    assert payload['latest_report']['path'] == latest_report
    assert payload['latest_report']['result_status'] == 'PASS'
    assert payload['latest_report']['goal_id'] == 'goal-bootstrap'
    assert payload['latest_report']['feedback_decision_present'] is False
    assert payload['latest_report']['selected_tasks'] == 'Record cycle reward [task_id=record-reward]'
    assert payload['latest_report']['task_selection_source'] == 'recorded_current_task'
    assert set(payload['blocked_capabilities']) == {
        'sudo_noninteractive',
        'execute_opencode_nanobot',
        'read_authority_outbox',
        'read_goal_registry',
    }


def test_preflight_main_always_exits_zero_for_auditable_blocked_state(monkeypatch, capsys):
    module = _load_module()
    monkeypatch.setattr(module, 'build_preflight', lambda **kwargs: {
        'schema_version': 'eeepc-privileged-rollout-preflight-v1',
        'state': 'blocked_privileged_access',
        'ready': False,
    })

    assert module.main(['--host', 'eeepc', '--ssh-key', '/tmp/key', '--json']) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload['state'] == 'blocked_privileged_access'


def test_preflight_reports_ready_when_all_privileged_checks_pass(monkeypatch):
    module = _load_module()
    state_root = '/var/lib/eeepc-agent/self-evolving-agent/state'
    latest_report = f'{state_root}/reports/evolution-ready.json'
    report_payload = {'result_status': 'PASS', 'goal_id': 'goal-bootstrap', 'feedback_decision': {'selected_task_id': 'task-1'}}

    def fake_run(args, timeout=20):
        command = args[-1]
        if command in {'hostname; whoami; id', 'sudo -n true'}:
            return subprocess.CompletedProcess(args, 0, 'ok\n', '')
        if command.startswith('test -x ') or command.startswith('test -r '):
            return subprocess.CompletedProcess(args, 0, '', '')
        if '/reports/evolution-*.json' in command:
            return subprocess.CompletedProcess(args, 0, latest_report + '\n', '')
        if command == f'cat {latest_report}' or command == f'sudo -n cat {latest_report}':
            return subprocess.CompletedProcess(args, 0, json.dumps(report_payload), '')
        raise AssertionError(command)

    monkeypatch.setattr(module, 'run_command', fake_run)

    payload = module.build_preflight(host='eeepc', state_root=state_root, key='/tmp/key')

    assert payload['state'] == 'ready'
    assert payload['ready'] is True
    assert payload['blocked_capabilities'] == []
    assert payload['latest_report']['feedback_decision_present'] is True


def test_preflight_reports_blocked_unreachable_without_running_privileged_checks(monkeypatch):
    module = _load_module()
    commands = []

    def fake_run(args, timeout=20):
        commands.append(args[-1])
        return subprocess.CompletedProcess(args, 255, '', 'host unreachable')

    monkeypatch.setattr(module, 'run_command', fake_run)

    payload = module.build_preflight(host='eeepc', state_root='/state', key='/tmp/key')

    assert payload['state'] == 'blocked_unreachable'
    assert payload['ready'] is False
    assert payload['blocked_capabilities'] == ['ssh_reachability']
    assert commands == ['hostname; whoami; id']


def test_preflight_quotes_user_supplied_remote_paths(monkeypatch):
    module = _load_module()
    commands = []
    injected_state_root = "/state; touch /tmp/SHOULD_NOT_EXIST"
    injected_nanobot = "/bin/nanobot; touch /tmp/SHOULD_NOT_EXIST"
    injected_home = "/home/opencode; touch /tmp/SHOULD_NOT_EXIST"

    def fake_run(args, timeout=20):
        command = args[-1]
        commands.append(command)
        if command == 'hostname; whoami; id':
            return subprocess.CompletedProcess(args, 0, 'ok\n', '')
        return subprocess.CompletedProcess(args, 1, '', '')

    monkeypatch.setattr(module, 'run_command', fake_run)

    module.build_preflight(
        host='eeepc',
        state_root=injected_state_root,
        key='/tmp/key',
        nanobot_path=injected_nanobot,
        opencode_home=injected_home,
    )

    joined = '\n'.join(commands)
    assert "'/state; touch /tmp/SHOULD_NOT_EXIST'" in joined
    assert "'/bin/nanobot; touch /tmp/SHOULD_NOT_EXIST'" in joined
    assert "'/home/opencode; touch /tmp/SHOULD_NOT_EXIST'" in joined
    assert 'test -r /state; touch /tmp/SHOULD_NOT_EXIST' not in joined
    assert 'test -x /bin/nanobot; touch /tmp/SHOULD_NOT_EXIST' not in joined


def test_preflight_uses_sudo_mediated_checks_when_passwordless_sudo_is_available(monkeypatch):
    module = _load_module()
    state_root = '/var/lib/eeepc-agent/self-evolving-agent/state'
    latest_report = f'{state_root}/reports/evolution-ready.json'
    report_payload = {'result_status': 'PASS', 'goal_id': 'goal-bootstrap', 'feedback_decision': {'selected_task_id': 'task-1'}}

    def fake_run(args, timeout=20):
        command = args[-1]
        if command == 'hostname; whoami; id':
            return subprocess.CompletedProcess(args, 0, 'debian\nozand\nuid=1000(ozand)\n', '')
        if command == 'sudo -n true':
            return subprocess.CompletedProcess(args, 0, '', '')
        if command.startswith('test -x ') or command.startswith('test -r '):
            return subprocess.CompletedProcess(args, 1, '', 'permission denied')
        if command.startswith('sudo -n test -x ') or command.startswith('sudo -n test -r ') or command.startswith('sudo -n sh -lc '):
            return subprocess.CompletedProcess(args, 0, '', '')
        if '/reports/evolution-*.json' in command:
            return subprocess.CompletedProcess(args, 0, latest_report + '\n', '')
        if command == f'cat {latest_report}' or command == f'sudo -n cat {latest_report}':
            return subprocess.CompletedProcess(args, 0, json.dumps(report_payload), '')
        raise AssertionError(command)

    monkeypatch.setattr(module, 'run_command', fake_run)

    payload = module.build_preflight(host='eeepc', state_root=state_root, key='/tmp/key')

    assert payload['state'] == 'ready'
    assert payload['ready'] is True
    assert payload['blocked_capabilities'] == []
    assert payload['checks']['opencode_nanobot_executable']['via'] == 'sudo'
    assert payload['checks']['read_authority_outbox']['via'] == 'sudo'
    assert payload['checks']['read_goal_registry']['via'] == 'sudo'
    assert payload['checks']['direct_opencode_nanobot_executable']['ok'] is False


def test_preflight_blocks_exact_sudo_mediated_capability_when_sudo_check_fails(monkeypatch):
    module = _load_module()
    state_root = '/var/lib/eeepc-agent/self-evolving-agent/state'

    def fake_run(args, timeout=20):
        command = args[-1]
        if command == 'hostname; whoami; id':
            return subprocess.CompletedProcess(args, 0, 'ok\n', '')
        if command == 'sudo -n true':
            return subprocess.CompletedProcess(args, 0, '', '')
        if command.startswith('test -x ') or command.startswith('test -r '):
            return subprocess.CompletedProcess(args, 1, '', 'permission denied')
        if command.startswith('sudo -n sh -lc '):
            return subprocess.CompletedProcess(args, 0, '', '')
        if command.startswith('sudo -n test -x '):
            return subprocess.CompletedProcess(args, 0, '', '')
        if 'outbox/report.index.json' in command and command.startswith('sudo -n test -r '):
            return subprocess.CompletedProcess(args, 1, '', 'missing')
        if 'goals/registry.json' in command and command.startswith('sudo -n test -r '):
            return subprocess.CompletedProcess(args, 0, '', '')
        if '/reports/evolution-*.json' in command:
            return subprocess.CompletedProcess(args, 1, '', '')
        raise AssertionError(command)

    monkeypatch.setattr(module, 'run_command', fake_run)

    payload = module.build_preflight(host='eeepc', state_root=state_root, key='/tmp/key')

    assert payload['state'] == 'blocked_privileged_access'
    assert payload['ready'] is False
    assert payload['blocked_capabilities'] == ['read_authority_outbox']
    assert payload['checks']['read_authority_outbox']['via'] == 'sudo'
