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
    assert "Allowed targets: surfaces/, scripts/, memory/, lessons/, docs/, tests/, skills/, diary/, AGENTS.md" in block


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


# ---------------------------------------------------------------------------
# #1750: the ratchet -- old_text turns the absolute bound into a
# no-regression check once HEAD is already non-compliant, so a commit that
# strictly improves a 192-line/9-heading file is not rejected forever by the
# rule meant to fix it.
# ---------------------------------------------------------------------------

from pathlib import Path as _Path

_REAL_INSTANCE_AGENTS_MD = (
    _Path(__file__).resolve().parent / "fixtures" / "instance_agents_md" / "AGENTS.md"
).read_text(encoding="utf-8")


def test_ratchet_accepts_strict_improvement_over_noncompliant_head() -> None:
    """HEAD non-compliant (192 lines, 9 headings) + staged version strictly
    better (fewer lines, one forbidden heading removed, none reintroduced) ->
    accepted."""
    old = _REAL_INSTANCE_AGENTS_MD
    lines = old.splitlines()
    # Remove the first forbidden-heading section (its heading line plus the
    # next two lines) to produce a shorter, strictly-improved version.
    heading = MUTATION_POLICY.agents_md_runtime_headings[0]
    idx = next(i for i, line in enumerate(lines) if line.strip() == heading)
    new_lines = lines[:idx] + lines[idx + 3:]
    new = "\n".join(new_lines) + "\n"
    assert len(new_lines) < len(lines)
    assert heading not in MUTATION_POLICY._agents_md_present_headings(new)

    assert MUTATION_POLICY._agents_md_absolute_violations(old) != []  # HEAD non-compliant
    assert MUTATION_POLICY.agents_md_scope_violations(new, old_text=old) == []


def test_ratchet_accepts_same_line_count_and_same_heading_set() -> None:
    """HEAD non-compliant + staged version the SAME length with the SAME
    heading set (no improvement, but no regression either) -> accepted.

    Decision (stated per the task): ``lines(new) <= lines(old)`` is the
    literal bound the issue and the dispatch both specify -- equality
    satisfies it, so a same-shape edit (e.g. a wording fix that changes no
    line count and touches no heading) is accepted, not rejected for failing
    to also improve. The ratchet's job is "no regression", not "forced
    progress every single commit" -- a bounded executor may need more than
    one cycle to whittle the file down.
    """
    old = _REAL_INSTANCE_AGENTS_MD
    lines = old.splitlines()
    # Reword one non-heading line without changing the line count or any heading.
    idx = next(i for i, line in enumerate(lines) if line.strip() and not line.startswith("#"))
    new_lines = list(lines)
    new_lines[idx] = new_lines[idx] + " (reworded)"
    new = "\n".join(new_lines) + "\n"
    assert len(new_lines) == len(lines)
    assert MUTATION_POLICY._agents_md_present_headings(new) == MUTATION_POLICY._agents_md_present_headings(old)

    assert MUTATION_POLICY.agents_md_scope_violations(new, old_text=old) == []


def test_ratchet_rejects_line_count_regression() -> None:
    """HEAD non-compliant + staged version adds a line beyond lines(old) ->
    violation naming the line regression."""
    old = _REAL_INSTANCE_AGENTS_MD
    new = old + "\n- one more line\n"
    violations = MUTATION_POLICY.agents_md_scope_violations(new, old_text=old)
    assert len(violations) == 1
    assert violations[0].startswith("agents_md_scope: lines regressed from")
    assert f"to {len(new.splitlines())}" in violations[0]


