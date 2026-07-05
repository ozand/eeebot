"""Lessons database — unified read/write module for errors.yaml and lessons.yaml.

Schema is intentionally compatible with a sibling project's lessons format so
that cards can be migrated between projects without conversion.

Mandatory fields shared across projects:
  errors  : id, date, category, title, description, root_cause, impact,
             fix_applied, prevention
  lessons : id, date, category, title, description, impact, approach,
             reusable_insight

Additional fields used only by automated (source="auto") entries:
  occurrences, first_seen, last_seen, source, related, files_changed,
  sample_cycle_id

Agents must NEVER read the full YAML files.
Use find_similar() or query_for_task() which do targeted grep-equivalent
in-process search on the already-loaded (small, deduplicated) lists.
"""
from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import yaml  # type: ignore
    _YAML_OK = True
except ImportError:
    _YAML_OK = False

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_STATE_DIRS = ("state/reports/", "state/goals/", "state/subagents/",
               "state/experiments/", "state/approvals/", ".nanobot/subagents/")

_SOURCE_EXTS = {".py", ".sh", ".yaml", ".yml", ".toml", ".md", ".json", ".ts",
                ".js", ".txt", ".cfg", ".ini", ".env"}

_SKIP_FAILURE_CLASSES = {"approval_gate:expired", "approval:expired",
                          "expired", "approval_gate_expired"}

_SKIP_TASKS = frozenset({"record-reward", "inspect-pass-streak", "refresh-approval-gate",
                          "verify-approval-gate", "run-bounded-turn"})


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _is_source_file(path_str: str) -> bool:
    """Return True if path looks like a real source/config file, not a state artifact."""
    for state_dir in _STATE_DIRS:
        if state_dir in path_str:
            return False
    return Path(path_str).suffix.lower() in _SOURCE_EXTS


def _filter_source_files(paths: list[str]) -> list[str]:
    return [p for p in paths if _is_source_file(p)]


