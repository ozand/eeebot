import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from nanobot.heartbeat.service import HeartbeatService
from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from nanobot.runtime.coordinator import run_self_evolving_cycle
from nanobot.runtime.state import load_runtime_state


class DummyProvider(LLMProvider):
    def __init__(self, responses: list[LLMResponse]):
        super().__init__()
        self._responses = list(responses)
        self.calls = 0

    async def chat(self, *args, **kwargs) -> LLMResponse:
        self.calls += 1
        if self._responses:
            return self._responses.pop(0)
        return LLMResponse(content="", tool_calls=[])

    def get_default_model(self) -> str:
        return "test-model"


def _read_json(path: str | Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


@pytest.mark.asyncio
async def test_start_is_idempotent(tmp_path) -> None:
    provider = DummyProvider([])

    service = HeartbeatService(
        workspace=tmp_path,
        provider=provider,
        model="openai/gpt-4o-mini",
        interval_s=9999,
        enabled=True,
    )

    await service.start()
    first_task = service._task
    await service.start()

    assert service._task is first_task

    service.stop()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_decide_returns_skip_when_no_tool_call(tmp_path) -> None:
    provider = DummyProvider([LLMResponse(content="no tool call", tool_calls=[])])
    service = HeartbeatService(
        workspace=tmp_path,
        provider=provider,
        model="openai/gpt-4o-mini",
    )

    action, tasks = await service._decide("heartbeat content")
    assert action == "skip"
    assert tasks == ""


@pytest.mark.asyncio
async def test_trigger_now_executes_when_decision_is_run(tmp_path) -> None:
    (tmp_path / "HEARTBEAT.md").write_text("- [ ] do thing", encoding="utf-8")

    provider = DummyProvider([
        LLMResponse(
            content="",
            tool_calls=[
                ToolCallRequest(
                    id="hb_1",
                    name="heartbeat",
                    arguments={"action": "run", "tasks": "check open tasks"},
                )
            ],
        )
    ])

    called_with: list[str] = []

    async def _on_execute(tasks: str) -> str:
        called_with.append(tasks)
        return "done"

    service = HeartbeatService(
        workspace=tmp_path,
        provider=provider,
        model="openai/gpt-4o-mini",
        on_execute=_on_execute,
    )

    result = await service.trigger_now()
    assert result == "done"
    assert called_with == ["check open tasks"]


@pytest.mark.asyncio
async def test_trigger_now_returns_none_when_decision_is_skip(tmp_path) -> None:
    (tmp_path / "HEARTBEAT.md").write_text("- [ ] do thing", encoding="utf-8")

    provider = DummyProvider([
        LLMResponse(
            content="",
            tool_calls=[
                ToolCallRequest(
                    id="hb_1",
                    name="heartbeat",
                    arguments={"action": "skip"},
                )
            ],
        )
    ])

    async def _on_execute(tasks: str) -> str:
        return tasks

    service = HeartbeatService(
        workspace=tmp_path,
        provider=provider,
        model="openai/gpt-4o-mini",
        on_execute=_on_execute,
    )

    assert await service.trigger_now() is None


@pytest.mark.asyncio
async def test_tick_notifies_when_evaluator_says_yes(tmp_path, monkeypatch) -> None:
    """Phase 1 run -> Phase 2 execute -> Phase 3 evaluate=notify -> on_notify called."""
    (tmp_path / "HEARTBEAT.md").write_text("- [ ] check deployments", encoding="utf-8")

    provider = DummyProvider([
        LLMResponse(
            content="",
            tool_calls=[
                ToolCallRequest(
                    id="hb_1",
                    name="heartbeat",
                    arguments={"action": "run", "tasks": "check deployments"},
                )
            ],
        ),
    ])

    executed: list[str] = []
    notified: list[str] = []

    async def _on_execute(tasks: str) -> str:
        executed.append(tasks)
        return "deployment failed on staging"

    async def _on_notify(response: str) -> None:
        notified.append(response)

    service = HeartbeatService(
        workspace=tmp_path,
        provider=provider,
        model="openai/gpt-4o-mini",
        on_execute=_on_execute,
        on_notify=_on_notify,
    )

    async def _eval_notify(*a, **kw):
        return True

    monkeypatch.setattr("nanobot.utils.evaluator.evaluate_response", _eval_notify)

    await service._tick()
    assert executed == ["check deployments"]
    assert notified == ["deployment failed on staging"]


@pytest.mark.asyncio
async def test_tick_suppresses_when_evaluator_says_no(tmp_path, monkeypatch) -> None:
    """Phase 1 run -> Phase 2 execute -> Phase 3 evaluate=silent -> on_notify NOT called."""
    (tmp_path / "HEARTBEAT.md").write_text("- [ ] check status", encoding="utf-8")

    provider = DummyProvider([
        LLMResponse(
            content="",
            tool_calls=[
                ToolCallRequest(
                    id="hb_1",
                    name="heartbeat",
                    arguments={"action": "run", "tasks": "check status"},
                )
            ],
        ),
    ])

    executed: list[str] = []
    notified: list[str] = []

    async def _on_execute(tasks: str) -> str:
        executed.append(tasks)
        return "everything is fine, no issues"

    async def _on_notify(response: str) -> None:
        notified.append(response)

    service = HeartbeatService(
        workspace=tmp_path,
        provider=provider,
        model="openai/gpt-4o-mini",
        on_execute=_on_execute,
        on_notify=_on_notify,
    )

    async def _eval_silent(*a, **kw):
        return False

    monkeypatch.setattr("nanobot.utils.evaluator.evaluate_response", _eval_silent)

    await service._tick()
    assert executed == ["check status"]
    assert notified == []


@pytest.mark.asyncio
async def test_decide_retries_transient_error_then_succeeds(tmp_path, monkeypatch) -> None:
    provider = DummyProvider([
        LLMResponse(content="429 rate limit", finish_reason="error"),
        LLMResponse(
            content="",
            tool_calls=[
                ToolCallRequest(
                    id="hb_1",
                    name="heartbeat",
                    arguments={"action": "run", "tasks": "check open tasks"},
                )
            ],
        ),
    ])

    delays: list[int] = []

    async def _fake_sleep(delay: int) -> None:
        delays.append(delay)

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)

    service = HeartbeatService(
        workspace=tmp_path,
        provider=provider,
        model="openai/gpt-4o-mini",
    )

    action, tasks = await service._decide("heartbeat content")

    assert action == "run"
    assert tasks == "check open tasks"
    assert provider.calls == 2
    assert delays == [1]


