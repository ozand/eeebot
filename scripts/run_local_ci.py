#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

repo_root = Path(os.environ.get('NANOBOT_REPO_ROOT', '/home/ozand/herkoot/Projects/nanobot'))
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from nanobot.runtime.local_ci import write_local_ci_result, write_local_ci_state_summary

workspace = Path(os.environ.get('NANOBOT_WORKSPACE', '/home/ozand/herkoot/Projects/nanobot/workspace'))
command_text = os.environ.get('NANOBOT_LOCAL_CI_COMMAND', 'python3 -m pytest tests/test_autoevolve.py tests/test_autoevolve_guards.py tests/test_autoevolve_commit.py tests/test_autoevolve_state.py tests/test_failure_learning_feedback.py -q')
command = ['bash', '-lc', command_text]
proc = subprocess.run(command, cwd=repo_root, text=True, capture_output=True)
summary = ('PASS' if proc.returncode == 0 else 'FAIL') + f' exit={proc.returncode}'
if proc.stdout.strip():
    summary += f' | {proc.stdout.strip().splitlines()[-1]}'
write_local_ci_result(workspace=workspace, command=command, exit_code=proc.returncode, output=(proc.stdout or '') + '\n' + (proc.stderr or ''), summary=summary)
write_local_ci_state_summary(workspace=workspace)
raise SystemExit(proc.returncode)
