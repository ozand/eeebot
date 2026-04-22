import json
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from nanobot.cli.commands import _make_provider, app
from nanobot.config.schema import Config
from nanobot.runtime.state import load_runtime_state, load_runtime_state_from_root
from nanobot.providers.litellm_provider import LiteLLMProvider
from nanobot.providers.openai_codex_provider import _strip_model_prefix
from nanobot.providers.registry import find_by_model

runner = CliRunner()


class _StopGatewayError(RuntimeError):
    pass


import shutil

import pytest


@pytest.fixture
def mock_paths():
    """Mock config/workspace paths for test isolation."""
    with patch("nanobot.config.loader.get_config_path") as mock_cp, \
         patch("nanobot.config.loader.save_config") as mock_sc, \
         patch("nanobot.config.loader.load_config") as mock_lc, \
         patch("nanobot.cli.commands.get_workspace_path") as mock_ws:

        base_dir = Path("./test_onboard_data")
        if base_dir.exists():
            shutil.rmtree(base_dir)
        base_dir.mkdir()

        config_file = base_dir / "config.json"
        workspace_dir = base_dir / "workspace"

        mock_cp.return_value = config_file
        mock_ws.return_value = workspace_dir
        mock_lc.side_effect = lambda _config_path=None: Config()

        def _save_config(config: Config, config_path: Path | None = None):
            target = config_path or config_file
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(config.model_dump(by_alias=True)), encoding="utf-8")

        mock_sc.side_effect = _save_config

        yield config_file, workspace_dir, mock_ws

        if base_dir.exists():
            shutil.rmtree(base_dir)


def test_onboard_fresh_install(mock_paths):
    """No existing config — should create from scratch."""
    config_file, workspace_dir, mock_ws = mock_paths

    result = runner.invoke(app, ["onboard"])

    assert result.exit_code == 0
    assert "Created config" in result.stdout
    assert "Created workspace" in result.stdout
    assert "nanobot is ready" in result.stdout
    assert config_file.exists()
    assert (workspace_dir / "AGENTS.md").exists()
    assert (workspace_dir / "memory" / "MEMORY.md").exists()
    expected_workspace = Config().workspace_path
    assert mock_ws.call_args.args == (expected_workspace,)


def test_onboard_existing_config_refresh(mock_paths):
    """Config exists, user declines overwrite — should refresh (load-merge-save)."""
    config_file, workspace_dir, _ = mock_paths
    config_file.write_text('{"existing": true}')

    result = runner.invoke(app, ["onboard"], input="n\n")

    assert result.exit_code == 0
    assert "Config already exists" in result.stdout
    assert "existing values preserved" in result.stdout
    assert workspace_dir.exists()
    assert (workspace_dir / "AGENTS.md").exists()


def test_onboard_existing_config_overwrite(mock_paths):
    """Config exists, user confirms overwrite — should reset to defaults."""
    config_file, workspace_dir, _ = mock_paths
    config_file.write_text('{"existing": true}')

    result = runner.invoke(app, ["onboard"], input="y\n")

    assert result.exit_code == 0
    assert "Config already exists" in result.stdout
    assert "Config reset to defaults" in result.stdout
    assert workspace_dir.exists()


def test_onboard_existing_workspace_safe_create(mock_paths):
    """Workspace exists — should not recreate, but still add missing templates."""
    config_file, workspace_dir, _ = mock_paths
    workspace_dir.mkdir(parents=True)
    config_file.write_text("{}")

    result = runner.invoke(app, ["onboard"], input="n\n")

    assert result.exit_code == 0
    assert "Created workspace" not in result.stdout
    assert "Created AGENTS.md" in result.stdout
    assert (workspace_dir / "AGENTS.md").exists()


def _strip_ansi(text):
    """Remove ANSI escape codes from text."""
    ansi_escape = re.compile(r'\x1b\[[0-9;]*m')
    return ansi_escape.sub('', text)


def test_onboard_help_shows_workspace_and_config_options():
    result = runner.invoke(app, ["onboard", "--help"])

    assert result.exit_code == 0
    stripped_output = _strip_ansi(result.stdout)
    assert "--workspace" in stripped_output
    assert "-w" in stripped_output
    assert "--config" in stripped_output
    assert "-c" in stripped_output
    assert "--wizard" in stripped_output
    assert "--dir" not in stripped_output


def test_onboard_interactive_discard_does_not_save_or_create_workspace(mock_paths, monkeypatch):
    config_file, workspace_dir, _ = mock_paths

    from nanobot.cli.onboard_wizard import OnboardResult

    monkeypatch.setattr(
        "nanobot.cli.onboard_wizard.run_onboard",
        lambda initial_config: OnboardResult(config=initial_config, should_save=False),
    )

    result = runner.invoke(app, ["onboard", "--wizard"])

    assert result.exit_code == 0
    assert "No changes were saved" in result.stdout
    assert not config_file.exists()
    assert not workspace_dir.exists()


def test_onboard_uses_explicit_config_and_workspace_paths(tmp_path, monkeypatch):
    config_path = tmp_path / "instance" / "config.json"
    workspace_path = tmp_path / "workspace"

    monkeypatch.setattr("nanobot.channels.registry.discover_all", lambda: {})

    result = runner.invoke(
        app,
        ["onboard", "--config", str(config_path), "--workspace", str(workspace_path)],
    )

    assert result.exit_code == 0
    saved = Config.model_validate(json.loads(config_path.read_text(encoding="utf-8")))
    assert saved.workspace_path == workspace_path
    assert (workspace_path / "AGENTS.md").exists()
    stripped_output = _strip_ansi(result.stdout)
    compact_output = stripped_output.replace("\n", "")
    resolved_config = str(config_path.resolve())
    assert resolved_config in compact_output
    assert f"--config {resolved_config}" in compact_output


