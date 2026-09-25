"""Rule C service-path definition shared by runtime outcome and package measurement."""

from __future__ import annotations

SERVICE_PATH_PREFIXES = ("diary/",)
SERVICE_PATH_EXACT = frozenset({
    "memory/MEMORY.md",
    "memory/HISTORY.md",
    "memory/confirmation_status.json",
    "memory/prevent_repeats.json",
    "memory/repeat_failures.json",
})


def is_service_path(path: str) -> bool:
    normalized = str(path).replace("\\", "/")
    return normalized in SERVICE_PATH_EXACT or normalized.startswith(SERVICE_PATH_PREFIXES)


def is_service_only(files: list[str] | None) -> bool:
    """Whether a non-empty changed-file list contains only service paths."""
    return bool(files) and all(is_service_path(path) for path in files)


def is_delivered(integrated: bool, files: list[str] | None) -> bool:
    """Rule-C delivery signal; git integration and delivered work are distinct."""
    return bool(integrated and not is_service_only(files))
