"""Authoritative read-versus-commit policy for the self-evolving loop.

This module is deliberately off the instance mutation surface.  The loop may
read the operator-owned ``AGENTS.md`` bootstrap, but it may not commit that
file.  The commit surface remains the existing bounded set; this module only
makes the distinction explicit and gives prompt renderers and the gate one
source of truth.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


class MutationPolicyError(ValueError):
    """Raised when a policy or its rendered representation is malformed."""


@dataclass(frozen=True)
class MutationPolicy:
    """Immutable permissions used by both prompts and the mutation gate."""

    read_paths: tuple[str, ...]
    commit_path_prefixes: tuple[str, ...]
    commit_exact_paths: frozenset[str]

    def validate(self) -> None:
        """Validate structure and the read/commit separation, fail closed."""
        if not isinstance(self.read_paths, tuple) or not all(
            isinstance(path, str) and path for path in self.read_paths
        ):
            raise MutationPolicyError("read_paths must be a non-empty tuple of strings")
        if not isinstance(self.commit_path_prefixes, tuple) or not all(
            isinstance(path, str) and path.endswith("/") for path in self.commit_path_prefixes
        ):
            raise MutationPolicyError("commit_path_prefixes must be a tuple of slash-terminated strings")
        if not isinstance(self.commit_exact_paths, frozenset) or not all(
            isinstance(path, str) and path for path in self.commit_exact_paths
        ):
            raise MutationPolicyError("commit_exact_paths must be a frozenset of strings")
        if "AGENTS.md" in self.commit_exact_paths or any(
            prefix == "AGENTS.md" or "AGENTS.md".startswith(prefix)
            for prefix in self.commit_path_prefixes
        ):
            raise MutationPolicyError("operator-owned AGENTS.md must not be commit-permitted")

    @property
    def commit_surfaces(self) -> tuple[str, ...]:
        self.validate()
        return self.commit_path_prefixes + tuple(sorted(self.commit_exact_paths))

    def render_commit_surfaces(self) -> str:
        """Render the canonical prompt list of commit-permitted surfaces."""
        return ", ".join(self.commit_surfaces)

    def render_proposer_surface_clause(self) -> str:
        """Render the canonical proposer instruction for one target path."""
        self.validate()
        return (
            "target_path must name exactly ONE path (file or directory) under "
            f"one of these mutable surfaces: {self.render_commit_surfaces()} — no "
            "other path is acceptable."
        )

    def validate_rendered_surfaces(self, rendered: str) -> None:
        """Reject a prompt rendering that does not describe this policy exactly."""
        expected = self.render_commit_surfaces()
        if not isinstance(rendered, str) or expected not in rendered:
            raise MutationPolicyError(
                "rendered mutation surfaces disagree with the authoritative commit policy"
            )


# Read reachability intentionally includes AGENTS.md; commit permission does not.
MUTATION_POLICY = MutationPolicy(
    read_paths=("AGENTS.md",),
    commit_path_prefixes=(
        "surfaces/", "scripts/", "memory/", "lessons/", "docs/", "tests/", "skills/",
    ),
    commit_exact_paths=frozenset(),
)
MUTATION_POLICY.validate()


def policy_mismatch_diagnostic(policy: MutationPolicy = MUTATION_POLICY) -> str | None:
    """Return a diagnostic instead of raising for gate callers."""
    try:
        policy.validate()
        policy.validate_rendered_surfaces(policy.render_commit_surfaces())
    except Exception as exc:
        return f"mutation policy mismatch: {exc}"
    return None


def paths_in_commit_policy(paths: Iterable[str], policy: MutationPolicy = MUTATION_POLICY) -> bool:
    """Whether every path is covered by the authoritative commit policy."""
    diagnostic = policy_mismatch_diagnostic(policy)
    if diagnostic:
        raise MutationPolicyError(diagnostic)
    return all(
        path in policy.commit_exact_paths
        or any(path.startswith(prefix) for prefix in policy.commit_path_prefixes)
        for path in paths
    )
