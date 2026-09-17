"""Authoritative read-versus-commit policy for the self-evolving loop.

This module is deliberately off the instance mutation surface.  It gives the
prompt renderers, the proposer and the gate one source of truth for three
questions:

- which bootstrap files the loop reads (``read_paths``);
- where the loop may commit (``commit_path_prefixes`` + ``commit_exact_paths``);
- which release-owned files and control-plane directories it must never touch
  (``immutable_files``, ``forbidden_dirs``).

ADR-022 (#1720, #1723): the instance ``AGENTS.md`` is loop-owned again, but
only for repository layout — it is commit-permitted as an exact path and the
gate bounds every staged version to ``agents_md_max_lines`` lines with none of
the runtime-rule headings (those move to the release-owned ``OPERATING.md``).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


_READ_PATHS = ("AGENTS.md",)
_COMMIT_PATH_PREFIXES = (
    "surfaces/", "scripts/", "memory/", "lessons/", "docs/", "tests/", "skills/",
)
_COMMIT_EXACT_PATHS = frozenset({"AGENTS.md"})
# Release-owned files (ADR-022 ontology). They ship read-only in the release
# tree; the gate and the proposer reject any path whose basename matches.
_IMMUTABLE_FILES = ("goals.md", "IDENTITY.md", "SOUL.md", "USER.md", "OPERATING.md")
# Control-plane directories named in the prompt's "Do NOT modify" line. They
# are outside every commit prefix already; naming them keeps the rendered
# block and the instance AGENTS.md in agreement (#1723).
_FORBIDDEN_DIRS = ("state/", "ops/")
# Scope bound for the loop-owned AGENTS.md (#1720 decision 6, #1726 test 2).
_AGENTS_MD_MAX_LINES = 150
_AGENTS_MD_RUNTIME_HEADINGS = (
    "## Identity", "## Charter", "## Cycle contract", "## Immediate skip protocol",
    "## Staging protocol", "## Forbidden operational paths", "## Execution protocol",
    "## Cycle termination", "## Turn budget checkpoints",
)


class MutationPolicyError(ValueError):
    """Raised when a policy or its rendered representation is malformed."""


@dataclass(frozen=True)
class MutationPolicy:
    """Immutable permissions used by both prompts and the mutation gate."""

    read_paths: tuple[str, ...]
    commit_path_prefixes: tuple[str, ...]
    commit_exact_paths: frozenset[str]
    immutable_files: tuple[str, ...] = _IMMUTABLE_FILES
    forbidden_dirs: tuple[str, ...] = _FORBIDDEN_DIRS
    agents_md_max_lines: int = _AGENTS_MD_MAX_LINES
    agents_md_runtime_headings: tuple[str, ...] = _AGENTS_MD_RUNTIME_HEADINGS

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
        if any(
            prefix == "AGENTS.md" or "AGENTS.md".startswith(prefix)
            for prefix in self.commit_path_prefixes
        ):
            raise MutationPolicyError("AGENTS.md may be commit-permitted only as an exact path, never by prefix")
        if not isinstance(self.immutable_files, tuple) or not self.immutable_files or not all(
            isinstance(path, str) and path and "/" not in path for path in self.immutable_files
        ):
            raise MutationPolicyError("immutable_files must be a non-empty tuple of basenames")
        for path in self.immutable_files:
            if path in self.commit_exact_paths or any(
                path.startswith(prefix) for prefix in self.commit_path_prefixes
            ):
                raise MutationPolicyError(f"release-owned {path} must not be commit-permitted")
        if not isinstance(self.forbidden_dirs, tuple) or not all(
            isinstance(path, str) and path.endswith("/") for path in self.forbidden_dirs
        ):
            raise MutationPolicyError("forbidden_dirs must be a tuple of slash-terminated strings")
        if set(self.forbidden_dirs) & set(self.commit_path_prefixes):
            raise MutationPolicyError("a forbidden directory cannot also be a commit prefix")
        if not isinstance(self.agents_md_max_lines, int) or self.agents_md_max_lines <= 0:
            raise MutationPolicyError("agents_md_max_lines must be a positive int")

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
            f"Do NOT modify: {self.render_do_not_modify()}, secrets, or systemd units."
        )

    def render_do_not_modify(self) -> str:
        """Render the canonical list of forbidden directories and release-owned files."""
        self.validate()
        return ", ".join(self.forbidden_dirs + self.immutable_files)

    def agents_md_scope_violations(self, text: str) -> list[str]:
        """Return why a staged ``AGENTS.md`` text is out of scope, or ``[]``.

        The loop may commit ``AGENTS.md`` only as repository layout (ADR-022):
        at most ``agents_md_max_lines`` lines and none of the runtime-rule
        headings, which live in the release-owned ``OPERATING.md``.
        """
        self.validate()
        lines = text.splitlines()
        violations: list[str] = []
        if len(lines) > self.agents_md_max_lines:
            violations.append(
                f"agents_md_scope: {len(lines)} lines exceeds {self.agents_md_max_lines}"
            )
        for line in lines:
            stripped = line.strip()
            for heading in self.agents_md_runtime_headings:
                # Exact heading, or the heading with a parenthetical qualifier
                # ("## Staging protocol (target paths only)").
                if stripped == heading or stripped.startswith(heading + " ("):
                    violations.append(
                        f"agents_md_scope: runtime heading belongs to OPERATING.md: {stripped}"
                    )
                    break
        return violations

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


# AGENTS.md is both readable (bootstrap) and commit-permitted as an exact path,
# bounded by ``agents_md_scope_violations`` in the gate (ADR-022).
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
