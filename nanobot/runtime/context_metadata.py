"""Bounded descriptor-only metadata describing current context rules."""
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
    bootstrap_files,
    loop_context_section_order,
)
from nanobot.runtime.mutation_policy import MUTATION_POLICY
from nanobot.runtime.operator_documents import PRIORITIES_BLOCK_CAP

SCHEMA_VERSION = 1
MAX_METADATA_BYTES = 8192
_SHA = re.compile(r"^[0-9a-f]{40}$")
_RELEASE_RULE_IDS = {
    "IDENTITY.md": "release.identity",
    "SOUL.md": "release.soul",
    "goals.md": "release.charter",
    "USER.md": "release.user",
    "OPERATING.md": "release.operating",
}
# Descriptor sequence mirrors the canonical loop section-order tuple. The
# empty skills_catalogue slot is omitted: it is telemetry schema, not content.
_GENERATED_RULES = {
    "generated.priorities": ("generated", "when-resolved", {"cap": PRIORITIES_BLOCK_CAP}),
    "generated.memory": ("generated", "when-nonempty", {"cap": MEMORY_BLOCK_CAP}),
    "generated.runtime": ("generated", "always", {"cap": RUNTIME_BLOCK_CAP}),
    "generated.scorecard": ("generated", "when-state-readable", {"cap": SCORECARD_BLOCK_CAP}),
    "generated.position": ("generated", "always", {"cap": POSITION_BLOCK_CAP}),
}


def _descriptor(rule_id: str, order: int, owner: str, inclusion: str, values: dict[str, Any]) -> dict[str, Any]:
    return {"id": rule_id, "order": order, "owner": owner, "inclusion": inclusion, "values": values}


def _ordered_rules() -> list[dict[str, Any]]:
    rules: list[dict[str, Any]] = []
    rule_by_section: dict[str, dict[str, Any]] = {}
    for root, filename, _cap, _required in bootstrap_files(MUTATION_POLICY.read_paths):
        if root != "release":
            continue
        rule_id = _RELEASE_RULE_IDS.get(filename)
        if rule_id is None:
            raise ValueError("release path has no approved descriptor")
        rule_by_section[Path(filename).stem.lower()] = _descriptor(
            rule_id, 0, "release", "approved-file-if-readable",
            {"budget": "shared_release_pool", "floor": RELEASE_BLOCK_FLOORS.get(filename, 0)},
        )
    for root, filename, cap, _required in bootstrap_files(MUTATION_POLICY.read_paths):
        if root == "workspace":
            if filename != "AGENTS.md":
                raise ValueError("workspace path has no approved descriptor")
            rule_by_section[Path(filename).stem.lower()] = _descriptor(
                "workspace.agents", 0, "workspace", "required-file", {"cap": cap},
            )
    for rule_id, (owner, inclusion, values) in _GENERATED_RULES.items():
        section = rule_id.split(".", 1)[1]
        rule_by_section[section] = _descriptor(rule_id, 0, owner, inclusion, dict(values))
    order = loop_context_section_order(MUTATION_POLICY.read_paths)
    rules = [rule_by_section[name] for name in order if name in rule_by_section]
    for index, rule in enumerate(rules):
        rule["order"] = index
    return rules


def build_context_metadata(source_commit: str) -> dict[str, Any]:
    """Build safe metadata from canonical rules; never reads prompt/source contents."""
    if not isinstance(source_commit, str) or not _SHA.fullmatch(source_commit):
        raise ValueError("source_commit must be a full lowercase 40-character commit SHA")
    return {
        "schema_version": SCHEMA_VERSION,
        "source_commit": source_commit,
        "prompt_cap": {"default": MAX_SYSTEM_PROMPT_CHARS, "override_env_name": SYSTEM_PROMPT_CAP_ENV,
                       "effective": None},
        "release_pool": {"cap": RELEASE_POOL_CHARS,
                         "floors": {"release.operating": RELEASE_BLOCK_FLOORS["OPERATING.md"]}},
        "rules": _ordered_rules(),
    }


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is forbidden: {value}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def validate_context_metadata(value: Any, expected_commit: str) -> dict[str, Any]:
    """Fail closed on schema, shape, types, bounds, allowlist, or SHA mismatch."""
    if not isinstance(value, dict) or set(value) != {"schema_version", "source_commit", "prompt_cap", "release_pool", "rules"}:
        raise ValueError("metadata top-level shape is invalid")
    if type(value["schema_version"]) is not int or value["schema_version"] != SCHEMA_VERSION:
        raise ValueError("metadata schema version is unsupported")
    if not isinstance(expected_commit, str) or not _SHA.fullmatch(expected_commit):
        raise ValueError("expected source commit is invalid")
    if type(value["source_commit"]) is not str or value["source_commit"] != expected_commit:
        raise ValueError("metadata source commit does not match release")
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(encoded) > MAX_METADATA_BYTES:
        raise ValueError("metadata exceeds size limit")

    prompt = value["prompt_cap"]
    if (not isinstance(prompt, dict) or set(prompt) != {"default", "override_env_name", "effective"}
            or type(prompt["default"]) is not int or prompt["default"] != MAX_SYSTEM_PROMPT_CHARS
            or type(prompt["override_env_name"]) is not str or prompt["override_env_name"] != SYSTEM_PROMPT_CAP_ENV
            or prompt["effective"] is not None):
        raise ValueError("prompt cap metadata is invalid")
    pool = value["release_pool"]
    expected_floors = {"release.operating": RELEASE_BLOCK_FLOORS["OPERATING.md"]}
    floors = pool.get("floors") if isinstance(pool, dict) else None
    if (not isinstance(pool, dict) or set(pool) != {"cap", "floors"} or type(pool["cap"]) is not int
            or pool["cap"] != RELEASE_POOL_CHARS or not isinstance(floors, dict)
            or set(floors) != set(expected_floors)
            or any(type(v) is not int or v != expected_floors[k] for k, v in floors.items())):
        raise ValueError("release pool metadata is invalid")

    rules = value["rules"]
    expected_rules = _ordered_rules()
    if not isinstance(rules, list) or len(rules) != len(expected_rules):
        raise ValueError("metadata rules are incomplete")
    for rule, expected in zip(rules, expected_rules, strict=True):
        if not isinstance(rule, dict) or set(rule) != {"id", "order", "owner", "inclusion", "values"}:
            raise ValueError("rule descriptor shape is invalid")
        for field in ("id", "owner", "inclusion"):
            if type(rule[field]) is not str or rule[field] != expected[field]:
                raise ValueError(f"rule {field} is invalid")
        if type(rule["order"]) is not int or rule["order"] != expected["order"]:
            raise ValueError("rule order is invalid")
        values = rule["values"]
        expected_values = expected["values"]
        if not isinstance(values, dict) or set(values) != set(expected_values):
            raise ValueError("rule values are invalid")
        for key, item in values.items():
            expected_item = expected_values[key]
            if type(item) is not type(expected_item) or item != expected_item:
                raise ValueError("rule value type or value is invalid")
    return value


def load_context_metadata(path: str | Path, expected_commit: str) -> dict[str, Any]:
    with Path(path).open("rb") as stream:
        raw = stream.read(MAX_METADATA_BYTES + 1)
    if len(raw) > MAX_METADATA_BYTES:
        raise ValueError("metadata exceeds size limit")
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_reject_json_constant)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("metadata is not valid UTF-8 JSON") from exc
    return validate_context_metadata(value, expected_commit)
