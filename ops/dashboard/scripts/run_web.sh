#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
export PYTHONPATH=src
: "${NANOBOT_EEEPC_SUDO_PASSWORD:?Set NANOBOT_EEEPC_SUDO_PASSWORD first}"
exec python3 -m nanobot_ops_dashboard serve --host 0.0.0.0 --port 8787
