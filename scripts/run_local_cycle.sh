#!/usr/bin/env bash
set -euo pipefail

ROOT_DEFAULT_GUARDED="/home/ozand/herkoot/Projects/nanobot/workspace/state/self_evolution/runtime/current"
ROOT_FALLBACK_REPO="/home/ozand/herkoot/Projects/nanobot"
ROOT="${NANOBOT_RUNTIME_ROOT:-$ROOT_DEFAULT_GUARDED}"
if [[ ! -e "$ROOT" ]]; then
  ROOT="$ROOT_FALLBACK_REPO"
fi
cd "$ROOT"
export PYTHONPATH=.
: "${NANOBOT_WORKSPACE:=/home/ozand/herkoot/Projects/nanobot/workspace}"
: "${NANOBOT_RUNTIME_STATE_SOURCE:=workspace_state}"
exec python3 app/main.py