@pytest.mark.asyncio
async def test_decide_prompt_includes_current_time(tmp_path) -> None:
    """Phase 1 user prompt must contain current time so the LLM can judge task urgency."""

    captured_messages: list[dict] = []

    class CaptureProvider(DummyProvider):
        async def chat(self, *args, **kwargs) -> LLMResponse:
            captured_messages.extend(kwargs.get("messages") or [])
            return await super().chat(*args, **kwargs)

    provider = CaptureProvider([
        LLMResponse(
            content="",
            tool_calls=[
                ToolCallRequest(id="hb_1", name="heartbeat", arguments={"action": "skip"})
            ],
        )
    ])

    service = HeartbeatService(
        workspace=tmp_path,
        provider=provider,
        model="openai/gpt-4o-mini",
    )

    await service._decide("- [ ] check backups")

    assert captured_messages
    user_msg = next(m for m in captured_messages if m["role"] == "user")
    assert "Current Time:" in user_msg["content"]
    assert "check backups" in user_msg["content"]


@pytest.mark.asyncio
async def test_trigger_now_writes_block_runtime_artifacts_when_gate_missing(tmp_path) -> None:
    (tmp_path / "HEARTBEAT.md").write_text("- [ ] review self evolution", encoding="utf-8")
    provider = DummyProvider([
        LLMResponse(
            content="",
            tool_calls=[
                ToolCallRequest(
                    id="hb_1",
                    name="heartbeat",
                    arguments={"action": "run", "tasks": "review self evolution"},
                )
            ],
        )
    ])

    async def _on_execute(tasks: str) -> str:
        return await run_self_evolving_cycle(
            workspace=tmp_path,
            tasks=tasks,
            execute_turn=lambda _tasks: asyncio.sleep(0, result="should not run"),
            now=datetime(2026, 4, 16, 10, 0, tzinfo=timezone.utc),
        )

    service = HeartbeatService(
        workspace=tmp_path,
        provider=provider,
        model="openai/gpt-4o-mini",
        on_execute=_on_execute,
    )

    summary = await service.trigger_now()

    assert summary is not None
    assert "BLOCK" in summary
    runtime = load_runtime_state(tmp_path)
    assert runtime["approval_gate_state"] == "missing"
    assert runtime["next_hint"] == "approval gate missing; refresh manually"
    report = _read_json(runtime["report_path"])
    assert report["result_status"] == "BLOCK"


