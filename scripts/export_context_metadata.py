#!/usr/bin/env python3
"""Export or validate bounded context metadata for one exact source commit."""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_SHA = re.compile(r"^[0-9a-f]{40}$")


def _metadata_api():
    # Import package modules without executing nanobot/runtime __init__ files:
    # these package initializers pull in operational modules not shipped in the
    # bounded release archive. Module specs retain normal package resolution.
    import importlib.util

    def load_module(name: str, path: Path, package: bool = False):
        spec = importlib.util.spec_from_file_location(
            name, path, submodule_search_locations=[str(path.parent)] if package else None,
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load metadata module: {name}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module

    # Empty package shells preserve qualified module imports while avoiding
    # the operational package initializers and their unrelated dependencies.
    import types

    for name, path in (("nanobot", ROOT / "nanobot"), ("nanobot.runtime", ROOT / "nanobot" / "runtime")):
        package = types.ModuleType(name)
        package.__path__ = [str(path)]
        package.__package__ = name
        sys.modules[name] = package
    for name in ("context_rules", "mutation_policy", "operator_documents", "context_metadata"):
        load_module(f"nanobot.runtime.{name}", ROOT / "nanobot" / "runtime" / f"{name}.py")
    module = sys.modules["nanobot.runtime.context_metadata"]
    return module.build_context_metadata, module.load_context_metadata, module.validate_context_metadata


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--validate", action="store_true")
    args = parser.parse_args()
    if not _SHA.fullmatch(args.source_commit):
        parser.error("--source-commit must be a full lowercase 40-character SHA")
    build, load, validate = _metadata_api()
    if args.validate:
        load(args.output, args.source_commit)
    else:
        metadata = build(args.source_commit)
        validate(metadata, args.source_commit)
        args.output.write_text(
            json.dumps(metadata, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
