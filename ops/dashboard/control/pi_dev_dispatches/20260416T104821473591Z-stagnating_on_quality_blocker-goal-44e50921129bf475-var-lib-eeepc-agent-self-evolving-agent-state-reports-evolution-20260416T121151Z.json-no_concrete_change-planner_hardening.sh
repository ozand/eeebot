#!/usr/bin/env bash
set -euo pipefail
cd /home/ozand/herkoot/Projects/nanobot-ops-dashboard/control/pi_dev_dispatches
export PATH="$HOME/.hermes/node/bin:$PATH"
pi --mode json -p --no-session --no-tools --provider hermes_pi_qwen --model coder-model < "$(dirname "$0")/20260416T104821473591Z-stagnating_on_quality_blocker-goal-44e50921129bf475-var-lib-eeepc-agent-self-evolving-agent-state-reports-evolution-20260416T121151Z.json-no_concrete_change-planner_hardening.prompt.txt"