def test_onboard_wizard_preserves_explicit_config_in_next_steps(tmp_path, monkeypatch):
    config_path = tmp_path / "instance" / "config.json"
    workspace_path = tmp_path / "workspace"

    from nanobot.cli.onboard_wizard import OnboardResult

    monkeypatch.setattr(
        "nanobot.cli.onboard_wizard.run_onboard",
        lambda initial_config: OnboardResult(config=initial_config, should_save=True),
    )
    monkeypatch.setattr("nanobot.channels.registry.discover_all", lambda: {})

    result = runner.invoke(
        app,
        ["onboard", "--wizard", "--config", str(config_path), "--workspace", str(workspace_path)],
    )

    assert result.exit_code == 0
    stripped_output = _strip_ansi(result.stdout)
    compact_output = stripped_output.replace("\n", "")
    resolved_config = str(config_path.resolve())
    assert f'nanobot agent -m "Hello!" --config {resolved_config}' in compact_output
    assert f"nanobot gateway --config {resolved_config}" in compact_output


def test_config_matches_github_copilot_codex_with_hyphen_prefix():
    config = Config()
    config.agents.defaults.model = "github-copilot/gpt-5.3-codex"

    assert config.get_provider_name() == "github_copilot"


def test_config_matches_openai_codex_with_hyphen_prefix():
    config = Config()
    config.agents.defaults.model = "openai-codex/gpt-5.1-codex"

    assert config.get_provider_name() == "openai_codex"


def test_config_dump_excludes_oauth_provider_blocks():
    config = Config()

    providers = config.model_dump(by_alias=True)["providers"]

    assert "openaiCodex" not in providers
    assert "githubCopilot" not in providers


def test_config_matches_explicit_ollama_prefix_without_api_key():
    config = Config()
    config.agents.defaults.model = "ollama/llama3.2"

    assert config.get_provider_name() == "ollama"
    assert config.get_api_base() == "http://localhost:11434"


def test_config_explicit_ollama_provider_uses_default_localhost_api_base():
    config = Config()
    config.agents.defaults.provider = "ollama"
    config.agents.defaults.model = "llama3.2"

    assert config.get_provider_name() == "ollama"
    assert config.get_api_base() == "http://localhost:11434"


def test_config_auto_detects_ollama_from_local_api_base():
    config = Config.model_validate(
        {
            "agents": {"defaults": {"provider": "auto", "model": "llama3.2"}},
            "providers": {"ollama": {"apiBase": "http://localhost:11434"}},
        }
    )

    assert config.get_provider_name() == "ollama"
    assert config.get_api_base() == "http://localhost:11434"


def test_config_prefers_ollama_over_vllm_when_both_local_providers_configured():
    config = Config.model_validate(
        {
            "agents": {"defaults": {"provider": "auto", "model": "llama3.2"}},
            "providers": {
                "vllm": {"apiBase": "http://localhost:8000"},
                "ollama": {"apiBase": "http://localhost:11434"},
            },
        }
    )

    assert config.get_provider_name() == "ollama"
    assert config.get_api_base() == "http://localhost:11434"


def test_config_falls_back_to_vllm_when_ollama_not_configured():
    config = Config.model_validate(
        {
            "agents": {"defaults": {"provider": "auto", "model": "llama3.2"}},
            "providers": {
                "vllm": {"apiBase": "http://localhost:8000"},
            },
        }
    )

    assert config.get_provider_name() == "vllm"
    assert config.get_api_base() == "http://localhost:8000"


def test_find_by_model_prefers_explicit_prefix_over_generic_codex_keyword():
    spec = find_by_model("github-copilot/gpt-5.3-codex")

    assert spec is not None
    assert spec.name == "github_copilot"


def test_litellm_provider_canonicalizes_github_copilot_hyphen_prefix():
    provider = LiteLLMProvider(default_model="github-copilot/gpt-5.3-codex")

    resolved = provider._resolve_model("github-copilot/gpt-5.3-codex")

    assert resolved == "github_copilot/gpt-5.3-codex"


def test_openai_codex_strip_prefix_supports_hyphen_and_underscore():
    assert _strip_model_prefix("openai-codex/gpt-5.1-codex") == "gpt-5.1-codex"
    assert _strip_model_prefix("openai_codex/gpt-5.1-codex") == "gpt-5.1-codex"


def test_make_provider_passes_extra_headers_to_custom_provider():
    config = Config.model_validate(
        {
            "agents": {"defaults": {"provider": "custom", "model": "gpt-4o-mini"}},
            "providers": {
                "custom": {
                    "apiKey": "test-key",
                    "apiBase": "https://example.com/v1",
                    "extraHeaders": {
                        "APP-Code": "demo-app",
                        "x-session-affinity": "sticky-session",
                    },
                }
            },
        }
    )

    with patch("nanobot.providers.custom_provider.AsyncOpenAI") as mock_async_openai:
        _make_provider(config)

    kwargs = mock_async_openai.call_args.kwargs
    assert kwargs["api_key"] == "test-key"
    assert kwargs["base_url"] == "https://example.com/v1"
    assert kwargs["default_headers"]["APP-Code"] == "demo-app"
    assert kwargs["default_headers"]["x-session-affinity"] == "sticky-session"


