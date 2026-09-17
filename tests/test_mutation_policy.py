from __future__ import annotations

import pytest

from nanobot.agent.context import ContextBuilder
from nanobot.runtime import bridge, gate, llm_proposer
from nanobot.runtime.mutation_policy import MutationPolicy, MutationPolicyError, MUTATION_POLICY


def test_agents_md_is_readable_and_commit_permitted_as_exact_path() -> None:
    # ADR-022 decision 6: the instance AGENTS.md is loop-owned (repository
    # layout only, bounded by the gate), so it is both a bootstrap read and an
    # exact commit path — never a prefix.
    assert "AGENTS.md" in MUTATION_POLICY.read_paths
    assert "AGENTS.md" in MUTATION_POLICY.commit_exact_paths
    assert "AGENTS.md" in MUTATION_POLICY.commit_surfaces
    # #1725: BOOTSTRAP_FILES is now the loop profile's ordered
    # (root_kind, filename, cap, required) block list; the workspace-tagged
    # subset must stay exactly MUTATION_POLICY.read_paths, not a literal, so
    # the two can never drift apart.
    workspace_blocks = [b for b in ContextBuilder.BOOTSTRAP_FILES if b[0] == "workspace"]
    assert [name for _, name, _, _ in workspace_blocks] == list(MUTATION_POLICY.read_paths)
    assert MUTATION_POLICY.commit_path_prefixes == gate._ALLOWED_PATH_PREFIXES
    assert MUTATION_POLICY.commit_path_prefixes == bridge._ALLOWED_PATH_PREFIXES
    assert MUTATION_POLICY.commit_exact_paths == gate._ALLOWED_EXACT_PATHS
    assert MUTATION_POLICY.commit_exact_paths == bridge._ALLOWED_EXACT_PATHS
    assert MUTATION_POLICY.commit_exact_paths == llm_proposer._ALLOWED_EXACT_PATHS


def test_release_owned_files_are_blocked_in_every_mirror() -> None:
    expected = frozenset({"goals.md", "IDENTITY.md", "SOUL.md", "USER.md", "OPERATING.md"})
    assert frozenset(MUTATION_POLICY.immutable_files) == expected
    for module in (gate, bridge, llm_proposer):
        assert expected <= module._BLOCKED_EXACT_PATHS, module.__name__
        assert "agents_md_consolidate.py" in module._BLOCKED_EXACT_PATHS
    assert gate._BLOCKED_EXACT_PATHS == bridge._BLOCKED_EXACT_PATHS == llm_proposer._BLOCKED_EXACT_PATHS
    for name in sorted(expected):
        assert gate._validate_mutation_surfaces([name]) == [f"immutable file blocked from mutation: {name}"]
        assert bridge._validate_mutation_surfaces([name]) == [f"immutable file blocked from mutation: {name}"]
        blocked, violations, _tier = gate._classify_mutation_surface([name])
        assert blocked == [f"immutable file blocked from mutation: {name}"] and violations == []


def test_rendered_do_not_modify_names_control_plane_and_release_files() -> None:
    block = MUTATION_POLICY.render_bridge_surface_block()
    assert "Do NOT modify: state/, ops/, goals.md, IDENTITY.md, SOUL.md, USER.md, OPERATING.md, secrets, or systemd units." in block
    assert "Allowed targets: surfaces/, scripts/, memory/, lessons/, docs/, tests/, skills/, AGENTS.md" in block


def test_agents_md_scope_bound() -> None:
    layout = "# AGENTS.md\n\n## Working knowledge\n- x\n\n## Canonical skill path layout\n- skills/<name>/SKILL.md\n"
    assert MUTATION_POLICY.agents_md_scope_violations(layout) == []
    too_long = "\n".join(f"- line {i}" for i in range(151))
    [long_violation] = MUTATION_POLICY.agents_md_scope_violations(too_long)
    assert long_violation == "agents_md_scope: 151 lines exceeds 150"
    with_rules = layout + "\n## Immediate skip protocol\n- skip\n\n## Turn budget checkpoints (80-iteration cycles)\n- pace\n"
    violations = MUTATION_POLICY.agents_md_scope_violations(with_rules)
    assert violations == [
        "agents_md_scope: runtime heading belongs to OPERATING.md: ## Immediate skip protocol",
        "agents_md_scope: runtime heading belongs to OPERATING.md: ## Turn budget checkpoints (80-iteration cycles)",
    ]
    # A repo-layout heading that merely shares a word is not a runtime heading.
    assert MUTATION_POLICY.agents_md_scope_violations("## Identity of test fixtures\n") == []
    assert MUTATION_POLICY.agents_md_scope_violations("## Identity\n") != []


