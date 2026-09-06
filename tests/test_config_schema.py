"""Substantive test coverage for nanobot.config.schema and nanobot.runtime.schemas."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from nanobot.config.schema import (
    AgentsConfig,
    Config,
    ExecToolConfig,
    GatewayConfig,
    MCPServerConfig,
    SubagentToolConfig,
    ToolsConfig,
    WebSearchConfig,
    WebToolsConfig,
)
from nanobot.runtime.schemas import (
    CycleHealth,
    CycleReport,
    PromotionCandidate,
)


def test_tools_config_exposes_subagent_compatibility_section():
    cfg = ToolsConfig()
    assert cfg.subagent.max_running == 1
    # #1395: routed string on the gateway route, naming the model the gateway
    # actually serves; this is the executor's unstripped config_fallback tier.
    assert cfg.subagent.model == "openai/un/qwen3.8-27b-gguf"
    assert cfg.subagent.api_base == ""
    assert cfg.subagent.harness_max_iterations == 8
    assert cfg.subagent.harness_max_tool_calls == 24


def test_tools_config_edge_cases_custom_subagent_and_exec():
    custom_subagent = SubagentToolConfig(
        max_running=10,
        model="custom/test-model",
        api_base="https://custom.api.local",
        harness_max_iterations=16,
        harness_max_tool_calls=48,
    )
    custom_exec = ExecToolConfig(
        enable=False,
        timeout=180,
        path_append="/custom/bin",
    )
    custom_mcp = {
        "mcp_server_1": MCPServerConfig(
            type="stdio",
            command="node",
            args=["index.js"],
            tool_timeout=45,
            enabled_tools=["tool_a", "tool_b"],
        )
    }
    cfg = ToolsConfig(
        subagent=custom_subagent,
        exec=custom_exec,
        web=WebToolsConfig(
            proxy="http://proxy.local:8080",
            search=WebSearchConfig(provider="custom", api_key="secret-key", max_results=10),
        ),
        restrict_to_workspace=True,
        mcp_servers=custom_mcp,
    )
    assert cfg.subagent.max_running == 10
    assert cfg.subagent.model == "custom/test-model"
    assert cfg.subagent.api_base == "https://custom.api.local"
    assert cfg.subagent.harness_max_iterations == 16
    assert cfg.subagent.harness_max_tool_calls == 48
    assert cfg.exec.enable is False
    assert cfg.exec.timeout == 180
    assert cfg.exec.path_append == "/custom/bin"
    assert cfg.web.proxy == "http://proxy.local:8080"
    assert cfg.web.search.provider == "custom"
    assert cfg.web.search.api_key == "secret-key"
    assert cfg.web.search.max_results == 10
    assert cfg.restrict_to_workspace is True
    assert "mcp_server_1" in cfg.mcp_servers
    assert cfg.mcp_servers["mcp_server_1"].enabled_tools == ["tool_a", "tool_b"]


def test_config_root_defaults_and_validation():
    cfg = Config()
    assert isinstance(cfg.agents, AgentsConfig)
    assert isinstance(cfg.tools, ToolsConfig)
    assert isinstance(cfg.gateway, GatewayConfig)
    assert cfg.gateway.port == 18790
    assert cfg.gateway.host == "0.0.0.0"
    assert cfg.gateway.heartbeat.enabled is True
    assert cfg.gateway.heartbeat.interval_s == 1800
    assert cfg.workspace_path.name == "workspace"

    # MCPServerConfig validation
    mcp = MCPServerConfig(
        type="stdio",
        command="node",
        args=["server.js"],
        env={"NODE_ENV": "test"},
    )
    assert mcp.command == "node"
    assert mcp.args == ["server.js"]
    assert mcp.env == {"NODE_ENV": "test"}

    # Invalid type raises ValidationError
    with pytest.raises(ValidationError):
        MCPServerConfig(type="invalid_type")  # type: ignore[arg-type]


def test_runtime_schemas_cycle_report_typed_dict():
    report: CycleReport = {
        "schema_version": "1.0.0",
        "cycle_id": "c-2026-04-01-001",
        "cycle_started_utc": "2026-04-01T00:00:00Z",
        "cycle_ended_utc": "2026-04-01T00:05:00Z",
        "goal_id": "g-1043",
        "goal_text": "Improve test stability and io atomic helper",
        "current_task_id": "t-1",
        "result_status": "success",
        "decision": "promote",
        "improvement_score": 0.98,
        "promotion_candidate_id": "cand-1043",
        "review_status": "passed",
        "evidence_ref_id": "ev-1043",
        "experiment": {"type": "code"},
        "follow_through": {"status": "complete"},
        "result": {"exit_code": 0},
    }
    assert report["schema_version"] == "1.0.0"
    assert report["cycle_id"] == "c-2026-04-01-001"
    assert report["goal_id"] == "g-1043"
    assert report["improvement_score"] == 0.98
    assert report["decision"] == "promote"
    assert report["result"]["exit_code"] == 0


def test_runtime_schemas_promotion_candidate_and_cycle_health():
    candidate: PromotionCandidate = {
        "schema_version": "1.0.0",
        "promotion_candidate_id": "cand-1043",
        "review_status": "approved",
        "decision": "promoted",
        "decision_reason": "All checks passed cleanly",
        "candidate_path": "/path/to/candidate",
        "artifact_path": "/path/to/artifact",
        "readiness_checks": {"lint": True, "tests": True},
        "readiness_reasons": ["clean lint", "all tests pass"],
        "recommended_next_action": "merge",
        "governance_packet": {"approver": "governor"},
        "decision_record": "dr-1043",
        "accepted_record": "ar-1043",
        "promotion_provenance": {"commit": "abc123"},
    }
    assert candidate["promotion_candidate_id"] == "cand-1043"
    assert candidate["review_status"] == "approved"
    assert candidate["decision"] == "promoted"
    assert candidate["readiness_checks"]["tests"] is True

    health: CycleHealth = {
        "schema_version": "1.0.0",
        "runtime_state_source": "coordinator",
        "runtime_state_root": "/var/run/nanobot",
        "latest_cycle_id": "c-2026-04-01-001",
        "latest_report_path": "/var/run/nanobot/report.json",
        "latest_subagent_telemetry_id": "telem-001",
        "latest_subagent_telemetry_path": "/var/run/nanobot/telem.json",
        "service_status": {"daemon": "running"},
        "failed_units_count": 0,
        "promotion_readiness": {"ready": True},
        "severity": "info",
        "exit_code": 0,
        "next_recommended_action": "continue",
        "success_signals": {"healthcheck": "ok"},
    }
    assert health["severity"] == "info"
    assert health["exit_code"] == 0
    assert health["failed_units_count"] == 0
    assert health["service_status"]["daemon"] == "running"
