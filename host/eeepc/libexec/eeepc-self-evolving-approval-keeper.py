#!/usr/bin/env python3
import json
import os
import time
from pathlib import Path

STATE_DIR = Path(os.environ.get('STATE_DIR', '/var/lib/eeepc-agent/self-evolving-agent/state'))
ENABLED = os.environ.get('APPROVAL_KEEPER_ENABLED', '1').strip().lower() in {'1','true','yes','on'}
TTL_SECONDS = max(300, int(os.environ.get('APPROVAL_KEEPER_TTL_SECONDS', '7200')))
REASON = os.environ.get('APPROVAL_KEEPER_REASON', 'autonomous_apply_window')

approvals_dir = STATE_DIR / 'approvals'
approvals_dir.mkdir(parents=True, exist_ok=True)
gate_file = approvals_dir / 'apply.ok'

if not ENABLED:
    if gate_file.exists():
        gate_file.unlink()
    print('disabled')
    raise SystemExit(0)

payload = {
    'expires_at_epoch': int(time.time()) + TTL_SECONDS,
    'managed_by': 'eeepc-self-evolving-approval-keeper',
    'reason': REASON,
}
gate_file.write_text(json.dumps(payload, indent=2), encoding='utf-8')
print(gate_file)
print(json.dumps(payload))
