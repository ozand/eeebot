"""Tests for #749: the deterministic (no-LLM) SYSTEM_MAP generator and its
watermark + content-hash no-op gate.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from nanobot.runtime import system_map


def _git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "selfevo_repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=repo, check=True)
    return repo


def _commit_all(repo: Path, message: str) -> None:
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=repo, check=True)


def _write_script(repo: Path, rel_path: str, content: str) -> Path:
    path = repo / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


# ─── docstring / description extraction ───────────────────────────────────


class TestDescriptionExtraction:
    def test_docstring_first_line(self, tmp_path):
        repo = _git_repo(tmp_path)
        _write_script(
            repo,
            "scripts/track_memory.py",
            '"""Track memory usage over time.\n\nMore detail here.\n"""\nimport os\n',
        )
        lines = system_map.inventory_lines(repo)
        assert lines == ["- scripts/track_memory.py — Track memory usage over time."]

    def test_comment_fallback(self, tmp_path):
        repo = _git_repo(tmp_path)
        _write_script(
            repo,
            "scripts/no_docstring.py",
            "#!/usr/bin/env python\n# Reports disk usage as a table.\nimport os\n",
        )
        lines = system_map.inventory_lines(repo)
        assert lines == ["- scripts/no_docstring.py — Reports disk usage as a table."]

    def test_no_description_fallback(self, tmp_path):
        repo = _git_repo(tmp_path)
        _write_script(repo, "scripts/bare.py", "import os\nprint(os.getcwd())\n")
        lines = system_map.inventory_lines(repo)
        assert lines == ["- scripts/bare.py — (no description)"]

    def test_unparseable_source_falls_back_gracefully(self, tmp_path):
        repo = _git_repo(tmp_path)
        _write_script(repo, "scripts/broken.py", "def f(:\n    pass\n")
        lines = system_map.inventory_lines(repo)
        assert lines == ["- scripts/broken.py — (no description)"]

    def test_sorted_by_name(self, tmp_path):
        repo = _git_repo(tmp_path)
        _write_script(repo, "scripts/zeta.py", "'''z'''\n")
        _write_script(repo, "scripts/alpha.py", "'''a'''\n")
        lines = system_map.inventory_lines(repo)
        assert lines == ["- scripts/alpha.py — a", "- scripts/zeta.py — z"]


# ─── near-duplicate grouping ───────────────────────────────────────────────


class TestNearDuplicateGrouping:
    def test_track_and_monitor_memory_grouped(self):
        groups = system_map.near_duplicate_groups(
            ["scripts/track_memory.py", "scripts/monitor_memory.py"]
        )
        assert groups == [["scripts/monitor_memory.py", "scripts/track_memory.py"]]

    def test_unrelated_scripts_not_grouped(self):
        groups = system_map.near_duplicate_groups(
            ["scripts/track_memory.py", "scripts/deploy_release.py"]
        )
        assert groups == []

    def test_group_requires_at_least_two_members(self):
        groups = system_map.near_duplicate_groups(["scripts/track_memory.py"])
        assert groups == []

    def test_three_way_group(self):
        groups = system_map.near_duplicate_groups(
            [
                "scripts/track_memory.py",
                "scripts/monitor_memory.py",
                "scripts/memory_report.py",
            ]
        )
        assert len(groups) == 1
        assert set(groups[0]) == {
            "scripts/track_memory.py",
            "scripts/monitor_memory.py",
            "scripts/memory_report.py",
        }


# ─── generate_system_map ───────────────────────────────────────────────────


class TestGenerateSystemMap:
    def test_includes_header_and_inventory(self, tmp_path):
        repo = _git_repo(tmp_path)
        _write_script(repo, "scripts/track_memory.py", '"""Track memory."""\n')
        content = system_map.generate_system_map(repo)
        assert content.startswith("# SYSTEM MAP")
        assert "## Inventory" in content
        assert "- scripts/track_memory.py — Track memory." in content

    def test_near_duplicate_section_present(self, tmp_path):
        repo = _git_repo(tmp_path)
        _write_script(repo, "scripts/track_memory.py", '"""Track."""\n')
        _write_script(repo, "scripts/monitor_memory.py", '"""Monitor."""\n')
        content = system_map.generate_system_map(repo)
        assert "## Near-duplicate candidates" in content
        assert "scripts/monitor_memory.py, scripts/track_memory.py" in content

    def test_empty_repo_has_no_scripts_placeholder(self, tmp_path):
        repo = _git_repo(tmp_path)
        content = system_map.generate_system_map(repo)
        assert "(no scripts found)" in content
        assert "(none detected)" in content

    def test_backlog_and_completed_carried_over_verbatim(self, tmp_path):
        repo = _git_repo(tmp_path)
        _write_script(repo, "scripts/track_memory.py", '"""Track."""\n')
        docs = repo / "docs"
        docs.mkdir()
        old_map = (
            "# SYSTEM MAP\n\n"
            "## Inventory\n\n- scripts/old.py — stale\n\n"
            "## Near-duplicate candidates\n\n(none detected)\n\n"
            "## Backlog\n\n- noticed: needs a retry helper\n\n"
            "## Completed\n\n- done: abc1234 added track_memory.py\n"
        )
        (docs / "SYSTEM_MAP.md").write_text(old_map, encoding="utf-8")

        content = system_map.generate_system_map(repo)
        assert "## Backlog\n\n- noticed: needs a retry helper" in content
        assert "## Completed\n\n- done: abc1234 added track_memory.py" in content
        # Inventory is regenerated, not carried over from the stale map.
        assert "scripts/old.py" not in content
        assert "scripts/track_memory.py — Track." in content

    def test_no_prior_map_omits_backlog_and_completed(self, tmp_path):
        repo = _git_repo(tmp_path)
        content = system_map.generate_system_map(repo)
        assert "## Backlog" not in content
        assert "## Completed" not in content


# ─── update_system_map: watermark + content-hash no-op gate ───────────────


class TestUpdateSystemMap:
    def test_first_call_writes_file_and_watermark(self, tmp_path):
        repo = _git_repo(tmp_path)
        _write_script(repo, "scripts/track_memory.py", '"""Track."""\n')
        state_dir = tmp_path / "state"

        wrote = system_map.update_system_map(repo, state_dir)

        assert wrote is True
        assert (repo / "docs" / "SYSTEM_MAP.md").is_file()
        watermark = json.loads((state_dir / "system_map" / "watermark.json").read_text())
        assert watermark["git_head"]
        assert watermark["content_sha256"]
        assert watermark["updated_at_utc"]

    def test_unchanged_head_is_noop(self, tmp_path):
        repo = _git_repo(tmp_path)
        state_dir = tmp_path / "state"
        assert system_map.update_system_map(repo, state_dir) is True

        map_path = repo / "docs" / "SYSTEM_MAP.md"
        mtime_before = map_path.stat().st_mtime
        wm_before = (state_dir / "system_map" / "watermark.json").read_text()

        wrote_again = system_map.update_system_map(repo, state_dir)

        assert wrote_again is False
        assert map_path.stat().st_mtime == mtime_before
        assert (state_dir / "system_map" / "watermark.json").read_text() == wm_before

    def test_head_moved_but_content_identical_skips_write(self, tmp_path):
        repo = _git_repo(tmp_path)
        state_dir = tmp_path / "state"
        assert system_map.update_system_map(repo, state_dir) is True

        map_path = repo / "docs" / "SYSTEM_MAP.md"
        mtime_before = map_path.stat().st_mtime
        wm_before = (state_dir / "system_map" / "watermark.json").read_text()

        # Move HEAD without changing anything under scripts/surfaces (a docs
        # or unrelated commit) — regenerated content is byte-identical.
        (repo / "unrelated.txt").write_text("x\n", encoding="utf-8")
        _commit_all(repo, "chore: unrelated change")

        wrote = system_map.update_system_map(repo, state_dir)

        assert wrote is False
        assert map_path.stat().st_mtime == mtime_before
        assert (state_dir / "system_map" / "watermark.json").read_text() == wm_before

    def test_head_moved_and_content_changed_rewrites(self, tmp_path):
        repo = _git_repo(tmp_path)
        state_dir = tmp_path / "state"
        assert system_map.update_system_map(repo, state_dir) is True

        _write_script(repo, "scripts/new_script.py", '"""A new script."""\n')
        _commit_all(repo, "feat: add new_script.py")

        wrote = system_map.update_system_map(repo, state_dir)

        assert wrote is True
        content = (repo / "docs" / "SYSTEM_MAP.md").read_text()
        assert "scripts/new_script.py" in content

    def test_missing_repo_fails_open(self, tmp_path):
        assert system_map.update_system_map(tmp_path / "nope", tmp_path / "state") is False

    def test_missing_git_fails_open(self, tmp_path):
        not_a_repo = tmp_path / "not_a_repo"
        not_a_repo.mkdir()
        assert system_map.update_system_map(not_a_repo, tmp_path / "state") is False

    def test_does_not_raise_on_unwritable_target(self, tmp_path, monkeypatch):
        repo = _git_repo(tmp_path)

        def _boom(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(system_map.Path, "write_text", _boom, raising=False)
        # Should degrade to False, never raise.
        assert system_map.update_system_map(repo, tmp_path / "state") is False


# ─── parse_inventory_section ───────────────────────────────────────────────


class TestParseInventorySection:
    def test_extracts_bullet_lines(self):
        markdown = (
            "# SYSTEM MAP\n\n## Inventory\n\n"
            "- scripts/a.py — a\n- scripts/b.py — b\n\n"
            "## Near-duplicate candidates\n\n(none detected)\n"
        )
        assert system_map.parse_inventory_section(markdown) == [
            "- scripts/a.py — a",
            "- scripts/b.py — b",
        ]

    def test_missing_section_returns_empty(self):
        assert system_map.parse_inventory_section("# SYSTEM MAP\n\nno sections here\n") == []
