"""Drift-guard: the host config template must validate against the real Config schema.

`host/eeepc/etc/nanobot-config.template.json` hand-mirrors the structure defined
by `nanobot/config/schema.py`. Nothing enforced the two stay in sync, so the
template could silently drift from the schema it's meant to model. These tests
load the template through the exact same code path `nanobot.config.loader`
uses for a real config file (`Config.model_validate`), then fail on unknown
fields, missing required fields, or load-bearing values not surviving the
round-trip.
"""

import json
from pathlib import Path
from typing import Any

from nanobot.config.schema import Config

TEMPLATE_PATH = (
    Path(__file__).resolve().parents[1]
    / "host"
    / "eeepc"
    / "etc"
    / "nanobot-config.template.json"
)


def _load_template_data() -> dict[str, Any]:
    with open(TEMPLATE_PATH, encoding="utf-8") as f:
        return json.load(f)


def _find_dropped_keys(original: Any, dumped: Any, path: str = "") -> list[str]:
    """Recursively collect keys present in `original` but absent from `dumped`.

    `Config`'s default pydantic extra policy is "ignore": unknown keys parse
    without error but vanish silently on `model_dump` -- convenient for a
    schema that wants to stay lenient toward user config files, but it means
    drift between the template and the schema would never surface on its
    own. A model with `extra="allow"` (e.g. `ChannelsConfig`, which stores
    per-channel plugin config as extra fields by design) round-trips its
    extra keys losslessly, so this diff only flags fields the schema
    actually discards -- i.e. real unknown/dead fields -- without requiring
    the production schema to switch to `extra="forbid"`.
    """
    dropped: list[str] = []
    if isinstance(original, dict):
        if not isinstance(dumped, dict):
            return [path or "<root>"]
        for key, value in original.items():
            sub_path = f"{path}.{key}" if path else key
            if key not in dumped:
                dropped.append(sub_path)
                continue
            dropped.extend(_find_dropped_keys(value, dumped[key], sub_path))
    elif isinstance(original, list):
        if not isinstance(dumped, list):
            return [path]
        for i, item in enumerate(original):
            if i < len(dumped):
                dropped.extend(_find_dropped_keys(item, dumped[i], f"{path}[{i}]"))
    return dropped


def _required_schema_paths(
    node: dict[str, Any], defs: dict[str, Any], path: str = ""
) -> list[str]:
    """Recursively resolve every field the JSON schema marks required, as dotted paths."""
    paths: list[str] = []
    for req in node.get("required", []):
        paths.append(f"{path}.{req}" if path else req)
    for prop_name, prop_schema in node.get("properties", {}).items():
        ref = prop_schema.get("$ref")
        if ref is None and "allOf" in prop_schema:
            ref = prop_schema["allOf"][0].get("$ref")
        if ref:
            def_name = ref.rsplit("/", 1)[-1]
            sub_node = defs.get(def_name, {})
            sub_path = f"{path}.{prop_name}" if path else prop_name
            paths.extend(_required_schema_paths(sub_node, defs, sub_path))
    return paths


def _get_nested(data: dict[str, Any], dotted_path: str) -> tuple[bool, Any]:
    """Return (present, value) for a dotted path into a nested dict."""
    current: Any = data
    for part in dotted_path.split("."):
        if not isinstance(current, dict) or part not in current:
            return False, None
        current = current[part]
    return True, current


def test_template_loads_through_real_config_schema() -> None:
    """The template must parse cleanly through the exact call `load_config` uses."""
    data = _load_template_data()
    config = Config.model_validate(data)  # same call as nanobot.config.loader.load_config
    assert isinstance(config, Config)


def test_template_has_no_unknown_or_dropped_fields() -> None:
    """Fail if the template carries fields the schema doesn't know about (drift)."""
    data = _load_template_data()
    config = Config.model_validate(data)
    dumped = config.model_dump(mode="json", by_alias=True)

    dropped = _find_dropped_keys(data, dumped)
    assert not dropped, (
        "Template fields not recognized by nanobot/config/schema.py (silently "
        f"dropped by pydantic's default extra='ignore' policy): {dropped}"
    )


def test_template_declares_all_schema_required_fields() -> None:
    """Fail if the template is missing any field the schema marks required."""
    schema = Config.model_json_schema()
    defs = schema.get("$defs", {})
    data = _load_template_data()

    required_paths = _required_schema_paths(schema, defs)
    missing = [p for p in required_paths if not _get_nested(data, p)[0]]
    assert not missing, f"Template is missing schema-required fields: {missing}"


def test_template_round_trips_load_bearing_values() -> None:
    """Load-bearing provider/model fields must survive the real load path unchanged."""
    data = _load_template_data()
    config = Config.model_validate(data)

    assert config.agents.defaults.model == data["agents"]["defaults"]["model"]
    assert config.agents.defaults.provider == data["agents"]["defaults"]["provider"]
    assert (
        config.providers.custom.api_base
        == data["providers"]["custom"]["apiBase"]
    )
    assert (
        config.tools.mcp_servers["openspace"].url
        == data["tools"]["mcpServers"]["openspace"]["url"]
    )