def test_ratchet_rejects_reintroduced_heading() -> None:
    """HEAD non-compliant but missing one forbidden heading + staged version
    reintroduces exactly that heading -> violation naming it, even though the
    line count does not regress."""
    old_lines = _REAL_INSTANCE_AGENTS_MD.splitlines()
    heading = MUTATION_POLICY.agents_md_runtime_headings[0]
    idx = next(i for i, line in enumerate(old_lines) if line.strip() == heading)
    # old is missing this one heading (but still non-compliant via the rest).
    old = "\n".join(old_lines[:idx] + old_lines[idx + 1:]) + "\n"
    assert heading not in MUTATION_POLICY._agents_md_present_headings(old)
    assert MUTATION_POLICY._agents_md_absolute_violations(old) != []

    # new reintroduces it, same line count as old (no line regression).
    new_lines = old.splitlines()
    new_lines.insert(idx, heading)
    new = "\n".join(new_lines[: len(old_lines) - 1]) + "\n"  # keep <= old's length
    assert heading in MUTATION_POLICY._agents_md_present_headings(new)

    violations = MUTATION_POLICY.agents_md_scope_violations(new, old_text=old)
    assert any(
        v == f"agents_md_scope: reintroduced forbidden heading absent from HEAD: {heading}"
        for v in violations
    ), violations


def test_append_only_surfaces_are_memory_and_lessons() -> None:
    """#1768 Part 1: the append-only rule's declared scope. gate.py reads
    these two fields directly (never a literal), so this is the one place
    the surface list is pinned."""
    assert MUTATION_POLICY.append_only_prefixes == ("memory/", "lessons/")
    assert MUTATION_POLICY.append_only_no_shrink_files == ("memory/HISTORY.md",)
    # Every no-shrink file lives under a declared append-only prefix -- the
    # per-line rule is a refinement of the per-file rule, not a separate one.
    for path in MUTATION_POLICY.append_only_no_shrink_files:
        assert any(path.startswith(prefix) for prefix in MUTATION_POLICY.append_only_prefixes)


def test_append_only_prefixes_must_be_slash_terminated() -> None:
    from nanobot.runtime.mutation_policy import MutationPolicy, MutationPolicyError

    with pytest.raises(MutationPolicyError, match="append_only_prefixes"):
        MutationPolicy(
            read_paths=("AGENTS.md",),
            commit_path_prefixes=("scripts/",),
            commit_exact_paths=frozenset(),
            append_only_prefixes=("memory",),  # missing trailing slash
        ).validate()


def test_append_only_no_shrink_files_must_be_non_empty_strings() -> None:
    from nanobot.runtime.mutation_policy import MutationPolicy, MutationPolicyError

    with pytest.raises(MutationPolicyError, match="append_only_no_shrink_files"):
        MutationPolicy(
            read_paths=("AGENTS.md",),
            commit_path_prefixes=("scripts/",),
            commit_exact_paths=frozenset(),
            append_only_no_shrink_files=("",),
        ).validate()


def test_ratchet_rejects_longer_new_even_with_fewer_headings() -> None:
    """New longer than old -> rejected even though it removed a heading --
    both axes must hold, neither alone is sufficient."""
    old = _REAL_INSTANCE_AGENTS_MD
    lines = old.splitlines()
    heading = MUTATION_POLICY.agents_md_runtime_headings[0]
    idx = next(i for i, line in enumerate(lines) if line.strip() == heading)
    # Remove the heading's own line only (one fewer heading) but pad well
    # past old's original length -- net longer overall.
    new_lines = lines[:idx] + lines[idx + 1:] + [f"- padding {i}" for i in range(20)]
    new = "\n".join(new_lines) + "\n"
    assert len(new_lines) > len(lines)

    violations = MUTATION_POLICY.agents_md_scope_violations(new, old_text=old)
    assert any(v.startswith("agents_md_scope: lines regressed from") for v in violations)


def test_absolute_bound_applies_when_head_is_already_compliant() -> None:
    """HEAD compliant + staged version violating -> rejected under the
    absolute bound, same behaviour as before #1750 (no ratchet applies)."""
    compliant_old = "# AGENTS.md\n\n## Repository layout\n- x\n"
    violating_new = compliant_old + "\n## Cycle contract\n- rules\n"
    assert MUTATION_POLICY._agents_md_absolute_violations(compliant_old) == []

    violations = MUTATION_POLICY.agents_md_scope_violations(violating_new, old_text=compliant_old)
    assert violations == [
        "agents_md_scope: runtime heading belongs to OPERATING.md: ## Cycle contract",
    ]


