"""Skills loader for agent capabilities."""

import json
import os
import re
import shutil
from pathlib import Path

# Default builtin skills directory (relative to this file)
BUILTIN_SKILLS_DIR = Path(__file__).parent.parent / "skills"

# #939 Part E: operator/builtin-only skills that are NEVER auto-loaded in the
# self-evolving loop subagent context.  The bridge passes these names via
# SubagentManager(excluded_skill_names=...) → ContextBuilder(excluded_skill_names=...)
# → SkillsLoader.build_skills_summary(excluded_names=...).
#
# Rule: workspace/instance skills (source="workspace") are NEVER included in
# get_always_skills() — auto-loading an instance-written skill would let the
# loop inject arbitrary content into every future subagent context.  Only
# builtin and operator-installed skills may carry always=true.
_WORKSPACE_SOURCE = "workspace"
_RELEASE_SOURCE = "release"
# ADR-033 / #1863: these are package-shipped operator instructions, distinct
# from generic builtin capabilities and never overrideable by the instance.
_RELEASE_OWNED_SKILL_NAMES = frozenset({
    "eeebot-agent-work-review", "memory-lookup", "run-tests", "task-writing",
})
# #1585: classify only the declared 30-day confirmed-read window. This is a
# safety ordering signal, not a relevance rank; unavailable input never becomes
# a valid zero-read result.
_SKILL_USAGE_WINDOW_DAYS = 30