@pytest.fixture
def mock_agent_runtime(tmp_path):
    """Mock agent command dependencies for focused CLI tests."""
    config = Config()
    config.agents.defaults.workspace = str(tmp_path / "default-workspace")
    cron_dir = tmp_path / "data" / "cron"

    with patch("nanobot.config.loader.load_config", return_value=config) as mock_load_config, \
         patch("nanobot.config.paths.get_cron_dir", return_value=cron_dir), \
         patch("nanobot.cli.commands.sync_workspace_templates") as mock_sync_templates, \
         patch("nanobot.cli.commands._make_provider", return_value=object()), \
         patch("nanobot.cli.commands._print_agent_response") as mock_print_response, \
         patch("nanobot.bus.queue.MessageBus"), \
         patch("nanobot.cron.service.CronService"), \
         patch("nanobot.agent.loop.AgentLoop") as mock_agent_loop_cls:

        agent_loop = MagicMock()
        agent_loop.channels_config = None
        agent_loop.process_direct = AsyncMock(return_value="mock-response")
        agent_loop.close_mcp = AsyncMock(return_value=None)
        mock_agent_loop_cls.return_value = agent_loop

        yield {
            "config": config,
            "load_config": mock_load_config,
            "sync_templates": mock_sync_templates,
            "agent_loop_cls": mock_agent_loop_cls,
            "agent_loop": agent_loop,
            "print_response": mock_print_response,
        }


def test_agent_help_shows_workspace_and_config_options():
    result = runner.invoke(app, ["agent", "--help"])

    assert result.exit_code == 0
    stripped_output = _strip_ansi(result.stdout)
    assert "--workspace" in stripped_output
    assert "-w" in stripped_output
    assert "--config" in stripped_output
    assert "-c" in stripped_output


def test_agent_uses_default_config_when_no_workspace_or_config_flags(mock_agent_runtime):
    result = runner.invoke(app, ["agent", "-m", "hello"])

    assert result.exit_code == 0
    assert mock_agent_runtime["load_config"].call_args.args == (None,)
    assert mock_agent_runtime["sync_templates"].call_args.args == (
        mock_agent_runtime["config"].workspace_path,
    )
    assert mock_agent_runtime["agent_loop_cls"].call_args.kwargs["workspace"] == (
        mock_agent_runtime["config"].workspace_path
    )
    mock_agent_runtime["agent_loop"].process_direct.assert_awaited_once()
    mock_agent_runtime["print_response"].assert_called_once_with("mock-response", render_markdown=True)


def test_agent_uses_explicit_config_path(mock_agent_runtime, tmp_path: Path):
    config_path = tmp_path / "agent-config.json"
    config_path.write_text("{}")

    result = runner.invoke(app, ["agent", "-m", "hello", "-c", str(config_path)])

    assert result.exit_code == 0
    assert mock_agent_runtime["load_config"].call_args.args == (config_path.resolve(),)


def test_agent_config_sets_active_path(monkeypatch, tmp_path: Path) -> None:
    config_file = tmp_path / "instance" / "config.json"
    config_file.parent.mkdir(parents=True)
    config_file.write_text("{}")

    config = Config()
    seen: dict[str, Path] = {}

    monkeypatch.setattr(
        "nanobot.config.loader.set_config_path",
        lambda path: seen.__setitem__("config_path", path),
    )
    monkeypatch.setattr("nanobot.config.loader.load_config", lambda _path=None: config)
    monkeypatch.setattr("nanobot.config.paths.get_cron_dir", lambda: config_file.parent / "cron")
    monkeypatch.setattr("nanobot.cli.commands.sync_workspace_templates", lambda _path: None)
    monkeypatch.setattr("nanobot.cli.commands._make_provider", lambda _config: object())
    monkeypatch.setattr("nanobot.bus.queue.MessageBus", lambda: object())
    monkeypatch.setattr("nanobot.cron.service.CronService", lambda _store: object())

    class _FakeAgentLoop:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def process_direct(self, *_args, **_kwargs) -> str:
            return "ok"

        async def close_mcp(self) -> None:
            return None

    monkeypatch.setattr("nanobot.agent.loop.AgentLoop", _FakeAgentLoop)
    monkeypatch.setattr("nanobot.cli.commands._print_agent_response", lambda *_args, **_kwargs: None)

    result = runner.invoke(app, ["agent", "-m", "hello", "-c", str(config_file)])

    assert result.exit_code == 0
    assert seen["config_path"] == config_file.resolve()


def test_agent_overrides_workspace_path(mock_agent_runtime):
    workspace_path = Path("/tmp/agent-workspace")

    result = runner.invoke(app, ["agent", "-m", "hello", "-w", str(workspace_path)])

    assert result.exit_code == 0
    assert mock_agent_runtime["config"].agents.defaults.workspace == str(workspace_path)
    assert mock_agent_runtime["sync_templates"].call_args.args == (workspace_path,)
    assert mock_agent_runtime["agent_loop_cls"].call_args.kwargs["workspace"] == workspace_path


def test_agent_workspace_override_wins_over_config_workspace(mock_agent_runtime, tmp_path: Path):
    config_path = tmp_path / "agent-config.json"
    config_path.write_text("{}")
    workspace_path = Path("/tmp/agent-workspace")

    result = runner.invoke(
        app,
        ["agent", "-m", "hello", "-c", str(config_path), "-w", str(workspace_path)],
    )

    assert result.exit_code == 0
    assert mock_agent_runtime["load_config"].call_args.args == (config_path.resolve(),)
    assert mock_agent_runtime["config"].agents.defaults.workspace == str(workspace_path)
    assert mock_agent_runtime["sync_templates"].call_args.args == (workspace_path,)
    assert mock_agent_runtime["agent_loop_cls"].call_args.kwargs["workspace"] == workspace_path


