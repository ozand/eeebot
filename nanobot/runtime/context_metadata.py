"""Bounded descriptor-only metadata describing the current context rules.

Imported by the release exporter and the dashboard-compatible reader. This
module deliberately avoids importing ContextBuilder or mutable runtime state.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from nanobot.runtime.context_rules import (
    MAX_SYSTEM_PROMPT_CHARS,
    MEMORY_BLOCK_CAP,
    POSITION_BLOCK_CAP,
    RELEASE_BLOCK_FLOORS,
    RELEASE_POOL_CHARS,
    RUNTIME_BLOCK_CAP,
    SCORECARD_BLOCK_CAP,
    SYSTEM_PROMPT_CAP_ENV,
    WORKSPACE_BLOCK_CAP,
    bootstrap_files,
)
from nanobot.runtime.mutation_policy import MUTATION_POLICY
from nanobot.runtime.operator_documents import PRIORITIES_BLOCK_CAP

SCHEMA_VERSION = 1
_MAX_ARTIFACT_BYTES = 8_192
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_RELEASE_IDS = {
    "IDENTITY.md": "release.identity",
    "SOUL.md": "release.soul",
    "goals.md": "release.charter",
    "USER.md": "release.user",
    "OPERATING.md": "release.operating",
}
_RULE_FIELDS = {
    "release.identity": ("release", "approved-file-if-readable", {"budget": "shared_release_pool", "floor": 0}),
    "release.soul": ("release", "approved-file-if-readable", {"budget": "shared_release_pool", "floor": 0}),
    "release.charter": ("release", "approved-file-if-readable", {"budget": "shared_release_pool", "floor": 0}),
    "release.user": ("release", "approved-file-if-readable", {"budget": "shared_release_pool", "floor": 0}),
    "release.operating": ("release", "approved-file-if-readable", {"budget": "shared_release_pool", "floor": RELEASE_BLOCK_FLOORS["OPERATING.md"]}),
    "workspace.agents": ("workspace", "required-file", {"cap": WORKSPACE_BLOCK_CAP}),
    "generated.priorities": ("generated", "when-resolved", {"cap": PRIORITIES_BLOCK_CAP}),
    "generated.memory": ("generated", "when-nonempty", {"cap": MEMORY_BLOCK_CAP}),
    "generated.runtime": ("generated", "always", {"cap": RUNTIME_BLOCK_CAP}),
    "generated.scorecard": ("generated", "when-state-readable", {"cap": SCORECARD_BLOCK_CAP}),
    "generated.position": ("generated", "always", {"cap": POSITION_BLOCK_CAP}),
}


def _rule(rule_id: str, order: int, owner: str, inclusion: str, values: dict[str, Any]) -> dict[str, Any]:
    return {"id": rule_id, "order": order, "owner": owner, "inclusion": inclusion, "values": values}


def build_context_metadata(source_commit: str) -> dict[str, Any]:
    """Return deterministic rule descriptors; never reads source/prompt content."""
    if not isinstance(source_commit, str) or not _SHA_RE.fullmatch(source_commit):
        raise ValueError("source_commit must be a full lowercase 40-character commit SHA")
    rules: list[dict[str, Any]] = []
    for order, (root_kind, filename, _cap, _required) in enumerate(bootstrap_files(MUTATION_POLICY.read_paths)):
        if root_kind == "release":
            rule_id = _RELEASE_IDS.get(filename)
            if rule_id is None:
                raise ValueError("release rule has no approved descriptor")
        elif root_kind == "workspace" and filename == "AGENTS.md":
            rule_id = "workspace.agents"
        else:
            raise ValueError("input path has no approved descriptor")
        owner, inclusion, values = _RULE_FIELDS[rule_id]
        rules.append(_rule(rule_id, order, owner, inclusion, dict(values)))

    order = len(rules)
    for offset, rule_id in enumerate((
        "generated.priorities", "generated.memory", "generated.runtime",
        "generated.scorecard", "generated.position",
    )):
        owner, inclusion, values = _RULE_FIELDS[rule_id]
        rules.append(_rule(rule_id, order + offset, owner, inclusion, dict(values)))

    return {
        "schema_version": SCHEMA_VERSION,
        "source_commit": source_commit,
        "prompt_cap": {
            "default": MAX_SYSTEM_PROMPT_CHARS,
            "override_env_name": SYSTEM_PROMPT_CAP_ENV,
            "effective": None,
        },
        "release_pool": {
            "cap": RELEASE_POOL_CHARS,
            "floors": {"release.operating": RELEASE_BLOCK_FLOORS["OPERATING.md"]},
        },
        "rules": rules,
    }


def validate_context_metadata(value: Any, expected_commit: str) -> dict[str, Any]:
    """Validate bounded typed descriptors and expected release identity."""
    if not isinstance(value, dict) or set(value) != {"schema_version", "source_commit", "prompt_cap", "release_pool", "rules"}:
        raise ValueError("metadata top-level shape is invalid")
    if type(value["schema_version"]) is not int or value["schema_version"] != SCHEMA_VERSION:
        raise ValueError("metadata schema version is unsupported")
    if not isinstance(expected_commit, str) or not _SHA_RE.fullmatch(expected_commit):
        raise ValueError("expected source commit is invalid")
    if value["source_commit"] != expected_commit:
        raise ValueError("metadata source commit does not match release")
    if len(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")) > _MAX_ARTIFACT_BYTES:
        raise ValueError("metadata exceeds size limit")

    prompt_cap = value["prompt_cap"]
    if (not isinstance(prompt_cap, dict) or set(prompt_cap) != {"default", "override_env_name", "effective"}
            or type(prompt_cap["default"]) is not int or prompt_cap["default"] != MAX_SYSTEM_PROMPT_CHARS
            or prompt_cap["override_env_name"] != SYSTEM_PROMPT_CAP_ENV or prompt_cap["effective"] is not None):
        raise ValueError("prompt cap metadata is invalid")
    pool = value["release_pool"]
    if (not isinstance(pool, dict) or set(pool) != {"cap", "floors"} or type(pool["cap"]) is not int
            or pool["cap"] != RELEASE_POOL_CHARS
            or pool["floors"] != {"release.operating": RELEASE_BLOCK_FLOORS["OPERATING.md"]}):
        raise ValueError("release pool metadata is invalid")

    rules = value["rules"]
    if not isinstance(rules, list) or len(rules) != len(_RULE_FIELDS):
        raise ValueError("metadata rules must contain every approved descriptor")
    seen: set[str] = set()
    for rule in rules:
        if not isinstance(rule, dict) or set(rule) != {"id", "order", "owner", "inclusion", "values"}:
            raise ValueError("rule descriptor shape is invalid")
        rule_id = rule["id"]
        if rule_id not in _RULE_FIELDS or rule_id in seen:
            raise ValueError("rule identifier is not allowlisted or is duplicated")
        seen.add(rule_id)
        if type(rule["order"]) is not int or not 0 <= rule["order"] < len(_RULE_FIELDS):
            raise ValueError("rule order is invalid")
        owner, inclusion, values = _RULE_FIELDS[rule_id]
        if rule["owner"] != owner or rule["inclusion"] != inclusion or rule["values"] != values:
            raise ValueError("rule descriptor does not match canonical definitions")
    if seen != set(_RULE_FIELDS):
        raise ValueError("metadata is missing an approved rule descriptor")
    return value


def load_context_metadata(path: str | Path, expected_commit: str) -> dict[str, Any]:
    artifact = Path(path).read_bytes()
    if len(artifact) > _MAX_ARTIFACT_BYTES:
        raise ValueError("metadata exceeds size limit")
    try:
        value = json.loads(artifact)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("metadata is not valid UTF-8 JSON") from exc
    return validate_context_metadata(value, expected_commit)
