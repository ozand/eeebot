#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from pathlib import Path
from nanobot.runtime.autoevolve import commit_and_push_self_evolution

repo_root = Path(os.environ.get('NANOBOT_REPO_ROOT', '/home/ozand/herkoot/Projects/nanobot'))
message = os.environ.get('NANOBOT_AUTOEVO_COMMIT_MESSAGE', 'autoevolve: bounded self-update')
result = commit_and_push_self_evolution(repo_root=repo_root, message=message)
print(json.dumps(result, indent=2, ensure_ascii=False))