@pytest.mark.asyncio
async def test_trigger_now_writes_pass_artifacts_and_promotion_metadata_when_gate_fresh(tmp_path) -> None:
    (tmp_path / "HEARTBEAT.md").write_text("- [ ] review self evolution", encoding="utf-8")
    approvals_dir = tmp_path / "state" / "approvals"
    approvals_dir.mkdir(parents=True)
    expires_at = datetime(2026, 4, 16, 12, 0, tzinfo=timezone.utc)
    (approvals_dir / "apply.ok").write_text(
        json.dumps({"expires_at_utc": expires_at.isoformat(), "ttl_minutes": 90}),
        encoding="utf-8",
    )

    provider = DummyProvider([
        LLMResponse(
            content="",
            tool_calls=[
                ToolCallRequest(
                    id="hb_1",
                    name="heartbeat",
                    arguments={"action": "run", "tasks": "review self evolution"},
                )
            ],
        )
    ])

    async def _on_execute(tasks: str) -> str:
        return await run_self_evolving_cycle(
            workspace=tmp_path,
            tasks=tasks,
            execute_turn=lambda _tasks: asyncio.sleep(0, result="bounded work complete"),
            now=expires_at - timedelta(minutes=30),
        )

    service = HeartbeatService(
        workspace=tmp_path,
        provider=provider,
        model="openai/gpt-4o-mini",
        on_execute=_on_execute,
    )

    summary = await service.trigger_now()

    assert summary is not None
    assert "PASS" in summary
    runtime = load_runtime_state(tmp_path)
    assert runtime["approval_gate_state"] == "fresh"
    report = _read_json(runtime["report_path"])
    assert report["result_status"] == "PASS"
    assert report["execution_response"] == "bounded work complete"
    assert runtime["promotion_candidate_id"] == report["promotion_candidate_id"]
    assert runtime["review_status"] == "pending"
    assert runtime["decision"] == "pending"
    assert report["promotion_candidate_id"].startswith("promotion-")
    assert report["review_status"] == "pending"
    assert report["decision"] == "pending"
    promotions_latest = _read_json(tmp_path / "state" / "promotions" / "latest.json")
    assert promotions_latest["promotion_candidate_id"] == report["promotion_candidate_id"]
    assert promotions_latest["review_status"] == "pending"
    candidate = _read_json(tmp_path / "state" / "promotions" / f"{report['promotion_candidate_id']}.json")
    assert candidate["promotion_candidate_id"] == report["promotion_candidate_id"]
    assert candidate["evidence_refs"] == [report["evidence_ref_id"]]
    outbox = _read_json(tmp_path / "state" / "outbox" / "latest.json")
    assert outbox["latest_report"]["promotion_candidate_id"] == report["promotion_candidate_id"]