def test_agent_hints_about_deprecated_memory_window(mock_agent_runtime, tmp_path):
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"agents": {"defaults": {"memoryWindow": 42}}}))

    result = runner.invoke(app, ["agent", "-m", "hello", "-c", str(config_file)])

    assert result.exit_code == 0
    assert "memoryWindow" in result.stdout
    assert "no longer used" in result.stdout


def test_load_runtime_state_from_host_control_plane_root(tmp_path):
    state_root = tmp_path / "host-state"
    reports_dir = state_root / "reports"
    reports_dir.mkdir(parents=True)
    (reports_dir / "evolution-1.json").write_text(
        json.dumps({"goal_id": "goal-1", "result_status": "PASS"}),
        encoding="utf-8",
    )

    runtime = load_runtime_state_from_root(
        state_root,
        source_kind="host_control_plane",
    )

    assert runtime["runtime_state_source"] == "host_control_plane"
    assert runtime["runtime_state_root"] == str(state_root)



def test_load_runtime_state_reads_host_control_plane_layout(tmp_path):
    state_root = tmp_path / "host-state"
    reports_dir = state_root / "reports"
    goals_dir = state_root / "goals"
    outbox_dir = state_root / "outbox"
    reports_dir.mkdir(parents=True)
    goals_dir.mkdir(parents=True)
    outbox_dir.mkdir(parents=True)
    report_path = reports_dir / "evolution-20260415T230020Z.json"
    report_path.write_text(
        json.dumps(
            {
                "goal": {"goal_id": "goal-44"},
                "result": {"status": "BLOCK"},
                "process_reflection": {"status": "BLOCK"},
                "capability_gate": None,
            }
        ),
        encoding="utf-8",
    )
    (goals_dir / "registry.json").write_text(
        json.dumps(
            {
                "active_goal_id": "goal-44",
                "goals": {"goal-44": {"goal_id": "goal-44", "status": "blocked"}},
            }
        ),
        encoding="utf-8",
    )
    (outbox_dir / "report.index.json").write_text(
        json.dumps(
            {
                "status": "BLOCK",
                "source": str(report_path),
                "improvement_score": 32,
                "goal": {
                    "goal_id": "goal-44",
                    "text": "Improve prompt clarity",
                    "follow_through": {
                        "status": "blocked_next_action",
                        "artifact_paths": ["prompts/diagnostics.md"],
                    },
                },
                "goal_context": {
                    "subagent_rollup": {
                        "enabled": True,
                        "count_total": 3,
                        "count_done": 2,
                    }
                },
                "capability_gate": {"approval": {"ok": False, "reason": "missing"}},
            }
        ),
        encoding="utf-8",
    )
    (outbox_dir / "latest_workspace_export.json").write_text(
        json.dumps({"kind": "workspace_export"}),
        encoding="utf-8",
    )

    runtime = load_runtime_state_from_root(
        state_root,
        source_kind="host_control_plane",
    )

    assert runtime["report_path"] == str(report_path)
    assert runtime["active_goal"] == "goal-44"
    assert runtime["goal_text"] == "Improve prompt clarity"
    assert runtime["runtime_status"] == "BLOCK"
    assert runtime["approval_gate_state"] == "missing"
    assert runtime["artifact_paths"] == ["prompts/diagnostics.md"]
    assert runtime["follow_through_status"] == "blocked_next_action"
    assert runtime["improvement_score"] == 32
    assert runtime["subagent_rollup"]["enabled"] is True
    assert runtime["outbox_path"].endswith("report.index.json")



def test_status_can_report_host_control_plane_authority(tmp_path, monkeypatch):
    state_root = tmp_path / "host-state"
    reports_dir = state_root / "reports"
    outbox_dir = state_root / "outbox"
    goals_dir = state_root / "goals"
    reports_dir.mkdir(parents=True)
    outbox_dir.mkdir(parents=True)
    goals_dir.mkdir(parents=True)
    report_path = reports_dir / "evolution-20260415T230020Z.json"
    report_path.write_text(
        json.dumps(
            {
                "goal": {"goal_id": "goal-44"},
                "process_reflection": {"status": "PASS"},
                "capability_gate": {"approval": {"ok": True, "reason": "valid"}},
                "follow_through": {"artifact_paths": ["prompts/diagnostics.md"]},
            }
        ),
        encoding="utf-8",
    )
    (outbox_dir / "report.index.json").write_text(
        json.dumps(
            {
                "status": "PASS",
                "source": str(report_path),
                "improvement_score": 77,
                "goal": {
                    "goal_id": "goal-44",
                    "text": "Improve prompt clarity",
                    "follow_through": {
                        "status": "artifact",
                        "artifact_paths": ["prompts/diagnostics.md"],
                    },
                },
                "goal_context": {
                    "subagent_rollup": {
                        "enabled": True,
                        "count_total": 3,
                        "count_done": 2,
                    }
                },
                "capability_gate": {"approval": {"ok": True, "reason": "valid"}},
            }
        ),
        encoding="utf-8",
    )
    (goals_dir / "registry.json").write_text(
        json.dumps(
            {
                "active_goal_id": "goal-44",
                "goals": {"goal-44": {"goal_id": "goal-44", "status": "active"}},
            }
        ),
        encoding="utf-8",
    )

    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    config = Config()
    config.agents.defaults.workspace = str(workspace)
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps(config.model_dump(by_alias=True)), encoding="utf-8")

    monkeypatch.setattr("nanobot.config.loader.get_config_path", lambda: config_file)
    monkeypatch.setattr("nanobot.config.loader.load_config", lambda _path=None: config)

    result = runner.invoke(
        app,
        [
            "status",
            "--runtime-state-source",
            "host_control_plane",
            "--runtime-state-root",
            str(state_root),
        ],
    )

    assert result.exit_code == 0
    assert "Runtime state source: host_control_plane" in result.stdout
    assert f"Runtime state root: {state_root}" in result.stdout
    assert "Runtime status: PASS" in result.stdout
    assert "Active goal: goal-44" in result.stdout
    assert "Goal text: Improve prompt clarity" in result.stdout
    assert "Follow-through: artifact" in result.stdout
    assert "Improvement score: 77" in result.stdout
    assert "Subagents: enabled=True, total=3, done=2" in result.stdout
    assert "Artifacts: prompts/diagnostics.md" in result.stdout
    assert "Gate state: valid" in result.stdout



