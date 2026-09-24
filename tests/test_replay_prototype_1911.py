"""Tests for the offline replay prototype (#1911)."""
from scripts.prototype_already_implemented import (
    build_evaluation_prompt,
    extract_file_and_commits,
)


def test_build_evaluation_prompt_contains_all_components():
    prompt = build_evaluation_prompt(
        "scripts/test.py",
        "Implement feature X",
        "print('hello')",
        [{"sha": "12345678", "subject": "prior work", "diff": "+print('init')"}],
    )
    assert "scripts/test.py" in prompt
    assert "Implement feature X" in prompt
    assert "print('hello')" in prompt
    assert "12345678" in prompt
    assert "prior work" in prompt


def test_extract_file_and_commits_skips_auto_commits(tmp_path):
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.check_call(["git", "init"], cwd=repo)
    subprocess.check_call(["git", "config", "user.name", "Test"], cwd=repo)
    subprocess.check_call(["git", "config", "user.email", "test@test.com"], cwd=repo)

    target = repo / "script.py"
    target.write_text("v1\n")
    subprocess.check_call(["git", "add", "script.py"], cwd=repo)
    subprocess.check_call(["git", "commit", "-m", "feat: initial script"], cwd=repo)

    target.write_text("v2\n")
    subprocess.check_call(["git", "add", "script.py"], cwd=repo)
    subprocess.check_call(["git", "commit", "-m", "selfevo: auto-commit uncommitted subagent work"], cwd=repo)

    target.write_text("v3\n")
    subprocess.check_call(["git", "add", "script.py"], cwd=repo)
    subprocess.check_call(["git", "commit", "-m", "feat: second feature"], cwd=repo)

    content, commits = extract_file_and_commits(repo, "script.py")
    assert content == "v3\n"
    assert len(commits) == 2
    subjects = [c["subject"] for c in commits]
    assert "feat: second feature" in subjects
    assert "feat: initial script" in subjects
    assert not any("selfevo: auto-commit" in s for s in subjects)

