"""Shared toolset declarations for prompt and registration parity."""

from __future__ import annotations

EXECUTOR_TOOL_NAMES = ("read_file", "write_file", "edit_file", "list_dir", "exec")
INTERACTIVE_WEB_TOOL_NAMES = ("web_search", "web_fetch")
INTERACTIVE_TOOL_NAMES = EXECUTOR_TOOL_NAMES + INTERACTIVE_WEB_TOOL_NAMES