def test_status_reports_runtime_surface(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    state_dir = workspace / "state"
    reports_dir = state_dir / "reports"
    goals_dir = state_dir / "goals"
    outbox_dir = state_dir / "outbox"
    hypotheses_dir = state_dir / "hypotheses"
    reports_dir.mkdir(parents=True)
    goals_dir.mkdir(parents=True)
    outbox_dir.mkdir(parents=True)
    hypotheses_dir.mkdir(parents=True)

    (reports_dir / "evolution-20260412.json").write_text(
        json.dumps(
            {
                "cycle_id": "cycle-123",
                "cycle_started_utc": "2026-04-12T12:00:00Z",
                "cycle_ended_utc": "2026-04-12T12:05:00Z",
                "goal_id": "goal-44e50921129bf475",
                "evidence_ref_id": "evidence-88",
                "promotion_candidate_id": "promotion-42",
                "review_status": "pending",
                "decision": "pending",
            }
        ),
        encoding="utf-8",
    )
    (state_dir / "promotions").mkdir(parents=True)
    ((state_dir / "promotions") / "latest.json").write_text(
        json.dumps(
            {
                "promotion_candidate_id": "promotion-42",
                "origin_cycle_id": "cycle-123",
                "review_status": "pending",
                "decision": "pending",
                "candidate_path": str((state_dir / "promotions") / "promotion-42.json"),
            }
        ),
        encoding="utf-8",
    )
    ((state_dir / "promotions") / "decisions").mkdir(parents=True)
    ((state_dir / "promotions") / "accepted").mkdir(parents=True)
    (((state_dir / "promotions") / "decisions") / "promotion-42.json").write_text(
        json.dumps(
            {
                "promotion_candidate_id": "promotion-42",
                "decision": "pending",
                "review_status": "pending",
                "reviewed_at_utc": "2026-04-12T12:06:00Z",
            }
        ),
        encoding="utf-8",
    )
    (((state_dir / "promotions") / "accepted") / "promotion-42.json").write_text(
        json.dumps(
            {
                "promotion_candidate_id": "promotion-42",
                "decision": "accept",
                "accepted_at_utc": "2026-04-12T12:07:00Z",
                "patch_bundle_path": str(((state_dir / "promotions") / "patches") / "promotion-42.json"),
            }
        ),
        encoding="utf-8",
    )
    (goals_dir / "active.json").write_text(
        json.dumps({"active_goal": "goal-44e50921129bf475"}),
        encoding="utf-8",
    )
    history_dir = goals_dir / "history"
    history_dir.mkdir(parents=True)
    (goals_dir / "current.json").write_text(
        json.dumps(
            {
                "schema_version": "task-plan-v1",
                "cycle_id": "cycle-123",
                "goal_id": "goal-44e50921129bf475",
                "active_goal": "goal-44e50921129bf475",
                "current_task_id": "record-reward",
                "task_counts": {"total": 3, "done": 2, "active": 1, "pending": 0},
                "reward_signal": {
                    "value": 1.0,
                    "source": "result_status",
                    "result_status": "PASS",
                    "improvement_score": None,
                },
                "history_path": str(history_dir / "cycle-cycle-123.json"),
            }
        ),
        encoding="utf-8",
    )
    (history_dir / "cycle-cycle-123.json").write_text(
        json.dumps(
            {
                "schema_version": "task-history-v1",
                "cycle_id": "cycle-123",
                "current_task_id": "record-reward",
                "task_counts": {"total": 3, "done": 2, "active": 1, "pending": 0},
                "reward_signal": {
                    "value": 1.0,
                    "source": "result_status",
                    "result_status": "PASS",
                    "improvement_score": None,
                },
                "recorded_at_utc": "2026-04-12T12:05:00Z",
            }
        ),
        encoding="utf-8",
    )
    (outbox_dir / "latest.json").write_text(
        json.dumps({"approval_gate": {"state": "fresh", "ttl_minutes": 60}}),
        encoding="utf-8",
    )
    (outbox_dir / "20260412-old.json").write_text(
        json.dumps({"approval_gate": {"state": "stale", "ttl_minutes": 5}}),
        encoding="utf-8",
    )
    (hypotheses_dir / "backlog.json").write_text(
        json.dumps(
            {
                "schema_version": "hypothesis-backlog-v1",
                "cycle_id": "cycle-123",
                "goal_id": "goal-44e50921129bf475",
                "selected_hypothesis_id": "record-reward",
                "selected_hypothesis_title": "Record cycle reward",
                "selected_hypothesis_score": 88,
                "entry_count": 3,
                "entries": [
                    {
                        "hypothesis_id": "hypothesis-record-reward",
                        "task_id": "record-reward",
                        "task_title": "Record cycle reward",
                        "task_status": "active",
                        "selected": True,
                        "selection_status": "selected",
                        "bounded_priority_score": 88,
                        "execution_spec": {
                            "goal": "goal-44e50921129bf475",
                            "task_title": "Record cycle reward",
                            "acceptance": "Record cycle reward is completed with durable evidence for goal-44e50921129bf475",
                            "budget": {"max_requests": 2, "max_tool_calls": 12, "max_subagents": 2, "max_timeout_seconds": 900},
                        },
                    },
                    {
                        "hypothesis_id": "hypothesis-run-bounded-turn",
                        "task_id": "run-bounded-turn",
                        "task_title": "Run bounded turn",
                        "task_status": "done",
                        "selected": False,
                        "selection_status": "backlog",
                        "bounded_priority_score": 42,
                        "execution_spec": {
                            "goal": "goal-44e50921129bf475",
                            "task_title": "Run bounded turn",
                            "acceptance": "Run bounded turn is completed with durable evidence for goal-44e50921129bf475",
                            "budget": {"max_requests": 2, "max_tool_calls": 12, "max_subagents": 2, "max_timeout_seconds": 900},
                        },
                    },
                    {
                        "hypothesis_id": "hypothesis-refresh-approval-gate",
                        "task_id": "refresh-approval-gate",
                        "task_title": "Refresh approval gate",
                        "task_status": "done",
                        "selected": False,
                        "selection_status": "backlog",
                        "bounded_priority_score": 35,
                        "execution_spec": {
                            "goal": "goal-44e50921129bf475",
                            "task_title": "Refresh approval gate",
                            "acceptance": "Refresh approval gate is completed with durable evidence for goal-44e50921129bf475",
                            "budget": {"max_requests": 2, "max_tool_calls": 12, "max_subagents": 2, "max_timeout_seconds": 900},
                        },
                    },
                ],
                "context": {
                    "result_status": "PASS",
                    "approval_gate_state": "fresh",
                    "next_hint": "none",
                },
            }
        ),
        encoding="utf-8",
    )
    config = Config()
    config.agents.defaults.workspace = str(workspace)
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps(config.model_dump(by_alias=True)), encoding="utf-8")

    monkeypatch.setattr("nanobot.config.loader.get_config_path", lambda: config_file)
    monkeypatch.setattr("nanobot.config.loader.load_config", lambda _path=None: config)

    result = runner.invoke(app, ["status"])

    assert result.exit_code == 0
    assert "Runtime:" in result.stdout
    assert "Runtime state source: workspace_state" in result.stdout
    assert f"Runtime state root: {state_dir}" in result.stdout
    assert "Runtime status: unknown" in result.stdout
    assert "Active goal: goal-44e50921129bf475" in result.stdout
    assert "Cycle: cycle-123" in result.stdout
    assert "Cycle started: 2026-04-12T12:00:00Z" in result.stdout
    assert "Cycle ended: 2026-04-12T12:05:00Z" in result.stdout
    assert "Evidence: evidence-88" in result.stdout
    assert "Promotion candidate: promotion-42" in result.stdout
    assert "Promotion review: pending" in result.stdout
    assert "Promotion decision: pending" in result.stdout
    assert "Promotion source:" in result.stdout
    assert "latest.json" in result.stdout
    assert "Promotion summary: promotion-42 | pending | pending" in result.stdout
    assert "Promotion candidate path:" in result.stdout
    assert "promotion-42.json" in result.stdout
    assert "Promotion decision record: present" in result.stdout
    assert "Promotion accepted record: present" in result.stdout
    assert "Promotion reviewed at: 2026-04-12T12:06:00Z" in result.stdout
    assert "Promotion accepted at: 2026-04-12T12:07:00Z" in result.stdout
    assert "Patch bundle: " in result.stdout
    assert "promotion-42.json" in result.stdout
    assert "Approval gate: state=fresh, ttl_minutes=60" in result.stdout
    assert "Gate state: fresh" in result.stdout
    assert "Gate TTL (min): 60" in result.stdout
    assert "Next: none" in result.stdout
    assert "Report source:" in result.stdout
    assert "evolution-20260412.json" in result.stdout
    assert "Current task: record-reward" in result.stdout
    assert "Task counts: total=3, done=2, active=1, pending=0" in result.stdout
    assert "Task reward: value=1.0, source=result_status" in result.stdout
    assert "Plan source:" in result.stdout
    assert "current.json" in result.stdout
    assert "History source:" in result.stdout
    assert "cycle-cycle-123.json" in result.stdout
    assert "Hypothesis backlog source:" in result.stdout
    assert "backlog.json" in result.stdout
    assert "Hypothesis backlog schema: hypothesis-backlog-v1" in result.stdout
    assert "Hypothesis backlog selected: record-reward" in result.stdout
    assert "Hypothesis backlog title: Record cycle reward" in result.stdout
    assert "Hypothesis backlog entries: 3" in result.stdout
    assert "Hypothesis backlog best score: 88" in result.stdout
    assert "Task plan schema: task-plan-v1" in result.stdout
    assert "Goal source:" in result.stdout
    assert "active.json" in result.stdout
    assert "Outbox source:" in result.stdout
    assert "latest.json" in result.stdout


