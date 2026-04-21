#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/ozand/herkoot/Projects/nanobot"
cd "$ROOT"
export PYTHONPATH=.
: "${NANOBOT_WORKSPACE:=/home/ozand/herkoot/Projects/nanobot/workspace}"
: "${NANOBOT_RUNTIME_STATE_SOURCE:=workspace_state}"
exec python3 app/main.py
