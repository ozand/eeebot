#!/usr/bin/env bash
set -euo pipefail

ROOT_DEFAULT="/home/ozand/herkoot/Projects/nanobot"
ROOT="${NANOBOT_RUNTIME_ROOT:-$ROOT_DEFAULT}"
cd "$ROOT"
export PYTHONPATH=.
: "${NANOBOT_WORKSPACE:=/home/ozand/herkoot/Projects/nanobot/workspace}"
: "${NANOBOT_RUNTIME_STATE_SOURCE:=workspace_state}"
exec python3 app/main.py