def _safe_load_yaml(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return []
    if _YAML_OK:
        data = yaml.safe_load(text)
        return data if isinstance(data, list) else []
    # Fallback without pyyaml: try JSON first (written by _dump_yaml fallback)
    try:
        data = json.loads(text)
        return data if isinstance(data, list) else []
    except Exception:
        pass
    # Last resort: id-only grep-style parse (loses lists/multiline)
    return _parse_yaml_ids(text)


def _parse_yaml_ids(text: str) -> list[dict[str, Any]]:
    """Extract id+category+title from raw YAML text without a full parser."""
    entries: list[dict[str, Any]] = []
    current: dict[str, Any] = {}
    for line in text.splitlines():
        if line.startswith("- id:"):
            if current:
                entries.append(current)
            current = {"id": line[5:].strip().strip("'\"")}
        elif line.startswith("  category:") and current:
            current["category"] = line[11:].strip().strip("'\"")
        elif line.startswith("  title:") and current:
            current["title"] = line[8:].strip().strip("'\"")
        elif line.startswith("  occurrences:") and current:
            try:
                current["occurrences"] = int(line[14:].strip())
            except ValueError:
                pass
        elif line.startswith("  first_seen:") and current:
            current["first_seen"] = line[13:].strip().strip("'\"")
        elif line.startswith("  last_seen:") and current:
            current["last_seen"] = line[12:].strip().strip("'\"")
    if current:
        entries.append(current)
    return entries


def _dump_yaml(data: list[dict[str, Any]]) -> str:
    if _YAML_OK:
        return yaml.dump(
            data,
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=False,
            width=120,
        )
    # Fallback without pyyaml: use JSON (fully round-trippable)
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class LessonsDB:
    """Thin wrapper around lessons/ directory for errors.yaml and lessons.yaml."""

    def __init__(self, workspace: Path) -> None:
        self.lessons_dir = workspace / "lessons"
        self.errors_path = self.lessons_dir / "errors.yaml"
        self.lessons_path = self.lessons_dir / "lessons.yaml"
        self.errors_cards_dir = self.lessons_dir / "errors"
        self.lessons_cards_dir = self.lessons_dir / "lessons"

    # ------------------------------------------------------------------
    # Read helpers
    # ------------------------------------------------------------------

    def load_errors(self) -> list[dict[str, Any]]:
        return _safe_load_yaml(self.errors_path)

    def load_lessons(self) -> list[dict[str, Any]]:
        return _safe_load_yaml(self.lessons_path)

    def find_similar_error(self, category: str, task_id: str) -> dict[str, Any] | None:
        """Return first error entry matching category or task_id (O(n) scan, n is tiny)."""
        key = f"{category}:{task_id}"
        for entry in self.load_errors():
            eid = str(entry.get("id", ""))
            if key in eid or category in eid:
                return entry
            if entry.get("category") == category:
                return entry
        return None

    def find_similar_lesson(self, task_id: str) -> dict[str, Any] | None:
        """Return first lesson entry matching task_id."""
        for entry in self.load_lessons():
            eid = str(entry.get("id", ""))
            if task_id in eid:
                return entry
        return None

    def query_for_task(self, task_id: str) -> dict[str, Any]:
        """Return relevant error + lesson cards for a given task_id.

        Designed to be included in a subagent prompt as lightweight context.
        Returns at most one of each to stay token-efficient.
        """
        result: dict[str, Any] = {}
        err = self.find_similar_error(category=task_id, task_id=task_id)
        if err:
            result["relevant_error"] = {
                "id": err.get("id"),
                "title": err.get("title"),
                "root_cause": err.get("root_cause", "")[:400],
                "prevention": err.get("prevention", "")[:400],
            }
        less = self.find_similar_lesson(task_id=task_id)
        if less:
            result["relevant_lesson"] = {
                "id": less.get("id"),
                "title": less.get("title"),
                "approach": less.get("approach", "")[:400],
                "reusable_insight": less.get("reusable_insight", "")[:400],
            }
        return result

    # ------------------------------------------------------------------
    # Write helpers
    # ------------------------------------------------------------------

    def _ensure_dirs(self) -> None:
        self.lessons_dir.mkdir(parents=True, exist_ok=True)
        self.errors_cards_dir.mkdir(parents=True, exist_ok=True)
        self.lessons_cards_dir.mkdir(parents=True, exist_ok=True)

    def _write_card(self, cards_dir: Path, entry_id: str, entry: dict[str, Any]) -> None:
        """Write individual markdown card for an entry."""
        safe_id = re.sub(r"[^A-Za-z0-9_\-]", "-", entry_id)
        card_path = cards_dir / f"{safe_id}.md"
        lines = [f"# {entry.get('title', entry_id)}", ""]
        for field in ("id", "date", "category", "occurrences", "first_seen", "last_seen"):
            if field in entry:
                lines.append(f"**{field}**: {entry[field]}")
        lines.append("")
        for field in ("description", "root_cause", "impact", "fix_applied",
                      "prevention", "approach", "reusable_insight"):
            if entry.get(field):
                lines.append(f"## {field.replace('_', ' ').title()}")
                lines.append(str(entry[field]))
                lines.append("")
        if entry.get("files_changed"):
            lines.append("## Files Changed")
            for f in entry["files_changed"]:
                lines.append(f"- `{f}`")
            lines.append("")
        card_path.write_text("\n".join(lines), encoding="utf-8")

    def record_error(
        self,
        *,
        category: str,
        title: str,
        description: str,
        root_cause: str,
        impact: str,
        fix_applied: str,
        prevention: str,
        task_id: str,
        cycle_id: str,
        date: str | None = None,
        source: str = "auto",
    ) -> str:
        """Add or update an error entry. Returns the entry id."""
        self._ensure_dirs()
        today = date or _today()
        entries = self.load_errors()

        # Find existing automated entry by key
        err_key_pattern = f"ERR-AUTO-{re.sub(r'[^A-Za-z0-9]', '-', category)}"
        existing_idx: int | None = None
        for i, entry in enumerate(entries):
            eid = str(entry.get("id", ""))
            if entry.get("source", "auto") != "manual" and (
                err_key_pattern in eid or entry.get("category") == category
            ):
                existing_idx = i
                break

        if existing_idx is not None:
            entries[existing_idx]["occurrences"] = int(entries[existing_idx].get("occurrences", 1)) + 1
            entries[existing_idx]["last_seen"] = today
            entry_id = str(entries[existing_idx]["id"])
        else:
            entry_id = f"{err_key_pattern}-{task_id}"[:80]
            new_entry: dict[str, Any] = {
                "id": entry_id,
                "date": today,
                "category": category,
                "title": title,
                "description": description,
                "root_cause": root_cause,
                "impact": impact,
                "fix_applied": fix_applied,
                "prevention": prevention,
                "occurrences": 1,
                "first_seen": today,
                "last_seen": today,
                "source": source,
                "sample_cycle_id": cycle_id,
            }
            # Prepend so latest entries appear first (grep-efficient)
            entries.insert(0, new_entry)

        self.errors_path.write_text(_dump_yaml(entries), encoding="utf-8")
        self._write_card(self.errors_cards_dir, entry_id, entries[0] if existing_idx is None else entries[existing_idx])
        return entry_id

    def record_lesson(
        self,
        *,
        task_id: str,
        title: str,
        description: str,
        impact: str,
        approach: str,
        reusable_insight: str,
        files_changed: list[str],
        cycle_id: str,
        date: str | None = None,
        source: str = "auto",
    ) -> str:
        """Add or update a lesson entry. Returns the entry id."""
        self._ensure_dirs()
        today = date or _today()
        entries = self.load_lessons()
        entry_id = f"LESS-AUTO-{re.sub(r'[^A-Za-z0-9]', '-', task_id)}"[:80]

        existing_idx: int | None = None
        for i, entry in enumerate(entries):
            if entry.get("id") == entry_id:
                existing_idx = i
                break

        clean_files = _filter_source_files(files_changed)

        if existing_idx is not None:
            entries[existing_idx]["occurrences"] = int(entries[existing_idx].get("occurrences", 1)) + 1
            entries[existing_idx]["last_seen"] = today
            # Accumulate unique source files only
            existing_files = set(entries[existing_idx].get("files_changed") or [])
            existing_files.update(clean_files)
            entries[existing_idx]["files_changed"] = sorted(existing_files)
        else:
            new_entry: dict[str, Any] = {
                "id": entry_id,
                "date": today,
                "category": "successful-improvement",
                "title": title,
                "description": description,
                "impact": impact,
                "approach": approach,
                "reusable_insight": reusable_insight,
                "occurrences": 1,
                "first_seen": today,
                "last_seen": today,
                "source": source,
                "sample_cycle_id": cycle_id,
                "files_changed": clean_files,
            }
            entries.insert(0, new_entry)

        self.lessons_path.write_text(_dump_yaml(entries), encoding="utf-8")
        target_entry = entries[0] if existing_idx is None else entries[existing_idx]
        self._write_card(self.lessons_cards_dir, entry_id, target_entry)
        return entry_id


# ---------------------------------------------------------------------------
# Coordinator integration hook
# ---------------------------------------------------------------------------

def update_lessons_from_cycle(
    *,
    workspace: Path,
    result_status: str,
    current_task_id: str | None,
    summary: str,
    artifact_paths: list[str],
    reward_signal: dict[str, Any],
    feedback_decision: dict[str, Any] | None,
    cycle_id: str,
    recorded_at: str,
    commits_pushed: int = 0,
) -> dict[str, Any]:
    """Called by coordinator at end of each cycle to update lessons/errors databases.

    Returns a dict with keys 'action', 'entry_id' (or None) for logging.
    This function is intentionally non-raising — any failure is swallowed and
    returned as action='error' so it never disrupts the coordinator cycle.
    """
    if not current_task_id:
        return {"action": "skipped", "reason": "no current_task_id"}

   # Skip system-internal coordinator tasks that carry no learnable signal
    if current_task_id in _SKIP_TASKS:
        return {"action": "skipped", "reason": "internal-task"}

    date = recorded_at[:10] if recorded_at else _today()

    try:
        db = LessonsDB(workspace)

        if result_status in ("BLOCK", "ERROR"):
            failure_class = (
                (feedback_decision or {}).get("repeat_block_failure_class")
                or result_status.lower()
            )
            # Skip approval-gate noise
            if any(skip in str(failure_class) for skip in _SKIP_FAILURE_CLASSES):
                return {"action": "skipped", "reason": "approval-gate-noise"}

            reason = summary or "No reason provided"
            entry_id = db.record_error(
                category=str(failure_class),
                title=f"Block on task '{current_task_id}' ({failure_class})",
                description=f"Self-evolving cycle {result_status} at task '{current_task_id}'. {reason}",
                root_cause=f"System encountered a block classified as '{failure_class}'. {reason}",
                impact="Self-evolving loop halted or required manual intervention.",
                fix_applied="Investigate coordinator logs and subagent results.",
                prevention="Inspect coordinator status rules to avoid repeating this state pattern.",
                task_id=current_task_id,
                cycle_id=cycle_id,
                date=date,
            )
            return {"action": "recorded_error", "entry_id": entry_id}

        elif result_status == "PASS":
            reward_val = float((reward_signal or {}).get("value", 0.0))
            clean_files = _filter_source_files(artifact_paths)
            fd_mode = (feedback_decision or {}).get("mode") or ""

            # Subagent ran but produced no commits — record diagnostic error, not a lesson
            if commits_pushed == 0 and current_task_id == "subagent-verify-materialized-improvement":
                entry_id = db.record_error(
                    category="subagent_no_commit",
                    title=f"Subagent completed without commit (task: {current_task_id})",
                    description=(
                        f"Cycle {cycle_id}: subagent bridge ran but produced 0 commits. "
                        f"reward={reward_val}, fd.mode={fd_mode}. "
                        f"Possible causes: task already done, or instructions unclear."
                    ),
                    root_cause="subagent completed without producing a git commit",
                    impact="Reward capped at 0.6-0.8 because _has_concrete_changes=False. Loop stalls.",
                    fix_applied="Check if backlog task is already done; skip re-execution.",
                    prevention=(
                        "Before spawning subagent: verify task not already done in git log. "
                        "If done, mark [Done] in MEMORY.md and skip. "
                        "If unclear, add explicit commit requirement to task prompt."
                    ),
                    task_id=current_task_id,
                    cycle_id=cycle_id,
                    date=date,
                )
                return {"action": "recorded_error", "entry_id": entry_id,
                        "reason": "subagent_no_commit"}

            # Only record a lesson if real code was touched OR reward is high
            if not clean_files and reward_val < 1.0:
                return {"action": "skipped", "reason": "no-real-work"}

            # Build meaningful approach from real context
            if commits_pushed > 0:
                _files_str = ", ".join(clean_files[:3]) if clean_files else "(unknown files)"
                approach = (
                    f"Subagent committed {commits_pushed} change(s): {_files_str}. "
                    f"fd.mode={fd_mode}."
                )
                reusable_insight = (
                    f"When task '{current_task_id}' succeeds: "
                    f"{commits_pushed} commit(s) to {_files_str} yield reward={reward_val}. "
                    "Replicate: same file targets, same commit pattern."
                )
            elif clean_files:
                _files_str = ", ".join(clean_files[:3])
                approach = (
                    f"Cycle changed source files: {_files_str}. "
                    f"fd.mode={fd_mode}, reward={reward_val}."
                )
                reusable_insight = (
                    f"Files {_files_str} were key to reward={reward_val} on '{current_task_id}'. "
                    "Target same files when repeating this task class."
                )
            else:
                approach = (
                    f"High-reward cycle ({reward_val}) with no source file changes. "
                    f"fd.mode={fd_mode}. Likely metadata/coordination improvement."
                )
                reusable_insight = (
                    f"Task '{current_task_id}' yields reward={reward_val} without code changes. "
                    "Coordination and metadata updates count as real progress."
                )

            entry_id = db.record_lesson(
                task_id=current_task_id,
                title=f"Optimization: {current_task_id}",
                description=summary or f"Successful cycle for task '{current_task_id}'",
                impact=f"Positive reward signal: {reward_val}",
                approach=approach,
                reusable_insight=reusable_insight,
                files_changed=artifact_paths,
                cycle_id=cycle_id,
                date=date,
            )
            return {"action": "recorded_lesson", "entry_id": entry_id}

    except Exception as exc:  # noqa: BLE001
        return {"action": "error", "reason": str(exc)}

    return {"action": "skipped", "reason": "unmatched-status"}
