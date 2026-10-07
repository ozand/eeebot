"""Read-only Git authority-closure comparison for context metadata exporters.

Production bootstrap provisioning is intentionally not implemented here; callers
must provide independently trusted anchor and manifest data.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class TreeEntry:
    mode: str
    object_type: str
    object_id: str


def _git_entry(repo: str, revision: str, path: str) -> TreeEntry:
    result = subprocess.run(
        ["git", "-C", repo, "ls-tree", "-z", revision, "--", path],
        check=True,
        capture_output=True,
    )
    records = result.stdout.split(b"\0")
    records = [record for record in records if record]
    if len(records) != 1:
        raise ValueError(f"expected one tree entry for {path}")
    metadata, returned_path = records[0].split(b"\t", 1)
    if returned_path.decode("utf-8", errors="strict") != path:
        raise ValueError(f"unexpected tree path for {path}")
    mode, object_type, object_id = metadata.decode("ascii").split()
    if mode not in {"100644", "100755"} or object_type != "blob":
        raise ValueError(f"unsupported authority entry for {path}")
    return TreeEntry(mode, object_type, object_id)


def assert_same_authority(
    repo: str,
    target_revision: str,
    anchor_revision: str,
    manifest: tuple[str, ...],
) -> None:
    """Reject any missing, non-regular, mode-changed, or content-changed member.

    `manifest` and `anchor_revision` are trusted inputs supplied by the caller;
    this function deliberately does not load either from the target tree.
    """
    if not manifest or len(set(manifest)) != len(manifest):
        raise ValueError("authority manifest must be non-empty and unique")
    for path in manifest:
        if path.startswith("/") or ".." in path.split("/") or "\\" in path:
            raise ValueError(f"invalid authority path: {path}")
        target = _git_entry(repo, target_revision, path)
        anchor = _git_entry(repo, anchor_revision, path)
        if target != anchor:
            raise ValueError(f"authority differs from approved anchor: {path}")