def test_runtime_state_prefers_newest_evolution_report(tmp_path):
    workspace = tmp_path / "workspace"
    reports_dir = workspace / "state" / "reports"
    reports_dir.mkdir(parents=True)
    (workspace / "state" / "goals").mkdir(parents=True)
    outbox_dir = workspace / "state" / "outbox"
    outbox_dir.mkdir(parents=True)

    (reports_dir / "evolution-20260401.json").write_text(
        json.dumps({"cycle_id": "old"}),
        encoding="utf-8",
    )
    (reports_dir / "evolution-20260412.json").write_text(
        json.dumps({"cycle_id": "new"}),
        encoding="utf-8",
    )
    (outbox_dir / "latest.json").write_text(
        json.dumps({"approval_gate": {"state": "fresh"}}),
        encoding="utf-8",
    )

    runtime = load_runtime_state(workspace)

    assert runtime["cycle_id"] == "new"


def test_runtime_state_marks_missing_gate_and_hint(tmp_path):
    workspace = tmp_path / "workspace"
    (workspace / "state" / "reports").mkdir(parents=True)
    (workspace / "state" / "goals").mkdir(parents=True)
    (workspace / "state" / "outbox").mkdir(parents=True)

    runtime = load_runtime_state(workspace)

    assert runtime["approval_gate"] is None
    assert runtime["next_hint"] == "approval gate missing; refresh manually"



