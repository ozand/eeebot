from pathlib import Path

from surf_consilium.council import run_council


def test_missing_adapters_are_not_reported_as_complete(tmp_path: Path):
    manifest = run_council("question", tmp_path, adapters={})
    assert manifest["completion"] == "FAILED"
    assert (tmp_path / "question.md").read_text(encoding="utf-8") == "question"
    assert (tmp_path / "manifest.json").exists()


def test_successful_stage1_has_explicit_degraded_stage2(tmp_path: Path):
    adapters = {name: (lambda prompt, name=name: f"{name}: answer") for name in ("chatgpt", "gemini", "claude")}
    manifest = run_council("question", tmp_path, adapters=adapters)
    assert manifest["completion"] == "COMPLETE"
    assert len([r for r in manifest["results"] if r["stage"] == "stage2" and r["status"] == "success"]) == 3
