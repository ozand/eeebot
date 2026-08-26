"""Tests for bridge staged-promotions pickup and defect C clarification. (#1001)"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _init_git_repo(path: Path) -> None:
    """Create a minimal git repo at path with one commit on main."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main", str(path)], capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@test"], capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], capture_output=True)
    (path / "README.md").write_text("init\n")
    subprocess.run(["git", "-C", str(path), "add", "README.md"], capture_output=True)
    subprocess.run(["git", "-C", str(path), "commit", "-m", "init"], capture_output=True)


def _write_manifest(state_dir: Path, entries: list[dict]) -> None:
    """Write a staging manifest and payload files."""
    from nanobot.runtime.knowledge_curator import _STAGED_DIR
    staged_dir = state_dir / "curator" / _STAGED_DIR
    staged_dir.mkdir(parents=True, exist_ok=True)
    for entry in entries:
        slug = entry["payload_file"]
        content = entry.get("_content", f"# {entry['path']}\n\ncontent\n")
        (staged_dir / slug).write_text(content, encoding="utf-8")
    manifest_path = staged_dir / "manifest.json"
    manifest_path.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# Tests for _pickup_staged_promotions
# ---------------------------------------------------------------------------

class TestPickupStagedPromotions:
    def test_pickup_nothing_when_no_manifest(self, tmp_path):
        """No manifest \u2192 returns 0 and touches nothing."""
        from nanobot.runtime.bridge import _pickup_staged_promotions
        repo = tmp_path / "repo"
        _init_git_repo(repo)
        state = tmp_path / "state"
        n = _pickup_staged_promotions(repo, state)
        assert n == 0

    def test_pickup_commits_staged_facts_on_main(self, tmp_path):
        """#1001: pickup copies staged fact into repo and creates a commit on main."""
        from nanobot.runtime.bridge import _pickup_staged_promotions
        repo = tmp_path / "repo"
        _init_git_repo(repo)
        # Setup memory/index.md so index append works
        (repo / "memory").mkdir(parents=True)
        (repo / "memory" / "index.md").write_text("# Index\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "memory/index.md"], capture_output=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-m", "add index"], capture_output=True)
        state = tmp_path / "state"
        _write_manifest(state, [{
            "path": "memory/facts/my-fact.md",
            "action": "create",
            "payload_file": "memory__facts__my-fact.md",
            "index_line": "- [My Fact](memory/facts/my-fact.md)",
            "index_rel": "memory/index.md",
            "_content": "# My Fact\n\nSome knowledge.\n",
        }])
        n = _pickup_staged_promotions(repo, state)
        assert n == 1
        # Fact committed in repo
        assert (repo / "memory" / "facts" / "my-fact.md").exists()
        # Index updated
        index_text = (repo / "memory" / "index.md").read_text()
        assert "My Fact" in index_text
        # Git log shows the pickup commit
        log = subprocess.run(
            ["git", "-C", str(repo), "log", "--oneline", "-1"],
            capture_output=True, text=True,
        ).stdout
        assert "curator:" in log and "fact" in log
        # Staging manifest cleared after success
        from nanobot.runtime.knowledge_curator import load_staged_manifest
        assert load_staged_manifest(state) == []

    def test_pickup_clears_staging_only_after_commit(self, tmp_path):
        """#1001: if commit fails, staging is retained for retry."""
        from nanobot.runtime.bridge import _pickup_staged_promotions
        repo = tmp_path / "not_a_repo"
        repo.mkdir()
        state = tmp_path / "state"
        _write_manifest(state, [{
            "path": "memory/facts/x.md",
            "action": "create",
            "payload_file": "memory__facts__x.md",
            "index_line": "",
            "index_rel": "",
            "_content": "x\n",
        }])
        # Fails because repo is not a git repo \u2014 staging must be retained
        n = _pickup_staged_promotions(repo, state)
        assert n == 0
        from nanobot.runtime.knowledge_curator import load_staged_manifest
        assert len(load_staged_manifest(state)) == 1  # still there

    def test_pickup_idempotent_missing_payload(self, tmp_path):
        """#1001: if payload file missing but fact already in repo, entry is skipped cleanly."""
        from nanobot.runtime.bridge import _pickup_staged_promotions
        repo = tmp_path / "repo"
        _init_git_repo(repo)
        (repo / "memory" / "facts").mkdir(parents=True)
        (repo / "memory" / "facts" / "exists.md").write_text("already\n")
        state = tmp_path / "state"
        from nanobot.runtime.knowledge_curator import _STAGED_DIR
        staged_dir = state / "curator" / _STAGED_DIR
        staged_dir.mkdir(parents=True)
        # Manifest entry with NO payload file (simulates prior partial apply)
        (staged_dir / "manifest.json").write_text(json.dumps([{
            "path": "memory/facts/exists.md",
            "action": "create",
            "payload_file": "memory__facts__exists.md",  # does not exist on disk
            "index_line": "",
            "index_rel": "",
        }]), encoding="utf-8")
        # Should not crash; will attempt commit of the already-present file
        # (git add will be a no-op if committed already, commit may fail)
        n = _pickup_staged_promotions(repo, state)
        # Either 0 (nothing changed) or 1 (committed); no exception thrown
        assert isinstance(n, int)


# ---------------------------------------------------------------------------
# Defect C: _validate_mutation_surfaces vs _classify_mutation_surface (#1001)
# ---------------------------------------------------------------------------

