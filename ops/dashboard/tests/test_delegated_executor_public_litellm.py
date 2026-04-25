from __future__ import annotations

import json
import importlib.util
from pathlib import Path


def _load_controller():
    script_path = Path(__file__).resolve().parents[1] / 'scripts' / 'consume_delegated_executor_requests.py'
    spec = importlib.util.spec_from_file_location('consume_delegated_executor_requests', script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_delegated_executor_request_uses_public_litellm_metadata_without_legacy_coder_model(tmp_path: Path, monkeypatch) -> None:
    controller = _load_controller()
    root = tmp_path / 'dashboard'
    monkeypatch.setattr(controller, 'ROOT', root)
    monkeypatch.setattr(controller, 'QUEUE_PATH', root / 'control' / 'execution_queue.json')
    monkeypatch.setattr(controller, 'REQUEST_DIR', root / 'control' / 'delegated_executor_requests')
    monkeypatch.setattr(controller, 'DISPATCH_DIR', root / 'control' / 'pi_dev_dispatches')
    monkeypatch.setattr(controller, 'LATEST_REQUEST_PATH', root / 'control' / 'delegated_executor_request.json')

    dispatch_path = root / 'control' / 'pi_dev_dispatches' / 'dispatch.json'
    dispatch_path.parent.mkdir(parents=True, exist_ok=True)
    dispatch_path.write_text(json.dumps({
        'dispatch_status': 'pi_dev_dispatch_ready',
        'runnable_command': 'pi --provider hermes_pi_qwen --model gpt-5.3-codex',
    }), encoding='utf-8')
    controller.QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
    controller.QUEUE_PATH.write_text(json.dumps({'tasks': [{
        'status': 'pi_dev_dispatch_ready',
        'active_goal': 'goal-public-litellm',
        'pi_dev_dispatch_path': str(dispatch_path),
    }]}), encoding='utf-8')

    controller.main()

    payload = json.loads(controller.LATEST_REQUEST_PATH.read_text(encoding='utf-8'))
    serialized = json.dumps(payload)
    assert 'coder-model' not in serialized
    assert payload['pi_dev_executor']['provider'] == 'hermes_pi_qwen'
    assert payload['pi_dev_executor']['model'] == 'gpt-5.3-codex'
    assert payload['pi_dev_executor']['base_url'] == 'https://litellm.ayga.tech:9443/v1'
    assert payload['pi_dev_executor']['auth'] == 'configured_out_of_band_redacted'
    assert 'sk-' not in serialized
