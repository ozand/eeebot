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


_READ_PATHS = ("AGENTS.md",)
_COMMIT_PATH_PREFIXES = (
    "surfaces/", "scripts/", "memory/", "lessons/", "docs/", "tests/", "skills/",
)
_COMMIT_EXACT_PATHS = frozenset()


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
        if not isinstance(self.read_paths, tuple) or not self.read_paths or not all(
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

    def render_read_paths(self) -> str:
        """Render the canonical prompt list of readable bootstrap paths."""
        self.validate()
        return ", ".join(self.read_paths)

    def render_proposer_surface_clause(self) -> str:
        """Render the canonical proposer instruction for one target path."""
        self.validate()
        return (
            "target_path must name exactly ONE path (file or directory) under "
            f"one of these mutable surfaces: {self.render_commit_surfaces()} — no "
            "other path is acceptable."
        )

    def render_bridge_surface_block(self) -> str:
        """Render the canonical bridge mutation-surface block."""
        self.validate()
        return (
            "## Mutation surfaces\n"
            f"Allowed targets: {self.render_commit_surfaces()}\n"
            "Creating or improving skills for repeated patterns is valuable work.\n"
            "Do NOT modify: state/, goals.md, IDENTITY.md, secrets, or systemd units."
        )

    def validate_rendered_surfaces(self, rendered: str) -> None:
        """Reject a prompt rendering that does not describe this policy exactly.

        Renderers pass the list between the stable markers below.  Comparing
        the extracted value, rather than checking that the expected text is a
        substring, also rejects an accidentally more-permissive rendering.
        """
        expected = self.render_commit_surfaces()
        if not isinstance(rendered, str):
            raise MutationPolicyError("mutation-surface rendering is not text")
        if "MUTATION_SURFACES=" in rendered:
            value = rendered.split("MUTATION_SURFACES=", 1)[1].split("\n", 1)[0].strip()
            if value != expected:
                raise MutationPolicyError(
                    "rendered mutation surfaces disagree with the authoritative commit policy"
                )
            return
        if expected not in rendered:
            raise MutationPolicyError(
                "rendered mutation surfaces disagree with the authoritative commit policy"
            )


# Read reachability intentionally includes AGENTS.md; commit permission does not.
MUTATION_POLICY = MutationPolicy(
    read_paths=_READ_PATHS,
    commit_path_prefixes=_COMMIT_PATH_PREFIXES,
    commit_exact_paths=_COMMIT_EXACT_PATHS,
)
MUTATION_POLICY.validate()


def policy_mismatch_diagnostic(policy: MutationPolicy | None = None) -> str | None:
    """Return a diagnostic instead of raising for gate callers."""
    policy = MUTATION_POLICY if policy is None else policy
    try:
        policy.validate()
        policy.validate_rendered_surfaces(
            f"MUTATION_SURFACES={policy.render_commit_surfaces()}"
        )
    except Exception as exc:
        return f"mutation policy mismatch: {exc}"
    return None


def paths_in_commit_policy(paths: Iterable[str], policy: MutationPolicy | None = None) -> bool:
    """Whether every path is covered by the authoritative commit policy."""
    policy = MUTATION_POLICY if policy is None else policy
    diagnostic = policy_mismatch_diagnostic(policy)
    if diagnostic:
        raise MutationPolicyError(diagnostic)
    return all(
        path in policy.commit_exact_paths
        or any(path.startswith(prefix) for prefix in policy.commit_path_prefixes)
        for path in paths
    )
