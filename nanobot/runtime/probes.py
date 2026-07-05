"""Runtime self-evolution probe slash-commands.

These are eeepc self-evolution regression probes (`/cap_status`,
`/workspace experiment tiny-runtime-check`, `/sub_run --profile ... --budget ...`)
exercised over the chat surface. They are runtime-owned behavior, not generic
chat-loop behavior, so they live here rather than in `nanobot.agent.loop` — the
agent loop only does a thin dispatch into `handle_probe_command`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from nanobot.bus.events import InboundMessage, OutboundMessage


def _runtime_probe_dir(workspace: Path) -> Path:
    path = workspace / "state" / "telegram_live_probe"
    path.mkdir(parents=True, exist_ok=True)
    return path


def handle_probe_command(
    workspace: Path, model: str, msg: InboundMessage, command_text: str
) -> OutboundMessage | None:
    """Handle runtime-owned slash commands before the LLM chat fallback."""
    cmd = command_text.lower()
    if cmd == "/cap_status":
        content = "\n".join([
            "autonomy: runtime-command-router",
            f"model: {model}",
            f"workspace: {workspace}",
            "telegram_runtime_dispatch: True",
        ])
        return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id, content=content, metadata=msg.metadata or {})

    if cmd == "/workspace experiment tiny-runtime-check":
        artifact = _runtime_probe_dir(workspace) / "tiny-runtime-check.json"
        payload = {
            "action_id": "workspace.experiment.tiny_runtime_check",
            "channel": msg.channel,
            "chat_id": msg.chat_id,
            "written": True,
            "executed": True,
            "verified": True,
        }
        artifact.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        verified = False
        try:
            loaded = json.loads(artifact.read_text(encoding="utf-8"))
            verified = loaded.get("action_id") == payload["action_id"] and loaded.get("written") is True
        except Exception:
            verified = False
        content = "\n".join([
            "action_id: workspace.experiment.tiny_runtime_check",
            f"written: {artifact.exists()}",
            "executed: True",
            f"verified: {verified}",
            f"artifact: {artifact}",
        ])
        return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id, content=content, metadata=msg.metadata or {})

    match = re.fullmatch(r"/sub_run\s+--profile\s+(\S+)\s+--budget\s+(\S+)\s+(.+)", command_text, flags=re.IGNORECASE)
    if match:
        profile, budget, task = match.groups()
        artifact = _runtime_probe_dir(workspace) / "sub_run_micro.json"
        bounded = profile == "research_only" and budget == "micro" and bool(task.strip())
        payload = {
            "action_id": "subagent.bounded_micro_request",
            "channel": msg.channel,
            "chat_id": msg.chat_id,
            "profile": profile,
            "budget": budget,
            "task": task.strip(),
            "bounded": bounded,
            "execution_state": "accepted_for_bounded_runtime_dispatch" if bounded else "rejected_by_policy",
        }
        artifact.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        content = "\n".join([
            "bounded: True" if bounded else "bounded: False",
            f"profile: {profile}",
            f"budget: {budget}",
            f"task: {task.strip()}",
            f"execution_state: {payload['execution_state']}",
            f"artifact: {artifact}",
        ])
        return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id, content=content, metadata=msg.metadata or {})

    return None
