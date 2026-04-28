from __future__ import annotations

import importlib.util
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / 'scripts' / 'verify_eeepc_self_evolving_service_guard.py'


def _load_module():
    spec = importlib.util.spec_from_file_location('verify_eeepc_self_evolving_service_guard', MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_service_guard_rejects_stale_release_paths() -> None:
    guard = _load_module()
    text = '''
[Service]
WorkingDirectory=/opt/eeepc-agent/runtimes/self-evolving-agent/releases/20260324-staged
Environment=POLICY_FILE=/opt/eeepc-agent/runtimes/self-evolving-agent/releases/20260324-staged/config/policy.yaml
ExecStart=/opt/eeepc-agent/runtimes/self-evolving-agent/current/.venv/bin/python -m app.main
'''

    result = guard.evaluate_service_text(text)

    assert result['state'] == 'blocked'
    assert 'working_directory_not_current' in result['reasons']
    assert 'policy_file_pinned_to_release' in result['reasons']


def test_service_guard_accepts_current_symlink_and_empty_policy_file() -> None:
    guard = _load_module()
    text = '''
[Service]
WorkingDirectory=/opt/eeepc-agent/runtimes/self-evolving-agent/current
Environment=POLICY_FILE=
ExecStart=/opt/eeepc-agent/runtimes/self-evolving-agent/current/.venv/bin/python -m app.main
'''

    result = guard.evaluate_service_text(text)

    assert result['state'] == 'healthy'
    assert result['reasons'] == []
