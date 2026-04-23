#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from pathlib import Path
from nanobot.runtime.autoevolve import create_candidate_release

repo_root = Path(os.environ.get('NANOBOT_REPO_ROOT', '/home/ozand/herkoot/Projects/nanobot'))
workspace = Path(os.environ.get('NANOBOT_WORKSPACE', '/home/ozand/herkoot/Projects/nanobot/workspace'))
record = create_candidate_release(repo_root=repo_root, workspace=workspace)
print(json.dumps(record, indent=2, ensure_ascii=False))
