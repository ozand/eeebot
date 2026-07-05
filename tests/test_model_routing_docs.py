from pathlib import Path


# Canonical routing truth lives in docs/specs/model-routing/spec.md. The
# predecessor doc (.legacy/docs/MODEL_ROUTING_FALLBACK_V1.md) was removed
# 2026-07-05 (#613) when `.legacy/` was deleted; these assertions were
# rebased onto the current spec.
ROUTING_DOC = (
    Path(__file__).resolve().parents[1]
    / "docs" / "specs" / "model-routing" / "spec.md"
)


def test_active_model_routing_doc_excludes_live_invalid_models() -> None:
    text = ROUTING_DOC.read_text(encoding="utf-8")

    assert "gpt-oss-120b-medium" not in text
    # qwen3-coder-flash and coder-model are documented only as rejected models,
    # never as an active route.
    assert "were rejected at" in text


def test_active_model_routing_doc_uses_verified_codex_for_code_executor() -> None:
    text = ROUTING_DOC.read_text(encoding="utf-8")

    assert "`un/qwen3.6-27b-mtp`" in text
    assert "only model in the" in text
    assert "`code` fallback list" in text
    assert "invalid model name" in text