def test_gate_agents_md_scope_runs_only_when_agents_md_changed(tmp_path) -> None:
    import subprocess
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "AGENTS.md").write_text("# AGENTS.md\n\n## Cycle contract\n- rules\n", encoding="utf-8")
    subprocess.run(["git", "add", "AGENTS.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=repo, check=True)
    assert gate._agents_md_scope_violations(repo, ["scripts/x.py"]) == []
    assert gate._agents_md_scope_violations(repo, ["AGENTS.md"]) == [
        "agents_md_scope: runtime heading belongs to OPERATING.md: ## Cycle contract",
    ]
    subprocess.run(["git", "rm", "-q", "AGENTS.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "delete"], cwd=repo, check=True)
    [missing] = gate._agents_md_scope_violations(repo, ["AGENTS.md"])
    assert missing.startswith("agents_md_scope: AGENTS.md missing at HEAD")


def test_bridge_no_longer_renders_surfaces_and_points_to_operating_md() -> None:
    """#1723(b): build_task rendered its own mutation-surface block (a second
    copy of the policy) until this issue moved it to OPERATING.md, loaded
    into the system prompt by the loop-profile loader (#1725). The
    byte-for-byte parity check against `MUTATION_POLICY.render_bridge_surface_block()`
    now lives in tests/test_operating_md.py; here we only assert build_task
    stopped duplicating it."""
    prompt = bridge.build_task(
        {"task_title": "x", "request_id": "r", "cycle_id": "c", "goal_id": "g"},
        "derived",
        "",
    )
    with pytest.raises(MutationPolicyError):
        MUTATION_POLICY.validate_rendered_surfaces(prompt)
    assert "Rules: see OPERATING.md in your system prompt." in prompt


def test_proposer_renderings_match_policy() -> None:
    expected = MUTATION_POLICY.render_commit_surfaces()
    assert expected in llm_proposer._PROPOSER_SYSTEM_PROMPT
    assert expected in llm_proposer._DEMAND_PROPOSER_SYSTEM_PROMPT
    MUTATION_POLICY.validate_rendered_surfaces(llm_proposer._PROPOSER_SYSTEM_PROMPT)
    MUTATION_POLICY.validate_rendered_surfaces(llm_proposer._DEMAND_PROPOSER_SYSTEM_PROMPT)


def test_gate_consults_authoritative_policy() -> None:
    assert gate._validate_mutation_surfaces(["scripts/x.py"]) == []
    assert gate._validate_mutation_surfaces(["AGENTS.md"]) == []
    assert bridge._validate_mutation_surfaces(["AGENTS.md"]) == []
    blocked, violations, tier = gate._classify_mutation_surface(["AGENTS.md"])
    assert (blocked, violations, tier) == ([], [], "script")
    # Nested copies are not the exact root allowance.
    assert gate._validate_mutation_surfaces(["other/AGENTS.md"]) != []


def test_malformed_policy_fails_closed_with_diagnostic() -> None:
    bad = MutationPolicy(
        read_paths=("AGENTS.md",),
        commit_path_prefixes=("AGENTS.md",),
        commit_exact_paths=frozenset(),
    )
    diagnostic = __import__("nanobot.runtime.mutation_policy", fromlist=["policy_mismatch_diagnostic"]).policy_mismatch_diagnostic(bad)
    assert diagnostic is not None
    assert "mutation policy mismatch" in diagnostic
    with pytest.raises(MutationPolicyError):
        bad.render_commit_surfaces()


def test_effective_commit_permission_set() -> None:
    assert MUTATION_POLICY.commit_surfaces == (
        "surfaces/", "scripts/", "memory/", "lessons/", "docs/", "tests/", "skills/", "AGENTS.md",
    )
    assert MUTATION_POLICY.commit_exact_paths == frozenset({"AGENTS.md"})


def test_policy_rejects_release_file_or_forbidden_dir_on_commit_surface() -> None:
    with pytest.raises(MutationPolicyError, match="release-owned goals.md"):
        MutationPolicy(
            read_paths=("AGENTS.md",),
            commit_path_prefixes=("scripts/",),
            commit_exact_paths=frozenset({"goals.md"}),
        ).validate()
    with pytest.raises(MutationPolicyError, match="forbidden directory"):
        MutationPolicy(
            read_paths=("AGENTS.md",),
            commit_path_prefixes=("scripts/", "ops/"),
            commit_exact_paths=frozenset(),
        ).validate()
