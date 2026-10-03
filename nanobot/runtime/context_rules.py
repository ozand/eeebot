"""Single-owner static rules for the self-evolving context builder.

This stdlib-only module is consumed by ContextBuilder and the release metadata
exporter. Dynamic allocation and input reads remain in their existing owners.
"""
from __future__ import annotations

RELEASE_BLOCK_NAMES: tuple[str, ...] = (
    "IDENTITY.md",
    "SOUL.md",
    "goals.md",
    "USER.md",
    "OPERATING.md",
)
RELEASE_POOL_CHARS = 15_500
RELEASE_BLOCK_FLOORS: dict[str, int] = {"OPERATING.md": 5_000}
WORKSPACE_BLOCK_CAP = 4_000
MEMORY_BLOCK_CAP = 1_000
RUNTIME_BLOCK_CAP = 400
SCORECARD_BLOCK_CAP = 600
POSITION_BLOCK_CAP = 1_200
MAX_SYSTEM_PROMPT_CHARS = 35_000
SYSTEM_PROMPT_CAP_ENV = "NANOBOT_SYSTEM_PROMPT_MAX_CHARS"


def bootstrap_files(workspace_paths: tuple[str, ...]) -> tuple[tuple[str, str, int, bool], ...]:
    """Return ordered roots using the caller's authoritative workspace policy."""
    return tuple(
        [("release", name, RELEASE_POOL_CHARS, True) for name in RELEASE_BLOCK_NAMES]
        + [("workspace", name, WORKSPACE_BLOCK_CAP, True) for name in workspace_paths]
    )