class TestDefectCDenySetClassification:
    def test_validate_allows_release_promotion_metadata(self):
        """#1001 C: memory/facts/release-promotion-metadata.md passes
        _validate_mutation_surfaces (the script-surface gate used by pickup)."""
        from nanobot.runtime.bridge import _validate_mutation_surfaces
        violations = _validate_mutation_surfaces(["memory/facts/release-promotion-metadata.md"])
        assert violations == [], (
            "memory/facts/release-promotion-metadata.md must be allowed by _validate_mutation_surfaces; "
            f"got violations: {violations}"
        )

    def test_classify_denies_via_promotion_token(self):
        """#1001 C (doc): _classify_mutation_surface applies _is_runtime_deny which matches
        the 'promotion' token in the basename. This is the cause of defect C: the cycle gate
        used _classify_mutation_surface and rejected an innocent file. The pickup path uses
        _validate_mutation_surfaces instead, which does NOT call _is_runtime_deny."""
        from nanobot.runtime.bridge import _classify_mutation_surface
        blocked, violations, tier = _classify_mutation_surface(
            ["memory/facts/release-promotion-metadata.md"]
        )
        # Confirm the deny-set token hit via _classify (the original defect)
        assert any("deny-set" in v for v in violations), (
            "Expected _classify_mutation_surface to hit deny-set for 'promotion' token; "
            f"violations: {violations}"
        )

    def test_normal_fact_path_passes_both_gates(self):
        """A typical memory/facts/*.md path (no sensitive token) passes both gates."""
        from nanobot.runtime.bridge import _validate_mutation_surfaces, _classify_mutation_surface
        path = "memory/facts/git-database-permissions.md"
        assert _validate_mutation_surfaces([path]) == []
        blocked, violations, _ = _classify_mutation_surface([path])
        assert not blocked and not violations

    def test_pickup_uses_validate_not_classify(self, tmp_path):
        """#1001 C: pickup must use _validate_mutation_surfaces so
        release-promotion-metadata.md is not rejected by the 'promotion' token."""
        from nanobot.runtime.bridge import _pickup_staged_promotions
        repo = tmp_path / "repo"
        _init_git_repo(repo)
        (repo / "memory" / "facts").mkdir(parents=True)
        state = tmp_path / "state"
        _write_manifest(state, [{
            "path": "memory/facts/release-promotion-metadata.md",
            "action": "create",
            "payload_file": "memory__facts__release-promotion-metadata.md",
            "index_line": "",
            "index_rel": "",
            "_content": "# Promotion metadata\n\ninfo\n",
        }])
        n = _pickup_staged_promotions(repo, state)
        # Must succeed (not 0 due to a surface violation)
        assert n == 1
        assert (repo / "memory" / "facts" / "release-promotion-metadata.md").exists()


# ---------------------------------------------------------------------------
# Overlap test: curator run leaves active checkout clean (#1001 A)
# ---------------------------------------------------------------------------

class TestCuratorCheckoutClean:
    def test_run_curation_leaves_workspace_clean(self, tmp_path):
        """#1001 A: run_curation must not write to the workspace (only staging)."""
        import os
        from nanobot.runtime.knowledge_curator import run_curation

        workspace = tmp_path / "ws"
        (workspace / "lessons").mkdir(parents=True)
        (workspace / "lessons" / "lessons.yaml").write_text(
            "- id: L1\n  title: some insight\n  approach: do x\n",
            encoding="utf-8",
        )
        (workspace / "memory").mkdir(parents=True)
        (workspace / "memory" / "index.md").write_text("# Index\n", encoding="utf-8")
        state = tmp_path / "state"

        def fake_llm(messages, model):
            return json.dumps([{
                "action": "create",
                "path": "memory/facts/insight.md",
                "content": "# Insight\n\nA fact.",
                "index_line": "- [Insight](memory/facts/insight.md)",
                "lesson_id": "L1",
                "reason": "new",
            }])

        # Capture workspace files before
        before = set(workspace.rglob("*"))
        result = run_curation(workspace, state, llm=fake_llm)
        after = set(workspace.rglob("*"))

        assert result["ok"], f"run_curation failed: {result}"
        # No new files in workspace — all staging goes to state_dir
        new_files = after - before
        assert new_files == set(), (
            f"run_curation wrote to workspace checkout: {new_files}"
        )


class TestPickupIndexIdempotency:
    def test_retry_with_existing_index_lines_makes_no_duplicate_or_commit(self, tmp_path):
        """D4: retrying an already-materialized promotion is a no-op."""
        from nanobot.runtime.bridge import _pickup_staged_promotions
        repo = tmp_path / "repo"
        _init_git_repo(repo)
        (repo / "memory").mkdir(parents=True)
        index = repo / "memory" / "index.md"
        lines = [
            "- [My Fact](memory/facts/my-fact.md)",
            "- [Other Fact](memory/facts/other-fact.md)",
            "- [Third Fact](memory/facts/third-fact.md)",
        ]
        index.write_text("# Index\n\n" + "\n".join(lines) + "\n", encoding="utf-8")
        (repo / "memory" / "facts").mkdir()
        for name in ("my-fact", "other-fact", "third-fact"):
            (repo / "memory" / "facts" / f"{name}.md").write_text(f"# {name}\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "memory"], capture_output=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-m", "existing curator facts"], capture_output=True)
        before = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
        state = tmp_path / "state"
        entries = []
        for name, title in zip(("my-fact", "other-fact", "third-fact"), ("My Fact", "Other Fact", "Third Fact")):
            entries.append({
                "path": f"memory/facts/{name}.md", "action": "create",
                "payload_file": f"memory__facts__{name}.md",
                "index_line": f"- [{title}](memory/facts/{name}.md)",
                "index_rel": "memory/index.md", "_content": f"# {name}\n",
            })
        _write_manifest(state, entries)
        assert _pickup_staged_promotions(repo, state) == 0
        assert subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip() == before
        updated = index.read_text(encoding="utf-8")
        for line in lines:
            assert updated.splitlines().count(line) == 1
