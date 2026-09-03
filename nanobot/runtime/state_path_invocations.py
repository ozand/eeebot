"""Check invocation contracts for selected state-path writers (#1228).

This is an operator/test check, not a hot-path runtime dependency. Systemd
writers are checked with ``list-unit-files`` because disabled timers are absent
from ``list-timers``. Direct bridge writers are represented as per-cycle
invokers and do not need a systemd query.
"""
from __future__ import annotations

import json
import subprocess
from typing import Any, Callable

from nanobot.runtime import state_paths

CommandRunner = Callable[[list[str]], subprocess.CompletedProcess[str]]

WRITER_INVOKERS: dict[str, dict[str, str]] = {
    "action_index": {
        "writer": "nanobot.runtime.action_index:build_action_index",
        "kind": "systemd",
        "unit": "eeebot-action-index.timer",
    },
    "curator": {
        "writer": "nanobot.runtime.knowledge_curator:promote_reflector_recommendations_to_v2",
        "kind": "systemd",
        "unit": "eeebot-knowledge-curator.timer",
    },
    "heldout": {
        "writer": "nanobot.runtime.heldout:_save_results",
        "kind": "systemd",
        "unit": "eeebot-skill-evals.timer",
    },
    "hypotheses": {
        "writer": "nanobot.runtime.hypothesis_backlog:append_hypotheses",
        "kind": "systemd",
        "unit": "eeebot-strategist.timer",
    },
    "ledger": {
        "writer": "nanobot.runtime.cycle_ledger:append_event",
        "kind": "direct",
        "invoker": "nanobot.runtime.bridge:_main_impl_body",
    },
    "llm_calls": {
        "writer": "nanobot.observability.llm_telemetry:record_llm_call",
        "kind": "direct",
        "invoker": "nanobot.runtime.bridge:_main_impl_body",
    },
    "reflector": {
        "writer": "nanobot.runtime.reflector:_append_journal",
        "kind": "systemd",
        "unit": "eeebot-reflector.timer",
    },
    "scorecard": {
        "writer": "nanobot.runtime.scorecard:compute_scorecard",
        "kind": "direct",
        "invoker": "nanobot.runtime.bridge:_main_impl_body",
    },
    "strategist": {
        "writer": "nanobot.runtime.strategist:_record_decision",
        "kind": "systemd",
        "unit": "eeebot-strategist.timer",
    },
    "subagents": {
        "writer": "nanobot.runtime.bridge:_write_bridge_completed_result",
        "kind": "direct",
        "invoker": "nanobot.runtime.bridge:_main_impl_body",
    },
}


def _default_runner(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, timeout=10, check=False)


def _unit_states(runner: CommandRunner) -> tuple[dict[str, tuple[str, str]], bool]:
    """Read timer unit files and return ``(unit -> state/preset, readable)``."""
    command = ["systemctl", "list-unit-files", "--type=timer", "--no-legend", "--no-pager"]
    try:
        result = runner(command)
    except Exception:
        return {}, False
    if result.returncode != 0:
        return {}, False
    units: dict[str, tuple[str, str]] = {}
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0].endswith(".timer"):
            units[fields[0]] = (fields[1], fields[2] if len(fields) >= 3 else "")
    return units, True


def check_writer_invocations(
    runner: CommandRunner | None = None,
    declarations: dict[str, dict[str, str]] = WRITER_INVOKERS,
) -> dict[str, Any]:
    """Report whether each audited declared writer has an active invoker."""
    run = runner or _default_runner
    units: dict[str, tuple[str, str]] = {}
    unit_readable = True
    if any(spec.get("kind") == "systemd" for spec in declarations.values()):
        units, unit_readable = _unit_states(run)

    results: dict[str, dict[str, str]] = {}
    failures: list[str] = []
    for segment, spec in declarations.items():
        writer = spec.get("writer", "")
        if writer not in state_paths.STATE_PATH_WRITERS.get(segment, ()):
            results[segment] = {"status": "invalid", "writer": writer, "reason": "writer is not declared"}
            failures.append(segment)
            continue
        if spec.get("kind") == "direct":
            results[segment] = {
                "status": "per_cycle",
                "writer": writer,
                "invoker": spec.get("invoker", ""),
                "reason": "invoked by the bridge cycle",
            }
            continue
        unit = spec.get("unit", "")
        state, preset = units.get(unit, ("", ""))
        if not unit_readable:
            status, reason = "unavailable", "systemctl list-unit-files failed"
        elif not state:
            status, reason = "absent", "unit not listed by systemctl list-unit-files"
        elif state in {"enabled", "enabled-runtime"}:
            status, reason = "scheduled", "timer is enabled"
        else:
            status, reason = "disabled", f"timer state is {state}"
        results[segment] = {
            "status": status, "writer": writer, "unit": unit, "preset": preset, "reason": reason,
        }
        if status != "scheduled":
            failures.append(segment)
    return {"ok": not failures, "results": results, "failures": failures}


def main() -> int:
    report = check_writer_invocations()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
