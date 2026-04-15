#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/ozand/herkoot/Projects/nanobot-ops-dashboard"
cd "$ROOT"
export PYTHONPATH=src
: "${NANOBOT_EEEPC_SUDO_PASSWORD:?Set NANOBOT_EEEPC_SUDO_PASSWORD first}"
exec python3 -m nanobot_ops_dashboard serve --host 127.0.0.1 --port 8787
