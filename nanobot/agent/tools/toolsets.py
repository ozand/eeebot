"""Small closed tool capability profiles for agent subagents."""

from __future__ import annotations

from dataclasses import dataclass

EXECUTOR_TOOL_NAMES = ("read_file", "write_file", "edit_file", "list_dir", "exec", "search_memory")
INTERACTIVE_WEB_TOOL_NAMES = ("web_search", "web_fetch")
INTERACTIVE_TOOL_NAMES = EXECUTOR_TOOL_NAMES + INTERACTIVE_WEB_TOOL_NAMES
_KNOWN_TOOLS = frozenset((*EXECUTOR_TOOL_NAMES, *INTERACTIVE_WEB_TOOL_NAMES))


@dataclass(frozen=True)
class ToolProfile:
    """Closed subagent operations and existing file-path policy."""

    name: str
    operations_by_tool: tuple[tuple[str, str], ...]
    file_path_policy: str
    workspace_restricted: bool | None

    @property
    def tool_names(self) -> tuple[str, ...]:
        return tuple(name for name, _operation in self.operations_by_tool)

    @property
    def file_operations(self) -> tuple[str, ...]:
        return tuple(operation for _name, operation in self.operations_by_tool)

    def allowed_actions(self, *, web_tools_enabled: bool = False) -> str:
        names = self.tool_names
        if web_tools_enabled and self.name == "executor":
            names += INTERACTIVE_WEB_TOOL_NAMES
        tools = ", ".join(names[:-1]) + (f" and {names[-1]}" if len(names) > 1 else names[0])
        text = f"Tools: {tools}."
        if self.file_path_policy == "existing_unrestricted_absolute_paths":
            text += (
                " Read/list use existing path behavior: workspace-relative paths resolve under "
                "the workspace; absolute paths are accepted outside it."
            )
        elif self.file_path_policy == "legacy_restrict_to_workspace":
            text += " Filesystem and command scope follows the caller's existing configuration."
        if self.name == "planner":
            text += " No write, edit, command, search, web, or delegation tools."
        return text


EXECUTOR_PROFILE = ToolProfile(
    name="executor",
    operations_by_tool=(
        ("read_file", "read"), ("write_file", "write"), ("edit_file", "edit"),
        ("list_dir", "list"), ("exec", "exec"), ("search_memory", "search_memory"),
    ),
    file_path_policy="legacy_restrict_to_workspace",
    workspace_restricted=None,
)

PLANNER_PROFILE = ToolProfile(
    name="planner",
    operations_by_tool=(("read_file", "read"), ("list_dir", "list")),
    file_path_policy="existing_unrestricted_absolute_paths",
    workspace_restricted=False,
)

_PROFILES = {profile.name: profile for profile in (EXECUTOR_PROFILE, PLANNER_PROFILE)}


def resolve_tool_profile(profile: str | ToolProfile | None) -> ToolProfile:
    """Resolve only the two known immutable profiles; reject everything else."""
    if profile is None:
        return EXECUTOR_PROFILE
    if isinstance(profile, ToolProfile):
        known = _PROFILES.get(profile.name)
        if known is not profile:
            raise ValueError("unknown or modified tool profile")
        if any(name not in _KNOWN_TOOLS for name in profile.tool_names):
            raise ValueError("tool profile contains an unknown tool")
        if not profile.tool_names or len(profile.tool_names) != len(set(profile.tool_names)):
            raise ValueError("tool profile has an invalid tool set")
        if profile.name == "planner" and profile.tool_names != ("read_file", "list_dir"):
            raise ValueError("planner profile is not read/list-only")
        return known
    if type(profile) is str and profile in _PROFILES:
        return _PROFILES[profile]
    raise ValueError("unknown or invalid tool profile")