def test_runtime_state_prefers_promotions_latest_over_report_index_promotion(tmp_path):
    workspace = tmp_path / "workspace"
    state_dir = workspace / "state"
    reports_dir = state_dir / "reports"
    goals_dir = state_dir / "goals"
    outbox_dir = state_dir / "outbox"
    promotions_dir = state_dir / "promotions"
    reports_dir.mkdir(parents=True)
    goals_dir.mkdir(parents=True)
    outbox_dir.mkdir(parents=True)
    promotions_dir.mkdir(parents=True)

    (reports_dir / "evolution-20260412.json").write_text(
        json.dumps({"cycle_id": "cycle-123", "goal_id": "goal-abc"}),
        encoding="utf-8",
    )
    (goals_dir / "active.json").write_text(
        json.dumps({"active_goal": "goal-abc"}),
        encoding="utf-8",
    )
    (outbox_dir / "report.index.json").write_text(
        json.dumps(
            {
                "status": "PASS",
                "source": str(reports_dir / "evolution-20260412.json"),
                "promotion": {
                    "promotion_candidate_id": "promotion-stale",
                    "candidate_path": str(promotions_dir / "promotion-stale.json"),
                    "review_status": "pending",
                    "decision": "pending",
                },
            }
        ),
        encoding="utf-8",
    )
    (promotions_dir / "latest.json").write_text(
        json.dumps(
            {
                "promotion_candidate_id": "promotion-fresh",
                "review_status": "reviewed",
                "decision": "accept",
            }
        ),
        encoding="utf-8",
    )

    runtime = load_runtime_state(workspace)

    assert runtime["promotion_candidate_id"] == "promotion-fresh"
    assert runtime["promotion_candidate_path"] is None
    assert runtime["review_status"] == "reviewed"
    assert runtime["decision"] == "accept"
    assert runtime["promotion_summary"] == "promotion-fresh | reviewed | accept"


def test_promotion_review_command_updates_candidate(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    promotions_dir = workspace / "state" / "promotions"
    promotions_dir.mkdir(parents=True)
    candidate_path = promotions_dir / "promotion-42.json"
    candidate_path.write_text(
        json.dumps(
            {
                "promotion_candidate_id": "promotion-42",
                "origin_cycle_id": "cycle-123",
                "target_branch": "promote/self-evolving",
                "evidence_refs": ["evidence-88"],
                "review_status": "pending",
                "decision": "pending",
            }
        ),
        encoding="utf-8",
    )
    (promotions_dir / "latest.json").write_text(
        json.dumps(
            {
                "promotion_candidate_id": "promotion-42",
                "candidate_path": str(candidate_path),
                "review_status": "pending",
                "decision": "pending",
            }
        ),
        encoding="utf-8",
    )

    config = Config()
    config.agents.defaults.workspace = str(workspace)
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps(config.model_dump(by_alias=True)), encoding="utf-8")

    monkeypatch.setattr("nanobot.config.loader.load_config", lambda _path=None: config)
    monkeypatch.setattr("nanobot.config.loader.set_config_path", lambda _path: None)

    result = runner.invoke(
        app,
        [
            "promotion",
            "review",
            "promotion-42",
            "--decision",
            "accept",
            "--reason",
            "validated for reviewable branch",
            "--workspace",
            str(workspace),
            "--config",
            str(config_file),
        ],
    )

    assert result.exit_code == 0
    assert "promotion-42" in result.stdout
    assert "accept" in result.stdout
    updated = json.loads(candidate_path.read_text(encoding="utf-8"))
    assert updated["decision"] == "accept"
    assert updated["decision_reason"] == "validated for reviewable branch"
    assert updated["review_status"] == "reviewed"
    accepted = json.loads((promotions_dir / "accepted" / "promotion-42.json").read_text(encoding="utf-8"))
    assert accepted["decision"] == "accept"


