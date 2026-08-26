

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
