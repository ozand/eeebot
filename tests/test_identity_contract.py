from pathlib import Path

from nanobot.agent.context import ContextBuilder
from nanobot.agent.skills import SkillsLoader
from nanobot.agent.subagent import SubagentManager
from nanobot.bus.queue import MessageBus
from nanobot.runtime import bridge, llm_proposer


IDENTITY = Path(__file__).parents[1] / "IDENTITY.md"


def test_loop_system_context_contains_identity_verbatim(tmp_path):
    content = IDENTITY.read_text(encoding="utf-8")
    class Provider:
        def get_default_model(self): return "test"
    manager = SubagentManager(
        Provider(), tmp_path, MessageBus(),
        system_context="# Loop agent identity\n\n" + content,
    )
    assert content.rstrip() in manager._build_subagent_prompt()


def test_bridge_gate_rejects_identity():
    assert any("IDENTITY.md" in x for x in bridge._validate_mutation_surfaces(["IDENTITY.md"]))


def test_proposer_rejects_identity():
    ok, reason = llm_proposer.validate_sizing({
        "task_title": "x", "rationale": "x", "serves": "priority 1",
        "target_path": "IDENTITY.md",
    })
    assert ok is False
    assert "immutable" in reason


def test_interactive_context_builder_keeps_nanobot_template(tmp_path):
    (tmp_path / "IDENTITY.md").write_text("LOOP IDENTITY SENTINEL", encoding="utf-8")
    prompt = ContextBuilder(tmp_path).build_system_prompt()
    assert "You are nanobot, a helpful AI assistant." in prompt
    assert "LOOP IDENTITY SENTINEL" not in prompt