def test_gateway_uses_workspace_from_config_by_default(monkeypatch, tmp_path: Path) -> None:
    config_file = tmp_path / "instance" / "config.json"
    config_file.parent.mkdir(parents=True)
    config_file.write_text("{}")

    config = Config()
    config.agents.defaults.workspace = str(tmp_path / "config-workspace")
    seen: dict[str, Path] = {}

    monkeypatch.setattr(
        "nanobot.config.loader.set_config_path",
        lambda path: seen.__setitem__("config_path", path),
    )
    monkeypatch.setattr("nanobot.config.loader.load_config", lambda _path=None: config)
    monkeypatch.setattr(
        "nanobot.cli.commands.sync_workspace_templates",
        lambda path: seen.__setitem__("workspace", path),
    )
    monkeypatch.setattr(
        "nanobot.cli.commands._make_provider",
        lambda _config: (_ for _ in ()).throw(_StopGatewayError("stop")),
    )

    result = runner.invoke(app, ["gateway", "--config", str(config_file)])

    assert isinstance(result.exception, _StopGatewayError)
    assert seen["config_path"] == config_file.resolve()
    assert seen["workspace"] == Path(config.agents.defaults.workspace)


def test_gateway_workspace_option_overrides_config(monkeypatch, tmp_path: Path) -> None:
    config_file = tmp_path / "instance" / "config.json"
    config_file.parent.mkdir(parents=True)
    config_file.write_text("{}")

    config = Config()
    config.agents.defaults.workspace = str(tmp_path / "config-workspace")
    override = tmp_path / "override-workspace"
    seen: dict[str, Path] = {}

    monkeypatch.setattr("nanobot.config.loader.set_config_path", lambda _path: None)
    monkeypatch.setattr("nanobot.config.loader.load_config", lambda _path=None: config)
    monkeypatch.setattr(
        "nanobot.cli.commands.sync_workspace_templates",
        lambda path: seen.__setitem__("workspace", path),
    )
    monkeypatch.setattr(
        "nanobot.cli.commands._make_provider",
        lambda _config: (_ for _ in ()).throw(_StopGatewayError("stop")),
    )

    result = runner.invoke(
        app,
        ["gateway", "--config", str(config_file), "--workspace", str(override)],
    )

    assert isinstance(result.exception, _StopGatewayError)
    assert seen["workspace"] == override
    assert config.workspace_path == override


def test_gateway_uses_config_directory_for_cron_store(monkeypatch, tmp_path: Path) -> None:
    config_file = tmp_path / "instance" / "config.json"
    config_file.parent.mkdir(parents=True)
    config_file.write_text("{}")

    config = Config()
    config.agents.defaults.workspace = str(tmp_path / "config-workspace")
    seen: dict[str, Path] = {}

    monkeypatch.setattr("nanobot.config.loader.set_config_path", lambda _path: None)
    monkeypatch.setattr("nanobot.config.loader.load_config", lambda _path=None: config)
    monkeypatch.setattr("nanobot.config.paths.get_cron_dir", lambda: config_file.parent / "cron")
    monkeypatch.setattr("nanobot.cli.commands.sync_workspace_templates", lambda _path: None)
    monkeypatch.setattr("nanobot.cli.commands._make_provider", lambda _config: object())
    monkeypatch.setattr("nanobot.bus.queue.MessageBus", lambda: object())
    monkeypatch.setattr("nanobot.session.manager.SessionManager", lambda _workspace: object())

    class _StopCron:
        def __init__(self, store_path: Path) -> None:
            seen["cron_store"] = store_path
            raise _StopGatewayError("stop")

    monkeypatch.setattr("nanobot.cron.service.CronService", _StopCron)

    result = runner.invoke(app, ["gateway", "--config", str(config_file)])

    assert isinstance(result.exception, _StopGatewayError)
    assert seen["cron_store"] == config_file.parent / "cron" / "jobs.json"


def test_gateway_uses_configured_port_when_cli_flag_is_missing(monkeypatch, tmp_path: Path) -> None:
    config_file = tmp_path / "instance" / "config.json"
    config_file.parent.mkdir(parents=True)
    config_file.write_text("{}")

    config = Config()
    config.gateway.port = 18791

    monkeypatch.setattr("nanobot.config.loader.set_config_path", lambda _path: None)
    monkeypatch.setattr("nanobot.config.loader.load_config", lambda _path=None: config)
    monkeypatch.setattr("nanobot.cli.commands.sync_workspace_templates", lambda _path: None)
    monkeypatch.setattr(
        "nanobot.cli.commands._make_provider",
        lambda _config: (_ for _ in ()).throw(_StopGatewayError("stop")),
    )

    result = runner.invoke(app, ["gateway", "--config", str(config_file)])

    assert isinstance(result.exception, _StopGatewayError)
    assert "port 18791" in result.stdout


def test_gateway_cli_port_overrides_configured_port(monkeypatch, tmp_path: Path) -> None:
    config_file = tmp_path / "instance" / "config.json"
    config_file.parent.mkdir(parents=True)
    config_file.write_text("{}")

    config = Config()
    config.gateway.port = 18791

    monkeypatch.setattr("nanobot.config.loader.set_config_path", lambda _path: None)
    monkeypatch.setattr("nanobot.config.loader.load_config", lambda _path=None: config)
    monkeypatch.setattr("nanobot.cli.commands.sync_workspace_templates", lambda _path: None)
    monkeypatch.setattr(
        "nanobot.cli.commands._make_provider",
        lambda _config: (_ for _ in ()).throw(_StopGatewayError("stop")),
    )

    result = runner.invoke(app, ["gateway", "--config", str(config_file), "--port", "18792"])

    assert isinstance(result.exception, _StopGatewayError)
    assert "port 18792" in result.stdout
