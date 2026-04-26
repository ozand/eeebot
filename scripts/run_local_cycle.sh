#!/usr/bin/env bash
set -euo pipefail

ROOT_DEFAULT_GUARDED="/home/ozand/herkoot/Projects/nanobot/workspace/state/self_evolution/runtime/current"
ROOT_FALLBACK_REPO="${NANOBOT_REPO_ROOT:-/home/ozand/herkoot/Projects/nanobot}"
ROOT="${NANOBOT_RUNTIME_ROOT:-$ROOT_DEFAULT_GUARDED}"
if [[ ! -e "$ROOT" ]]; then
  ROOT="$ROOT_FALLBACK_REPO"
fi
cd "$ROOT"
export PYTHONPATH=.
: "${NANOBOT_WORKSPACE:=/home/ozand/herkoot/Projects/nanobot/workspace}"
: "${NANOBOT_RUNTIME_STATE_SOURCE:=workspace_state}"
set +e
python3 app/main.py
cycle_status=$?
set -e
(
  cd "$ROOT_FALLBACK_REPO"
  PYTHONPATH="$ROOT_FALLBACK_REPO" python3 - <<'PY'
import os
from pathlib import Path
from nanobot.runtime.autoevolve import write_guarded_evolution_state
write_guarded_evolution_state(Path(os.environ['NANOBOT_WORKSPACE']))
PY
) || true
exit "$cycle_status"
