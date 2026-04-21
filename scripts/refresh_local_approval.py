#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import time
from pathlib import Path

WORKSPACE = Path(os.getenv('NANOBOT_WORKSPACE', '/home/ozand/herkoot/Projects/nanobot/workspace')).expanduser()
TTL_SECONDS = int(os.getenv('NANOBOT_LOCAL_APPROVAL_TTL_SECONDS', '900'))
APPROVAL = WORKSPACE / 'state' / 'approvals' / 'apply.ok'
APPROVAL.parent.mkdir(parents=True, exist_ok=True)
payload = {
    'expires_at_epoch': int(time.time()) + TTL_SECONDS,
    'source': 'eeebot-local-approval-keeper',
    'ttl_seconds': TTL_SECONDS,
}
APPROVAL.write_text(json.dumps(payload, indent=2) + '\n', encoding='utf-8')
print(str(APPROVAL))
