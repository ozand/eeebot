import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

from nanobot.runtime.coordinator import run_self_evolving_cycle


def _read(path: Path):
    return json.loads(path.read_text(encoding='utf-8'))


def test_cycle_writes_control_plane_current_summary(tmp_path: Path):
    approvals_dir = tmp_path / 'state' / 'approvals'
    approvals_dir.mkdir(parents=True)
    (approvals_dir / 'apply.ok').write_text(
        json.dumps({'expires_at_epoch': 2000000000}, indent=2),
        encoding='utf-8',
    )

    execute = AsyncMock(return_value='done')
    asyncio.run(
        run_self_evolving_cycle(
            workspace=tmp_path,
            tasks='summary proof',
            execute_turn=execute,
        )
    )

    summary = _read(tmp_path / 'state' / 'control_plane' / 'current_summary.json')
    assert summary['schema_version'] == 'control-plane-summary-v1'
    assert summary['result_status'] == 'PASS'
    assert summary['approval_gate']['state'] == 'fresh'
    assert summary['task_plan']['current_task_id'] == 'record-reward'
    assert summary['hypotheses']['selected_hypothesis_id'] == 'record-reward'
    assert summary['experiment']['outcome'] == 'keep'
    assert summary['report_index']['status'] == 'PASS'
