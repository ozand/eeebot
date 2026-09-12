"""Bounded FTS5-backed memory search tool for the loop executor (#1481)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from nanobot.agent.tools.base import Tool
from nanobot.runtime.existence_index import search_memory


class MemorySearchTool(Tool):
    """Search the verified memory corpus without embedding it in the prompt."""

    def __init__(self, workspace: Path, state_dir: Path):
        self._workspace = Path(workspace)
        self._state_dir = Path(state_dir)

    @property
    def name(self) -> str:
        return "search_memory"

    @property
    def description(self) -> str:
        return (
            "Search persistent memory with local FTS5. Returns JSON with status complete, "
            "partial, or unavailable, bounded snippets, and safe memory/*.md paths. "
            "complete with zero results is a real zero. If status is unavailable, do not "
            "treat it as no matches: when your decision depends on memory, stop and report "
            "outcome blocked with the returned reason. Use read_file on a returned path "
            "when the bounded snippet is insufficient."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 500,
                    "description": "Words describing the memory fact or prior decision to retrieve.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 8,
                    "description": "Maximum verified results to return (default 5, hard maximum 8).",
                },
            },
            "required": ["query"],
        }

    async def execute(self, query: str, limit: int = 5, **kwargs: Any) -> str:
        result = search_memory(self._state_dir, self._workspace, query, limit=limit)
        return json.dumps(result, ensure_ascii=False, separators=(",", ":"))
