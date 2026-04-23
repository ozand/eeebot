#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from pathlib import Path
from nanobot.runtime.autoevolve import health_check_release

workspace = Path(os.environ.get('NANOBOT_WORKSPACE', '/home/ozand/herkoot/Projects/nanobot/workspace'))
max_age = int(os.environ.get('NANOBOT_AUTOEVO_MAX_REPORT_AGE_SECONDS', '600'))
result = health_check_release(workspace=workspace, max_report_age_seconds=max_age)
print(json.dumps(result, indent=2, ensure_ascii=False))
raise SystemExit(0 if result.get('ok') else 1)
