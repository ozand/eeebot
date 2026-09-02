from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
README = REPO_ROOT / "README.md"
DOCS_INDEX = REPO_ROOT / "docs" / "README.md"


def test_readme_naming_docs_describe_final_state() -> None:
    """Keep the public README wording aligned with the #619 naming decision."""
    readme = README.read_text(encoding="utf-8")
    docs_index = DOCS_INDEX.read_text(encoding="utf-8")

    for text in (readme, docs_index):
        assert "permanent" in text
        assert "nanobot" in text
        assert "eeebot" in text
        assert "compatibility window" not in text
        assert "in progress" not in text

    assert "final state" in readme
    assert "internal imports remain in the `nanobot` package permanently" in readme
    assert "final nanobot→eeebot naming state" in docs_index
