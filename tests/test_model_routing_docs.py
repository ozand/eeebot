from pathlib import Path


ROUTING_DOC = Path(__file__).resolve().parents[1] / "docs" / "MODEL_ROUTING_FALLBACK_V1.md"


def _section(text: str, heading: str, next_heading: str) -> str:
    start = text.index(heading)
    end = text.index(next_heading, start)
    return text[start:end]


def test_active_model_routing_doc_excludes_live_invalid_models() -> None:
    text = ROUTING_DOC.read_text(encoding="utf-8")

    routing_rules = _section(text, "## Routing Rules", "## Detection Rules")
    executor_split = _section(text, "## Minimal Planner/Executor Split", "## Deferred On Purpose")
    active_routing_text = routing_rules + executor_split

    assert "qwen3-coder-flash" not in active_routing_text
    assert "coder-model" not in active_routing_text
    assert "gpt-oss-120b-medium" not in active_routing_text


def test_active_model_routing_doc_uses_verified_codex_for_code_executor() -> None:
    text = ROUTING_DOC.read_text(encoding="utf-8")

    assert "1. `gpt-5.3-codex`" in text
    assert "- `code` executor -> `gpt-5.3-codex`" in text
    assert "invalid model name" in text
