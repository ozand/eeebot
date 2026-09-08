from __future__ import annotations

import pytest

from nanobot.agent.context import ContextBuilder
from nanobot.runtime import bridge, gate, llm_proposer
from nanobot.runtime.mutation_policy import MutationPolicy, MutationPolicyError, MUTATION_POLICY


def test_read_and_commit_permissions_are_separate() -> None:
    assert "AGENTS.md" in MUTATION_POLICY.read_paths
    assert "AGENTS.md" not in MUTATION_POLICY.commit_surfaces
    assert ContextBuilder.BOOTSTRAP_FILES == ["AGENTS.md"]
    assert MUTATION_POLICY.commit_path_prefixes == gate._ALLOWED_PATH_PREFIXES
    assert MUTATION_POLICY.commit_path_prefixes == bridge._ALLOWED_PATH_PREFIXES


def test_bridge_rendering_matches_policy() -> None:
    prompt = bridge.build_task(
        {"task_title": "x", "request_id": "r", "cycle_id": "c", "goal_id": "g"},
        "derived",
        "",
    )
    MUTATION_POLICY.validate_rendered_surfaces(prompt)
    assert MUTATION_POLICY.render_commit_surfaces() in prompt


def test_proposer_renderings_match_policy() -> None:
    expected = MUTATION_POLICY.render_commit_surfaces()
    assert expected in llm_proposer._PROPOSER_SYSTEM_PROMPT
    assert expected in llm_proposer._DEMAND_PROPOSER_SYSTEM_PROMPT
    MUTATION_POLICY.validate_rendered_surfaces(llm_proposer._PROPOSER_SYSTEM_PROMPT)
    MUTATION_POLICY.validate_rendered_surfaces(llm_proposer._DEMAND_PROPOSER_SYSTEM_PROMPT)


def test_gate_consults_authoritative_policy() -> None:
    assert gate._validate_mutation_surfaces(["scripts/x.py"]) == []
    assert gate._validate_mutation_surfaces(["AGENTS.md"]) == ["operator_owned_path: AGENTS.md"]
    assert bridge._validate_mutation_surfaces(["AGENTS.md"]) == ["operator_owned_path: AGENTS.md"]


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


def test_effective_commit_permission_set_is_unchanged() -> None:
    assert MUTATION_POLICY.commit_surfaces == (
        "surfaces/", "scripts/", "memory/", "lessons/", "docs/", "tests/", "skills/",
    )
    assert MUTATION_POLICY.commit_exact_paths == frozenset()