class SkillsLoader:
    """
    Loader for agent skills.

    Skills are markdown files (SKILL.md) that teach the agent how to use
    specific tools or perform certain tasks.
    """

    def __init__(self, workspace: Path, builtin_skills_dir: Path | None = None):
        self.workspace = workspace
        self.workspace_skills = workspace / "skills"
        self.builtin_skills = builtin_skills_dir or BUILTIN_SKILLS_DIR
        self.last_catalogue_usage: dict[str, object] = {
            "status": "not_run",
            "window_days": _SKILL_USAGE_WINDOW_DAYS,
            "zero_read_names": [],
        }

    def list_skills(self, filter_unavailable: bool = True) -> list[dict[str, str]]:
        """
        List all available skills.

        Args:
            filter_unavailable: If True, filter out skills with unmet requirements.

        Returns:
            List of skill info dicts with 'name', 'path', 'source'.
        """
        skills = []

        # ADR-033 / #1863: selected operator instructions ship with the
        # release. They precede an identically named instance skill, so the
        # loop cannot replace an instruction it must obey.
        release_names: set[str] = set()
        if self.builtin_skills and self.builtin_skills.exists():
            for name in sorted(_RELEASE_OWNED_SKILL_NAMES):
                skill_file = self.builtin_skills / name / "SKILL.md"
                if skill_file.is_file():
                    skills.append({"name": name, "path": str(skill_file), "source": _RELEASE_SOURCE})
                    release_names.add(name)

        # Workspace skills remain the loop-owned surface. They retain their
        # existing precedence over generic builtins, but not over an actually
        # installed release skill. Falling back when the release file is
        # absent preserves readable skills across a failed/rolled-back deploy.
        if self.workspace_skills.exists():
            for skill_dir in sorted(self.workspace_skills.iterdir()):
                if skill_dir.is_dir():
                    skill_file = skill_dir / "SKILL.md"
                    if skill_file.exists() and skill_dir.name not in release_names:
                        skills.append({"name": skill_dir.name, "path": str(skill_file), "source": _WORKSPACE_SOURCE})

        # Generic package skills retain their historical fallback role.
        if self.builtin_skills and self.builtin_skills.exists():
            for skill_dir in sorted(self.builtin_skills.iterdir()):
                if skill_dir.is_dir():
                    skill_file = skill_dir / "SKILL.md"
                    if skill_file.exists() and not any(s["name"] == skill_dir.name for s in skills):
                        skills.append({"name": skill_dir.name, "path": str(skill_file), "source": "builtin"})

        # Filter by requirements
        if filter_unavailable:
            return [s for s in skills if self._check_requirements(self._get_skill_meta(s["name"]))]
        return skills

    def load_skill(self, name: str) -> str | None:
        """
        Load a skill by name.

        Args:
            name: Skill name (directory name).

        Returns:
            Skill content or None if not found.
        """
        # ADR-033: a release-owned operator skill cannot be shadowed by a
        # same-named instance skill.
        if name in _RELEASE_OWNED_SKILL_NAMES and self.builtin_skills:
            release_skill = self.builtin_skills / name / "SKILL.md"
            if release_skill.exists():
                return release_skill.read_text(encoding="utf-8")

        workspace_skill = self.workspace_skills / name / "SKILL.md"
        if workspace_skill.exists():
            return workspace_skill.read_text(encoding="utf-8")

        if self.builtin_skills:
            builtin_skill = self.builtin_skills / name / "SKILL.md"
            if builtin_skill.exists():
                return builtin_skill.read_text(encoding="utf-8")

        return None

    def load_skills_for_context(self, skill_names: list[str]) -> str:
        """
        Load specific skills for inclusion in agent context.

        Args:
            skill_names: List of skill names to load.

        Returns:
            Formatted skills content.
        """
        parts = []
        for name in skill_names:
            content = self.load_skill(name)
            if content:
                content = self._strip_frontmatter(content)
                parts.append(f"### Skill: {name}\n\n{content}")

        return "\n\n---\n\n".join(parts) if parts else ""

    def _retired_skill_paths(self) -> set[str] | None:
        """Return verified-absent skill paths, or None when state is unreadable.

        Filtering only on positive proof prevents an unreadable retirement
        sidecar from hiding the executor's entire toolkit: unavailable state
        means filter nothing, while an artifact still on disk remains visible.
        """
        try:
            from nanobot.runtime.state import resolve_runtime_state_root
            from nanobot.runtime.state_access import sidecar
            state_path = resolve_runtime_state_root(self.workspace) / "demand" / "skill_retirement_cooldown.json"
            loaded = sidecar(state_path, default=None, max_bytes=64 * 1024)
            if loaded.status == "absent":
                return set()
            if loaded.status != "present":
                return None
            data = loaded.data
            paths = data.get("paths") if isinstance(data, dict) else None
            if not isinstance(paths, dict):
                return None
            return {
                str(path).replace("\\", "/")
                for path, record in paths.items()
                if isinstance(record, dict) and record.get("status") == "verified_absent"
            }
        except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
            return None

    def _is_retired_skill(self, skill: dict[str, str], retired_paths: set[str] | None) -> bool:
        # ADR-033: the retirement sidecar describes instance-repository paths;
        # it must never hide a release-owned replacement with the same name.
        if skill.get("source") == _RELEASE_SOURCE or retired_paths is None:
            return False
        rel = f"skills/{skill['name']}/SKILL.md"
        return rel in retired_paths or not Path(skill["path"]).is_file()

    def _workspace_usage_order(self, skills: list[dict[str, str]]) -> tuple[list[dict[str, str]], dict[str, object]]:
        """Move only valid-window zero-read workspace skills after used ones.

        Alphabetical order is preserved within both groups. A missing or
        malformed fitness sidecar leaves the original order unchanged rather
        than treating unavailable usage as zero.
        """
        workspace = [skill for skill in skills if skill.get("source") == _WORKSPACE_SOURCE]
        others = [skill for skill in skills if skill.get("source") != _WORKSPACE_SOURCE]
        try:
            from nanobot.runtime.skill_fitness import census
            from nanobot.runtime.state import resolve_runtime_state_root
            result = census(
                resolve_runtime_state_root(self.workspace),
                self.workspace,
            )
            if result.get("ok") is not True:
                return skills, {
                    "status": "unavailable",
                    "window_days": _SKILL_USAGE_WINDOW_DAYS,
                    "skills_total": len(workspace),
                    "used_count": None,
                    "zero_read_names": [],
                }
            zero = sorted(
                str(row.get("skill") or "").strip()
                for row in result.get("zero_read", [])
                if isinstance(row, dict) and str(row.get("skill") or "").strip()
            )
            zero_set = set(zero)
            ordered = [skill for skill in workspace if skill["name"] not in zero_set]
            ordered.extend(skill for skill in workspace if skill["name"] in zero_set)
            return ordered + others, {
                "status": "complete",
                "window_days": _SKILL_USAGE_WINDOW_DAYS,
                "skills_total": len(workspace),
                "used_count": len(workspace) - len(zero_set),
                "zero_read_names": zero,
            }
        except Exception:
            return skills, {
                "status": "unavailable",
                "window_days": _SKILL_USAGE_WINDOW_DAYS,
                "skills_total": len(workspace),
                "used_count": None,
                "zero_read_names": [],
            }

    # #1732: the two-line header stated once for the compact (loop-profile)
    # catalogue, replacing a per-skill <location> that only ever restated
    # this same canonical layout rule (~200 chars of wrapper per skill,
    # 66% of a 10,109-char catalogue measured on 33 skills).
    _COMPACT_HEADER = (
        "Skills live at skills/<name>/SKILL.md unless marked release; read one "
        "with read_file when its description matches the task."
    )

    def build_skills_summary(
        self, excluded_names: "list[str] | None" = None, *, compact: bool = False,
    ) -> str:
        """
        Build a summary of all skills (name, description, path, availability).

        This is used for progressive loading - the agent can read the full
        skill content using read_file when needed.

        *excluded_names* is an optional list of skill names to omit from the
        summary entirely.  The bridge passes this for the self-evolving loop
        subagent to suppress operator-only or off-topic builtin skills
        (e.g. weather, tmux, clawhub) without affecting interactive sessions.
        Workspace/instance skills appear in the summary with
        ``source="workspace"`` so the loop knows they exist and may read them;
        they are never auto-loaded (see get_always_skills).

        *compact* (#1732): render one ``- NAME: DESC`` line per skill under a
        two-line header instead of an XML ``<skill>`` block. The loop profile
        passes ``compact=True`` (an explicit flag, not inferred from
        *excluded_names*, so a future interactive caller may pass
        *excluded_names* without switching format). Interactive sessions are
        unaffected: this parameter defaults to ``False`` and their XML output
        is unchanged.

        Returns:
            The skills summary — XML by default, or one line per skill when
            ``compact=True``.
        """
        all_skills = self.list_skills(filter_unavailable=False)
        if not all_skills:
            return ""

        usage_observation: dict[str, object] = {
            "status": "not_run",
            "window_days": _SKILL_USAGE_WINDOW_DAYS,
            "zero_read_names": [],
        }
        if excluded_names is not None:
            all_skills, usage_observation = self._workspace_usage_order(all_skills)
        self.last_catalogue_usage = usage_observation

        excluded_set = set(excluded_names or [])
        retired_paths = self._retired_skill_paths()

        def escape_xml(s: str) -> str:
            return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        if compact:
            lines = [self._COMPACT_HEADER, ""]
        else:
            lines = ["<skills>"]
        for s in all_skills:
            if s["name"] in excluded_set:
                continue
            if self._is_retired_skill(s, retired_paths):
                continue
            source = s.get("source", "builtin")
            skill_meta = self._get_skill_meta(s["name"])
            available = self._check_requirements(skill_meta)

            if compact:
                name = s["name"]
                desc = self._get_skill_description(s["name"]).replace("\n", " ").strip()
                entry = f"- {name}: {desc}"
                if source == _RELEASE_SOURCE:
                    entry += f" (release: nanobot/skills/{name}/SKILL.md)"
                elif source != _WORKSPACE_SOURCE:
                    # Not under the workspace layout rule the header states
                    # once — a generic builtin needs its real path named.
                    entry += f" (nanobot/skills/{name}/SKILL.md)"
                if not available:
                    missing = self._get_missing_requirements(skill_meta)
                    if missing:
                        entry += f" (requires {missing})"
                lines.append(entry)
                continue

            name = escape_xml(s["name"])
            path = s["path"]
            if source == _WORKSPACE_SOURCE:
                try:
                    path = str(Path(path).relative_to(self.workspace))
                except ValueError:
                    path = str(Path(path))
                path = path.replace("\\", "/")
            desc = escape_xml(self._get_skill_description(s["name"]))

            lines.append(f'  <skill available="{str(available).lower()}" source="{source}">')
            lines.append(f"    <name>{name}</name>")
            lines.append(f"    <description>{desc}</description>")
            lines.append(f"    <location>{path}</location>")

            # Show missing requirements for unavailable skills
            if not available:
                missing = self._get_missing_requirements(skill_meta)
                if missing:
                    lines.append(f"    <requires>{escape_xml(missing)}</requires>")

            lines.append("  </skill>")
        if not compact:
            lines.append("</skills>")
        self.last_catalogue_usage = usage_observation

        return "\n".join(lines)

    def _get_missing_requirements(self, skill_meta: dict) -> str:
        """Get a description of missing requirements."""
        missing = []
        requires = skill_meta.get("requires", {})
        for b in requires.get("bins", []):
            if not shutil.which(b):
                missing.append(f"CLI: {b}")
        for env in requires.get("env", []):
            if not os.environ.get(env):
                missing.append(f"ENV: {env}")
        return ", ".join(missing)

    def _get_skill_description(self, name: str) -> str:
        """Get the description of a skill from its frontmatter."""
        meta = self.get_skill_metadata(name)
        if meta and meta.get("description"):
            return meta["description"]
        return name  # Fallback to skill name

    def _strip_frontmatter(self, content: str) -> str:
        """Remove YAML frontmatter from markdown content."""
        if content.startswith("---"):
            match = re.match(r"^---\n.*?\n---\n", content, re.DOTALL)
            if match:
                return content[match.end():].strip()
        return content

    def _parse_nanobot_metadata(self, raw: str) -> dict:
        """Parse skill metadata JSON from frontmatter (supports nanobot and openclaw keys)."""
        try:
            data = json.loads(raw)
            return data.get("nanobot", data.get("openclaw", {})) if isinstance(data, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}

    def _check_requirements(self, skill_meta: dict) -> bool:
        """Check if skill requirements are met (bins, env vars)."""
        requires = skill_meta.get("requires", {})
        for b in requires.get("bins", []):
            if not shutil.which(b):
                return False
        for env in requires.get("env", []):
            if not os.environ.get(env):
                return False
        return True

    def _get_skill_meta(self, name: str) -> dict:
        """Get nanobot metadata for a skill (cached in frontmatter)."""
        meta = self.get_skill_metadata(name) or {}
        return self._parse_nanobot_metadata(meta.get("metadata", ""))

    def get_always_skills(self) -> list[str]:
        """Get skills marked as always=true that meet requirements.

        #939 Part E: workspace/instance skills (source='workspace') are NEVER
        auto-loaded regardless of their always flag — only builtin/operator
        skills may have always=true honoured.  This prevents an instance from
        injecting arbitrary content into every future subagent context by
        writing a SKILL.md with always=true.
        """
        result = []
        for s in self.list_skills(filter_unavailable=True):
            # Workspace/instance skills: never auto-load (always flag ignored).
            if s.get("source") == _WORKSPACE_SOURCE:
                continue
            meta = self.get_skill_metadata(s["name"]) or {}
            skill_meta = self._parse_nanobot_metadata(meta.get("metadata", ""))
            if skill_meta.get("always") or meta.get("always"):
                result.append(s["name"])
        return result

    def get_skill_metadata(self, name: str) -> dict | None:
        """
        Get metadata from a skill's frontmatter.

        Args:
            name: Skill name.

        Returns:
            Metadata dict or None.
        """
        content = self.load_skill(name)
        if not content:
            return None

        if content.startswith("---"):
            match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
            if match:
                # Simple YAML parsing
                metadata = {}
                for line in match.group(1).split("\n"):
                    if ":" in line:
                        key, value = line.split(":", 1)
                        metadata[key.strip()] = value.strip().strip('"\'')
                return metadata

        return None
