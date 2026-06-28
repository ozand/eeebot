from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from nanobot.runtime.coordinator import run_self_evolving_cycle

DEFAULT_RUNTIME_STATE_SOURCE = "host_control_plane"
DEFAULT_RUNTIME_STATE_ROOT = Path("/var/lib/eeepc-agent/self-evolving-agent/state")
DEFAULT_WORKSPACE = Path.cwd()
DEFAULT_TASKS = "Run one bounded self-evolving cycle and persist canonical runtime state."

# LiteLLM proxy env vars — set by /etc/eeepc-agent/litellm.env via systemd drop-in
_LITELLM_BASE_URL = os.environ.get("LITELLM_BASE_URL", "https://litellm.ayga.tech:9443/v1")
_LITELLM_API_KEY = os.environ.get("LITELLM_API_KEY", "")
# Prefixed models route via openai-compatible client pointed at the proxy.
# Default to an/gemini-3.5-flash-low; the old cl/gemini-2.5-flash Cliproxy route
# now returns "unknown provider for model gemini-2.5-flash" (BadGateway).
_LITELLM_MODEL = os.environ.get("LITELLM_MODEL", "an/gemini-3.5-flash-low")
_LITELLM_TIMEOUT = int(os.environ.get("LITELLM_TIMEOUT_S", "45"))

_SYSTEM_PROMPT = """\
You are the eeebot self-evolving runtime running on a constrained eeepc host.

Your mission has two vectors:
1. Self-Optimization on constrained hardware — reduce CPU/RAM/disk waste, inspect available
   hardware (camera, BT, WiFi, mic), improve runtime efficiency, build diagnostics.
2. Owner Utility and Creative Output — terminal dashboards (TUI), workflow helpers,
   research summaries, audio/visual generators, small games, interactive artifacts.

Valid progress requires at least one of:
- A real git commit with a concrete code or config change
- A new or meaningfully improved tool/script/utility
- A measurable reduction in a known failure mode (with evidence)
- A concrete owner-facing artifact: dashboard, TUI, generator, game, utility

Boilerplate artifacts without file changes do NOT count as progress.
Metadata-only materialization artifacts do NOT count as progress.

When given a task, respond with a concrete, specific, actionable proposal:
- Name the exact file(s) to create or change
- Describe the before/after behaviour
- Confirm it is safe and reversible on a weak i386 host
- Keep it small enough to implement in one bounded cycle
"""


async def _call_llm(prompt: str) -> str:
    """Call LiteLLM proxy (openai-compatible) and return the text response.

    Uses openai.AsyncOpenAI directly because the cl/ model prefix is a proxy
    convention that litellm does not recognise as a provider.
    Falls back to echoing the prompt on error so the coordinator still records
    the cycle.
    """
    try:
        import openai  # available in the venv

        client = openai.AsyncOpenAI(
            api_key=_LITELLM_API_KEY,
            base_url=_LITELLM_BASE_URL,
            timeout=_LITELLM_TIMEOUT,
        )
        response = await client.chat.completions.create(
            model=_LITELLM_MODEL,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            max_tokens=1024,
            temperature=0.4,
        )
        content = response.choices[0].message.content or ""
        return content.strip()
    except Exception as exc:
        # Graceful fallback: log error, return the task description unchanged
        # so the coordinator can still record the cycle
        return f"[llm_unavailable: {exc}] {prompt}"


async def _execute_turn(tasks: str) -> str:
    """Execute one self-evolving turn by asking the LLM what to do next."""
    return await _call_llm(tasks)


def _prime_runtime_defaults() -> None:
    source = os.environ.setdefault("NANOBOT_RUNTIME_STATE_SOURCE", DEFAULT_RUNTIME_STATE_SOURCE)
    if source == "host_control_plane":
        os.environ.setdefault("NANOBOT_RUNTIME_STATE_ROOT", str(DEFAULT_RUNTIME_STATE_ROOT))


def _write_strong_reflection_artifact(*, state_root: Path, workspace: Path, summary: str) -> Path:
    """Persist durable strong-reflection evidence for dashboard and audits."""
    reflection_dir = state_root / "strong_reflection"
    reflection_dir.mkdir(parents=True, exist_ok=True)
    recorded_at = datetime.now(timezone.utc).isoformat()
    payload = {
        "schema_version": "strong-reflection-run-v1",
        "recorded_at_utc": recorded_at,
        "workspace": str(workspace),
        "summary": summary,
        "mode": "strong-reflection",
    }
    latest = reflection_dir / "latest.json"
    latest.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    history = reflection_dir / f"reflection-{recorded_at.replace(':', '').replace('+', 'Z')}.json"
    history.write_text(json.dumps({**payload, "latest_path": str(latest)}, indent=2, ensure_ascii=False), encoding="utf-8")
    return latest


def main() -> int:
    previous_source = os.environ.get("NANOBOT_RUNTIME_STATE_SOURCE")
    previous_root = os.environ.get("NANOBOT_RUNTIME_STATE_ROOT")
    try:
        _prime_runtime_defaults()
        workspace_value = os.getenv("NANOBOT_WORKSPACE") or os.getenv("NANOBOT_AGENT_WORKSPACE")
        workspace = Path(workspace_value).expanduser() if workspace_value else DEFAULT_WORKSPACE
        tasks = os.getenv("NANOBOT_SELF_EVOLVING_TASKS", DEFAULT_TASKS)
        summary = asyncio.run(
            run_self_evolving_cycle(
                workspace=workspace,
                tasks=tasks,
                execute_turn=_execute_turn,
            )
        )
        state_root = Path(os.environ.get("NANOBOT_RUNTIME_STATE_ROOT", str(workspace / "state"))).expanduser()
        artifact_path = _write_strong_reflection_artifact(state_root=state_root, workspace=workspace, summary=summary)
        if any(arg == "strong-reflection" for arg in sys.argv[1:]):
            print(f"Strong reflection artifact persisted: {artifact_path}")
        print(summary)
        return 0
    finally:
        if previous_source is None:
            os.environ.pop("NANOBOT_RUNTIME_STATE_SOURCE", None)
        else:
            os.environ["NANOBOT_RUNTIME_STATE_SOURCE"] = previous_source
        if previous_root is None:
            os.environ.pop("NANOBOT_RUNTIME_STATE_ROOT", None)
        else:
            os.environ["NANOBOT_RUNTIME_STATE_ROOT"] = previous_root


if __name__ == "__main__":
    raise SystemExit(main())