def test_gate_agents_md_scope_runs_only_when_agents_md_changed(tmp_path) -> None:
    """No ratchet baseline here: AGENTS.md does not exist at ``base_sha``
    (the initial commit, before it was added), so ``old_text=None`` and the
    absolute bound applies exactly as before #1750 -- these assertions are
    unchanged from pre-#1750 behaviour."""
    import subprocess
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True)
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()
    (repo / "AGENTS.md").write_text("# AGENTS.md\n\n## Cycle contract\n- rules\n", encoding="utf-8")
    subprocess.run(["git", "add", "AGENTS.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=repo, check=True)
    assert gate._agents_md_scope_violations(repo, base_sha, ["scripts/x.py"]) == []
    assert gate._agents_md_scope_violations(repo, base_sha, ["AGENTS.md"]) == [
        "agents_md_scope: runtime heading belongs to OPERATING.md: ## Cycle contract",
    ]
    subprocess.run(["git", "rm", "-q", "AGENTS.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "delete"], cwd=repo, check=True)
    [missing] = gate._agents_md_scope_violations(repo, base_sha, ["AGENTS.md"])
    assert missing.startswith("agents_md_scope: AGENTS.md missing at HEAD")


def test_gate_agents_md_scope_unreadable_base_fails_closed(tmp_path, monkeypatch) -> None:
    """A base-sha read that raises (corrupt object, timeout, ...) fails
    closed with a distinct message -- unlike a clean non-zero exit (the file
    simply not existing at base, covered above), which is not a failure."""
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "AGENTS.md").write_text("# AGENTS.md\n\n## Repository layout\n- x\n", encoding="utf-8")
    subprocess.run(["git", "add", "AGENTS.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=repo, check=True)

    real_run = subprocess.run

    def _boom(cmd, *args, **kwargs):
        if cmd[:2] == ["git", "show"] and cmd[2].startswith("deadbeef"):
            raise TimeoutError("simulated git timeout reading base")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _boom)
    [violation] = gate._agents_md_scope_violations(repo, "deadbeef" * 5, ["AGENTS.md"])
    assert violation.startswith("agents_md_scope: unreadable at base deadbeef")


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
        "surfaces/", "scripts/", "memory/", "lessons/", "docs/", "tests/", "skills/", "diary/", "AGENTS.md",
    )
    assert MUTATION_POLICY.commit_exact_paths == frozenset({"AGENTS.md"})


def test_forbidden_path_is_rejected_even_when_nested_in_a_commit_prefix() -> None:
    policy = MutationPolicy(
        read_paths=("AGENTS.md",),
        commit_path_prefixes=("scripts/", "state/curator/"),
        commit_exact_paths=frozenset(),
        forbidden_dirs=("state/",),
    )
    policy.validate()
    assert policy.forbidden_path_violations(["state/curator/staged/fact.md"]) == [
        "forbidden directory blocked from mutation: state/ (state/curator/staged/fact.md)"
    ]


def test_gate_rejects_forbidden_path_before_allowlist_can_permit_it(monkeypatch) -> None:
    from nanobot.runtime import gate

    policy = MutationPolicy(
        read_paths=("AGENTS.md",),
        commit_path_prefixes=("scripts/", "state/curator/"),
        commit_exact_paths=frozenset(),
        forbidden_dirs=("state/",),
    )
    monkeypatch.setattr(gate, "MUTATION_POLICY", policy)
    monkeypatch.setattr(gate, "_ALLOWED_PATH_PREFIXES", policy.commit_path_prefixes)
    monkeypatch.setattr(gate, "_ALLOWED_EXACT_PATHS", policy.commit_exact_paths)

    assert gate._validate_mutation_surfaces(
        ["state/curator/staged/fact.md"],
        allowed_exact_paths=policy.commit_exact_paths,
        allowed_path_prefixes=policy.commit_path_prefixes,
    ) == [
        "forbidden directory blocked from mutation: state/ (state/curator/staged/fact.md)"
    ]


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
