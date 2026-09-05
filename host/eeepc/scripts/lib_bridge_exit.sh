# Exit-status classes of the oneshot bridge, shared by the deploy script's two
# rollback points (#1303): the remote activation `systemctl restart` and the
# local post-deploy health gate. Sourced, never executed.
#
# The bridge says with its exit status what kind of run it had (#1280):
#   0  ran a cycle to a recorded outcome
#   3  EXIT_EXECUTOR_LLM_ERROR — the process imported, started, ran a cycle,
#      could not reach its model, recorded the cycle `blocked` and left the
#      request pending. This is "this one cycle could not reach the gateway",
#      a transient with a few-percent base rate per cycle, NOT "this release
#      cannot run". It must still advance the failure streak (that is the
#      honesty #1280 added), and it must NOT roll a deploy back.
#   anything else non-zero (1 internal error, 4 system-prompt overflow, 203
#   missing interpreter, signals) — the release, its unit or its host is broken.
#
# Keep the value equal to nanobot/runtime/bridge.py::EXIT_EXECUTOR_LLM_ERROR;
# tests/test_deploy_activation_exit_class.py pins the two together.
BRIDGE_EXIT_EXECUTOR_LLM_ERROR=3
# #1300: the strict ContextBuilder could not fit every critical AGENTS.md
# section under the system-prompt cap. A property of the release plus the
# instance's AGENTS.md, not a transient: stays a genuine activation failure
# and rolls back — but the rollback must name itself and its remedy.
BRIDGE_EXIT_SYSTEM_PROMPT_OVERFLOW=4

# describe_bridge_exit_status <ExecMainStatus>
# One line naming a known status and what to do about it; empty for others.
describe_bridge_exit_status() {
  case "${1:-}" in
    "$BRIDGE_EXIT_EXECUTOR_LLM_ERROR")
      echo "EXIT_EXECUTOR_LLM_ERROR: the executor could not reach its model this cycle (#1280)" ;;
    "$BRIDGE_EXIT_SYSTEM_PROMPT_OVERFLOW")
      echo "EXIT_SYSTEM_PROMPT_OVERFLOW: the executor prompt cannot hold every critical AGENTS.md section (#1300) — the instance AGENTS.md must carry '<!-- prompt-fit: droppable -->' markers (ozand/eeebot-self-evolving#186) before this release can run; details in the cycle's system_prompt ledger row and the bridge journal" ;;
    *) echo "" ;;
  esac
}

# classify_bridge_run <systemctl-restart-rc> <Result> <ExecMainStatus>
# Prints exactly one word:
#   ok         the run exited 0
#   transport  the run exited EXIT_EXECUTOR_LLM_ERROR (Result=exit-code)
#   failed     any other failure, including signals and an unreadable status
classify_bridge_run() {
  local rc="${1:-1}" result="${2:-}" status="${3:-}"
  if [ "$rc" = "0" ]; then
    echo ok
    return 0
  fi
  if [ "$result" = "exit-code" ] && [ "$status" = "$BRIDGE_EXIT_EXECUTOR_LLM_ERROR" ]; then
    echo transport
    return 0
  fi
  echo failed
  return 0
}
