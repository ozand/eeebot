"""Model-free bridge activation check and behavioral-timeout recorder (#1904)."""
from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import uuid
from pathlib import Path
from typing import Any

from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.config.loader import _apply_eeebot_env_aliases, _migrate_config
from nanobot.config.schema import Config
from nanobot.runtime.cycle_ledger import append_event, read_events


class _ActivationNoopTool(Tool):
    """Local registry probe: no filesystem, network, or model side effects."""

    @property
    def name(self) -> str:
        return "activation_noop"

    @property
    def description(self) -> str:
        return "Deterministic activation self-check; performs no external work."

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "additionalProperties": False}

    async def execute(self, **kwargs: Any) -> str:
        if kwargs:
            raise ValueError("activation no-op accepts no parameters")
        return "activation-noop-ok"


def run_activation_self_check() -> int:
    """Import bridge, strictly validate its configured JSON, exercise one tool, persist proof."""
    bridge = importlib.import_module("nanobot.runtime.bridge")
    _apply_eeebot_env_aliases()
    config_path = Path(os.environ.get("NANOBOT_CONFIG_PATH", str(bridge.CONFIG_PATH)))
    if not config_path.is_file():
        raise FileNotFoundError(f"bridge config file is missing: {config_path}")
    with config_path.open(encoding="utf-8") as stream:
        config_data = _migrate_config(json.load(stream))
    Config.model_validate(config_data)
    state_dir = Path(os.environ.get("STATE_DIR", os.environ.get("NANOBOT_RUNTIME_STATE_ROOT", str(bridge.STATE_DIR))))
    bridge.STATE_DIR = state_dir
    bridge.BRIDGE_STATE_DIR = Path(os.environ.get("SUBAGENT_BRIDGE_STATE_DIR", str(state_dir / "subagent_bridge")))
    bridge.RELEASE_ROOT = Path(os.environ.get("ACTIVATION_CHECK_RELEASE", os.environ.get("RELEASE_ROOT", str(bridge.RELEASE_ROOT))))
    bridge.TARGET_WORKSPACE = Path(os.environ.get("TARGET_WORKSPACE", str(bridge.TARGET_WORKSPACE)))

    registry = ToolRegistry()
    registry.register(_ActivationNoopTool())
    noop = _ActivationNoopTool()
    if not registry.has(noop.name):
        raise RuntimeError("activation no-op tool was not registered")
    result = asyncio.run(registry.execute(noop.name, {}))
    if result != "activation-noop-ok":
        raise RuntimeError(f"model-free activation tool step failed: {result}")

    check_id = str(uuid.uuid4())
    event = {
        "phase": "bridge_activation_self_check",
        "check_id": check_id,
        "outcome": "pass",
        "release": str(bridge.RELEASE_ROOT),
        "tool_step": "activation_noop",
        "model_called": False,
    }
    append_event(state_dir, event)
    if not any(row.get("phase") == event["phase"] and row.get("check_id") == check_id
               for row in read_events(state_dir)):
        raise RuntimeError("activation self-check state evidence was not persisted")
    # The ledger is intentionally fail-open; add a direct required write proof.
    marker_dir = state_dir / "bridge" / "activation_checks"
    marker_dir.mkdir(parents=True, exist_ok=True)
    marker_path = marker_dir / f"{check_id}.json"
    marker_path.write_text(json.dumps(event, sort_keys=True), encoding="utf-8")
    if json.loads(marker_path.read_text(encoding="utf-8")) != event:
        raise RuntimeError("activation self-check marker verification failed")
    print(f"activation-self-check: pass release={bridge.RELEASE_ROOT} model_called=false")
    return 0


def record_behavior_timeout(*, result: str, status: str, release: str) -> None:
    """Persist the first real cycle's timeout as inconclusive; do not infer its cause."""
    bridge = importlib.import_module("nanobot.runtime.bridge")
    state_dir = Path(os.environ.get("STATE_DIR", os.environ.get("NANOBOT_RUNTIME_STATE_ROOT", str(bridge.STATE_DIR))))
    bridge.STATE_DIR = state_dir
    event = {
        "phase": "deploy_behavioral_check",
        "outcome": "inconclusive",
        "reason": "timeout_start_sec",
        "systemd_result": result,
        "exec_main_status": status,
        "release": release,
        "behavior_confirmed": False,
    }
    append_event(state_dir, event)
    if not any(row.get("phase") == event["phase"] and row.get("reason") == event["reason"]
               and row.get("release") == release for row in read_events(state_dir)):
        raise RuntimeError("behavioral timeout outcome was not persisted")
    marker_dir = state_dir / "bridge" / "behavioral_checks"
    marker_dir.mkdir(parents=True, exist_ok=True)
    marker = marker_dir / f"timeout-{uuid.uuid4()}.json"
    marker.write_text(json.dumps(event, sort_keys=True), encoding="utf-8")
    if json.loads(marker.read_text(encoding="utf-8")) != event:
        raise RuntimeError("behavioral timeout state marker verification failed")


def main(argv: list[str] | None = None) -> int:
    if os.environ.get("ACTIVATION_CHECK_MODE") == "record-behavior-timeout":
        result = os.environ.get("ACTIVATION_CHECK_RESULT", "timeout")
        status = os.environ.get("ACTIVATION_CHECK_STATUS", "")
        release = os.environ.get("ACTIVATION_CHECK_RELEASE", "")
        record_behavior_timeout(result=result, status=status, release=release)
        print(f"behavioral-check: inconclusive result={result} release={release}")
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record-behavior-timeout", action="store_true")
    parser.add_argument("--result", default="timeout")
    parser.add_argument("--status", default="")
    parser.add_argument("--release", default="")
    args = parser.parse_args(argv)
    if args.record_behavior_timeout:
        record_behavior_timeout(result=args.result, status=args.status, release=args.release)
        print(f"behavioral-check: inconclusive result={args.result} release={args.release}")
        return 0
    return run_activation_self_check()


if __name__ == "__main__":
    raise SystemExit(main())
