"""Skill-read fitness sidecar (#939 Part C).

A corrected join makes the read count true, not decisive: #941 measures
usefulness by the with-skill versus without-skill delta, not by reads alone.

Tracks which SKILL.md files the self-evolving subagent has successfully
read during a cycle, so the scorecard can reward cycles that actively learn
from skills before acting.  Purely stdlib; no dependency on bridge.py or any
other nanobot.runtime module that could create a circular import.

Schema (``skill_fitness.json``)::

    {
        "schema_version": "skill-fitness-v1",
        "reads": [
            {
                "skill":          str,   # skill name, e.g. "github"
                "cycle_id":       str,
                "ts":             str,   # ISO-8601 UTC
                "skill_commit":   str,   # git sha of SKILL.md at read time ('' = unknown)
                "cycle_base_sha": str,   # sha the cycle branched from ('' = unknown)
                "confirmed":      bool   # True when read cycle != authoring cycle
            }
        ]
    }

Immutability invariant (#789 / #603): this file lives entirely outside the
instance workspace in the harness-owned ``state_dir``.  The bridge adds it to
``scorecard.FITNESS_SIDECARS`` so spawn-boundary tamper detection covers it
(same guard as completed.json / scorecard/latest.json).  The instance can read
it for self-knowledge but may never write it — that path is physically
impossible given that writes go through :func:`record_skill_reads`, which is
only called by the bridge.

Birth-use rule: an authoring cycle (the cycle whose commit introduced or last
changed the SKILL.md) earns **zero** fitness credit — reading your own just-
written skill before anything else has run it is not evidence of genuine
learning.  :func:`record_skill_reads` enforces this via a git commit-history
check: if the skill's last-edit commit equals or is a child of the cycle's
own base sha, ``confirmed`` is set to ``False`` and the read is still
recorded (for audit) but not counted in fitness scoring.

Deny-set: ``skill_fitness.py`` is explicitly protected by
``_RUNTIME_DENY_ALWAYS_FILES`` because it writes a harness-owned fitness
sidecar. The instance may not rewrite the collector that grants usage credit.
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "skill-fitness-v1"
SIDECAR_REL = "skill_fitness/reads.json"  # state_dir-relative path

# Bounded write: never let the reads list grow unboundedly.
_MAX_READS = 2000
_SIDECAR_MAX_BYTES = 16 * 1024 * 1024  # _MAX_READS bounds the file (~1 KB/read); see #1178
_RENAME_SCAN_TIMEOUT = 10
_RENAME_MAP_REL = "skill_fitness/renames.json"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _git_sha(repo: Path, path: str) -> str:
    """Return the sha of the most recent commit that touched *path*.

    Returns '' on any error (offline, not a git repo, file untracked).
    """
    try:
        r = subprocess.run(
            ["git", "-c", f"safe.directory={repo}", "-C", str(repo),
             "log", "-1", "--format=%H", "--", path],
            capture_output=True, text=True, timeout=10,
        )
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def _is_ancestor_or_equal(repo: Path, ancestor: str, descendant: str) -> bool:
    """True iff *ancestor* is an ancestor of (or equal to) *descendant*.

    Used by the birth-use guard: a read is confirmed only when the skill's
    last-edit commit is already an ancestor of the cycle base. Errors return
    False, so missing provenance fails closed to unconfirmed usage.
    """
    if not ancestor or not descendant:
        return False
    try:
        r = subprocess.run(
            ["git", "-c", f"safe.directory={repo}", "-C", str(repo),
             "merge-base", "--is-ancestor", ancestor, descendant],
            capture_output=True, timeout=10,
        )
        return r.returncode == 0
    except Exception:
        return False


def _read_sidecar(state_dir: Path) -> dict[str, Any]:
    """Load the sidecar; return a blank schema on any error."""
    path = Path(state_dir) / SIDECAR_REL
    try:
        if not path.is_file():
            return {"schema_version": SCHEMA_VERSION, "reads": []}
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA_VERSION:
            return {"schema_version": SCHEMA_VERSION, "reads": []}
        if not isinstance(raw.get("reads"), list):
            raw["reads"] = []
        return raw
    except Exception:
        return {"schema_version": SCHEMA_VERSION, "reads": []}


def _write_sidecar_atomic(state_dir: Path, data: dict[str, Any]) -> None:
    """Atomic bounded write: truncate to _MAX_READS, write via tmp+rename."""
    path = Path(state_dir) / SIDECAR_REL
    # #1178 Class B: the read that produced ``data`` returns a blank default
    # on a corrupt/oversize/unreadable file; writing that back would erase the
    # history. Skip and say so; an absent file is created normally.
    from nanobot.runtime.state_access import WRITABLE_STATUSES, rewrite_status

    status = rewrite_status(path, max_bytes=_SIDECAR_MAX_BYTES)
    if status not in WRITABLE_STATUSES:
        import logging

        logging.getLogger(__name__).warning("skill_fitness: write skipped, existing file is %s: %s", status, path)
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        reads = data.get("reads", [])
        if len(reads) > _MAX_READS:
            data = dict(data)
            data["reads"] = reads[-_MAX_READS:]
        payload = json.dumps(data, indent=2, ensure_ascii=False)
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(payload, encoding="utf-8")
        tmp_path.replace(path)
    except Exception:
        pass  # fail-open: a write error must never crash the bridge


def record_skill_reads(
    *,
    state_dir: Path,
    reads: list[dict[str, Any]],
    repo: "Path | None" = None,
    cycle_id: str = "",
    cycle_base_sha: str = "",
) -> int:
    """Persist *reads* (from the bridge's instrumented spawn window) to the
    sidecar.

    Each item in *reads* must have at minimum ``{"skill": str}``.  The bridge
    supplies ``cycle_id`` and ``cycle_base_sha`` from its own context; this
    function adds ``ts``, looks up ``skill_commit`` via git, and applies the
    birth-use rule to set ``confirmed``.

    Returns the count of rows appended (0 on any error or empty input).
    Fail-open: any exception degrades to 0 rows written.
    """
    if not reads:
        return 0
    try:
        sidecar = _read_sidecar(state_dir)
        appended = 0
        ts = _utc_now()
        for item in reads:
            skill_name = str(item.get("skill") or "").strip()
            skill_path = str(item.get("path") or "").strip().replace("\\", "/")
            if not skill_name or skill_path != f"skills/{skill_name}/SKILL.md":
                continue
            skill_commit = _git_sha(repo, skill_path) if repo else ""
            # Birth-use guard: if the last-edit commit of this SKILL.md is an
            # ancestor of the cycle's current HEAD (i.e. was committed in THIS
            # cycle, after cycle_base_sha), the authoring-cycle rule fires.
            # Fail closed: positive credit requires complete git provenance
            # proving the last skill edit predates this cycle's base commit.
            confirmed = bool(
                repo
                and skill_commit
                and cycle_base_sha
                and _is_ancestor_or_equal(repo, skill_commit, cycle_base_sha)
            )
            row: dict[str, Any] = {
                "skill": skill_name,
                "cycle_id": cycle_id,
                "ts": ts,
                "skill_commit": skill_commit,
                "cycle_base_sha": cycle_base_sha,
                "confirmed": confirmed,
            }
            sidecar["reads"].append(row)
            appended += 1
        if appended:
            _write_sidecar_atomic(state_dir, sidecar)
        return appended
    except Exception:
        return 0


def confirmed_reads_for_cycle(state_dir: Path, cycle_id: str) -> list[dict[str, Any]]:
    """Return the confirmed skill reads for *cycle_id* (audit helper).

    Fail-open to ``[]`` on any error.
    """
    try:
        sidecar = _read_sidecar(state_dir)
        return [
            r for r in sidecar.get("reads", [])
            if r.get("cycle_id") == cycle_id and r.get("confirmed") is True
        ]
    except Exception:
        return []


def _skill_names(selfevo_repo: Path) -> set[str]:
    root = Path(selfevo_repo) / "skills"
    try:
        return {p.parent.name for p in root.glob("*/SKILL.md") if p.is_file()}
    except Exception:
        return set()


def _load_operator_rename_map(state_dir: Path) -> dict[str, str]:
    """Load optional harness/operator-confirmed mappings for deleted paths."""
    try:
        raw = json.loads((Path(state_dir) / _RENAME_MAP_REL).read_text(encoding="utf-8"))
        return {
            str(old): str(new)
            for old, new in raw.items()
            if isinstance(old, str) and isinstance(new, str) and old and new
        } if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _rename_pairs(selfevo_repo: Path) -> dict[str, str]:
    """Return old->new skill names from git rename records.

    The history is authoritative for migrated keys. Ambiguous or deleted
    paths are intentionally omitted so their counters remain visible as
    unresolved rather than being guessed into a different skill.
    """
    try:
        result = subprocess.run(
            ["git", "-c", f"safe.directory={selfevo_repo}", "-C", str(selfevo_repo),
             "log", "--all", "--diff-filter=R", "--find-renames=50%", "--format=",
             "--name-status", "--", "skills"],
            capture_output=True, text=True, timeout=_RENAME_SCAN_TIMEOUT,
        )
        if result.returncode != 0:
            return {}
        pairs: dict[str, str] = {}
        for line in result.stdout.splitlines():
            fields = line.split("\t")
            if len(fields) != 3 or not fields[0].startswith("R"):
                continue
            old, new = fields[1].replace("\\", "/"), fields[2].replace("\\", "/")
            old_parts, new_parts = old.split("/"), new.split("/")
            if len(old_parts) == 3 and len(new_parts) == 3 and old_parts[0] == new_parts[0] == "skills" and old_parts[2] == new_parts[2] == "SKILL.md":
                pairs.setdefault(old_parts[1], new_parts[1])
        return pairs
    except Exception:
        return {}


def _skill_key_resolver(selfevo_repo: Path, state_dir: Path | None = None) -> dict[str, str]:
    current = _skill_names(selfevo_repo)
    renames = _rename_pairs(selfevo_repo)
    if state_dir is not None:
        renames.update(_load_operator_rename_map(state_dir))
    resolved = {name: name for name in current}
    for old in renames:
        candidate = old
        seen: set[str] = set()
        while candidate in renames and candidate not in seen:
            seen.add(candidate)
            candidate = renames[candidate]
        if candidate in current:
            resolved[old] = candidate
    return resolved


def _resolved_read_counts(
    state_dir: Path,
    selfevo_repo: Path,
    *,
    confirmed_only: bool,
) -> tuple[dict[str, int], dict[str, int], dict[str, str]]:
    """Join recorded keys to current skill names without dropping history."""
    data = _read_sidecar(Path(state_dir))
    resolver = _skill_key_resolver(Path(selfevo_repo), Path(state_dir))
    counts: dict[str, int] = {}
    unresolved: dict[str, int] = {}
    for row in data.get("reads", []):
        if not isinstance(row, dict) or (confirmed_only and row.get("confirmed") is not True):
            continue
        raw = str(row.get("skill") or "").strip()
        if not raw:
            continue
        current = resolver.get(raw)
        if current:
            counts[current] = counts.get(current, 0) + 1
        else:
            unresolved[raw] = unresolved.get(raw, 0) + 1
    rename_map = {old: new for old, new in resolver.items() if old != new}
    return counts, unresolved, rename_map


def skill_fitness_inventory(state_dir: Path, selfevo_repo: Path) -> dict[str, Any]:
    """Classify recorded keys and preserve unresolved history.

    ``resolved`` counts distinct recorded keys that join to a current skill;
    ``unresolvable`` counts distinct keys with no current directory or
    unambiguous verified rename successor. The returned rename map is the
    exact old-name -> current-name mapping applied by the join.

    A corrected join makes the read count true, not decisive: #941 measures
    usefulness by the with-skill versus without-skill delta, not by reads alone.
    """
    data = _read_sidecar(Path(state_dir))
    resolver = _skill_key_resolver(Path(selfevo_repo), Path(state_dir))
    keys = sorted({
        str(row.get("skill") or "").strip()
        for row in data.get("reads", [])
        if isinstance(row, dict) and str(row.get("skill") or "").strip()
    })
    confirmed_keys = {
        str(row.get("skill") or "").strip()
        for row in data.get("reads", [])
        if isinstance(row, dict)
        and row.get("confirmed") is True
        and str(row.get("skill") or "").strip()
    }
    mapped = {key: resolver.get(key) for key in keys}
    return {
        "recorded_keys": len(keys),
        "resolved": sum(mapped[key] is not None for key in keys),
        "unresolvable": sum(mapped[key] is None for key in keys),
        "unresolvable_keys": [key for key in keys if mapped[key] is None],
        "confirmed_recorded_keys": len(confirmed_keys),
        "confirmed_resolved": sum(mapped.get(key) is not None for key in confirmed_keys),
        "confirmed_unresolvable": sum(mapped.get(key) is None for key in confirmed_keys),
        "rename_map": {key: value for key, value in mapped.items() if value and key != value},
    }


def migrate_skill_keys(state_dir: Path, selfevo_repo: Path) -> dict[str, Any]:
    """Canonicalize renamed skill keys without dropping any read row.

    Resolved rows keep their evidence and gain ``skill_key_original`` plus a
    ``migrated`` status. Unknown rows retain their original ``skill`` value and
    gain ``unresolvable`` status, so missing history cannot look like zero use.
    """
    try:
        path = Path(state_dir) / SIDECAR_REL
        data = _read_sidecar(Path(state_dir))
        reads = data.get("reads", [])
        if not path.is_file() or not isinstance(reads, list):
            return {"ok": False, "reason": "reads_unavailable", "migrated": 0, "unresolvable": 0}
        resolver = _skill_key_resolver(Path(selfevo_repo), Path(state_dir))
        migrated = 0
        unresolved = 0
        changed = False
        output: list[dict[str, Any]] = []
        for raw_row in reads:
            if not isinstance(raw_row, dict):
                output.append(raw_row)
                continue
            row = dict(raw_row)
            raw_skill = str(row.get("skill") or "").strip()
            canonical = resolver.get(raw_skill)
            if canonical is None:
                if raw_skill:
                    unresolved += 1
                    if row.get("skill_key_status") != "unresolvable" or row.get("skill_key_original") != raw_skill:
                        row["skill_key_original"] = raw_skill
                        row["skill_key_status"] = "unresolvable"
                        changed = True
            elif canonical != raw_skill:
                migrated += 1
                if row.get("skill") != canonical or row.get("skill_key_original") != raw_skill or row.get("skill_key_status") != "migrated":
                    row["skill"] = canonical
                    row["skill_key_original"] = raw_skill
                    row["skill_key_status"] = "migrated"
                    changed = True
            else:
                if row.get("skill_key_status") not in {"current", "migrated"}:
                    row["skill_key_status"] = "current"
                    changed = True
            output.append(row)
        if changed:
            data["reads"] = output
            _write_sidecar_atomic(Path(state_dir), data)
        return {"ok": True, "migrated": migrated, "unresolvable": unresolved, "changed": changed}
    except Exception:
        return {"ok": False, "reason": "migration_error", "migrated": 0, "unresolvable": 0}


def last_confirmed_skill_reads(
    state_dir: Path, selfevo_repo: "Path | None" = None
) -> dict[str, str]:
    """Return newest confirmed read timestamp per current skill name.

    When ``selfevo_repo`` is supplied, historical names are resolved through
    the rename-aware join before the timestamps are returned. Without a repo,
    this remains the legacy read-only lookup for callers that cannot inspect
    the catalogue.
    """
    try:
        data = _read_sidecar(Path(state_dir))
        resolver = _skill_key_resolver(Path(selfevo_repo), Path(state_dir)) if selfevo_repo is not None else {}
        latest: dict[str, str] = {}
        for row in data.get("reads", []):
            if not isinstance(row, dict) or row.get("confirmed") is not True:
                continue
            raw_skill = str(row.get("skill") or "").strip()
            skill = resolver.get(raw_skill, raw_skill)
            ts = str(row.get("ts") or "").strip()
            if skill and ts and ts > latest.get(skill, ""):
                latest[skill] = ts
        return latest
    except Exception:
        return {}


# ── #1342: zero-read census (report only, never gates) ──────────────────────
# Which skills nobody has read in the rolling window. Written next to the
# skill-candidate sidecar under state/demand/ so the same readers (dashboard,
# operator) find it. Retirement stays an operator decision (#958 has the demand
# path); this file only names the idle skills with their evidence.
CENSUS_SCHEMA = "skill-census-v1"
CENSUS_REL = "demand/skill_census.json"
_CENSUS_WINDOW_DAYS = 30
_CENSUS_MAX_SKILLS = 200


def zero_read_census(
    state_dir: Path, selfevo_repo: Path, *, now: "datetime | None" = None
) -> list[dict[str, Any]]:
    """Rows of :func:`census` — kept for callers that only want the idle list."""
    return census(state_dir, selfevo_repo, now=now)["zero_read"]


def _load_reads_strict(state_dir: Path) -> "list[dict[str, Any]] | None":
    """The sidecar's ``reads`` list, or None when the source is unavailable.

    Unlike :func:`_read_sidecar` (which blanks any problem into a valid empty
    schema for the fitness path), the census must tell "no reads recorded"
    (a valid file with an empty list) from "no data" (missing file, invalid
    JSON, wrong schema, ``reads`` not a list). Only the first is evidence.
    """
    path = Path(state_dir) / SIDECAR_REL
    try:
        if not path.is_file():
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA_VERSION:
        return None
    reads = raw.get("reads")
    return reads if isinstance(reads, list) else None


def _parse_read_ts(value: Any) -> "datetime | None":
    """Timezone-aware ISO-8601 timestamp, or None (naive, malformed, non-string)."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else None


def census(
    state_dir: Path, selfevo_repo: Path, *, now: "datetime | None" = None
) -> dict[str, Any]:
    """Skills under ``<repo>/skills/*/SKILL.md`` with no confirmed read in the window.

    Each row: ``{"skill", "reads_in_window", "last_read"}`` — ``reads_in_window``
    is always 0 by construction (the census lists the idle ones), ``last_read``
    is the newest confirmed read ever, or None.

    A read counts only with a parseable, timezone-aware ``ts`` inside
    ``cutoff <= ts <= now``; an unknown or future timestamp is not evidence of
    a read (it is skipped, not treated as fresh). Fail-open, never a
    rejection: an unavailable source (missing/invalid ``reads.json``) yields
    ``ok: False`` with an EMPTY census, because "no data" must not be
    published as "every skill has zero reads"; a valid file with no rows is
    evidence and yields every skill idle with ``ok: True``.
    """
    try:
        now_dt = now or datetime.now(timezone.utc)
        cutoff_dt = now_dt - timedelta(days=_CENSUS_WINDOW_DAYS)
        skills_root = Path(selfevo_repo) / "skills"
        names = sorted(
            p.parent.name for p in skills_root.glob("*/SKILL.md") if p.is_file()
        )[:_CENSUS_MAX_SKILLS]
        reads = _load_reads_strict(Path(state_dir))
        if reads is None:
            return {"ok": False, "reason": "reads_unavailable", "skills_total": len(names), "zero_read": []}
        in_window: dict[str, int] = {}
        last_read: dict[str, datetime] = {}
        resolver = _skill_key_resolver(Path(selfevo_repo), Path(state_dir))
        unresolved: dict[str, int] = {}
        for row in reads:
            if not isinstance(row, dict) or row.get("confirmed") is not True:
                continue
            raw_skill = str(row.get("skill") or "").strip()
            skill = resolver.get(raw_skill)
            if skill is None:
                unresolved[raw_skill] = unresolved.get(raw_skill, 0) + 1
                continue
            ts = _parse_read_ts(row.get("ts"))
            if not skill or ts is None or ts > now_dt:
                continue  # unknown or future timestamp: not a proven read
            if skill not in last_read or ts > last_read[skill]:
                last_read[skill] = ts
            if ts >= cutoff_dt:
                in_window[skill] = in_window.get(skill, 0) + 1
        return {
            "ok": True,
            "skills_total": len(names),
            "zero_read": [
                {
                    "skill": name,
                    "reads_in_window": 0,
                    "last_read": last_read[name].isoformat().replace("+00:00", "Z") if name in last_read else None,
                }
                for name in names
                if in_window.get(name, 0) == 0
            ],
            "unresolvable_reads": unresolved,
            "rename_map": {old: new for old, new in resolver.items() if old != new},
        }
    except Exception:
        return {"ok": False, "reason": "census_error", "skills_total": 0, "zero_read": []}


def write_zero_read_census(
    state_dir: Path, selfevo_repo: Path, *, now: "datetime | None" = None
) -> dict[str, Any]:
    """Migrate skill keys, then write the rename-aware zero-read census."""
    migrate_skill_keys(state_dir, selfevo_repo)
    result = census(state_dir, selfevo_repo, now=now)
    rows = result["zero_read"]
    payload = {
        "schema": CENSUS_SCHEMA,
        "written_at": (now or datetime.now(timezone.utc)).isoformat().replace("+00:00", "Z"),
        "window_days": _CENSUS_WINDOW_DAYS,
        "ok": result["ok"],
        "reason": result.get("reason"),
        "skills_total": result["skills_total"],
        "zero_read": rows,
        "unresolvable_reads": result.get("unresolvable_reads", {}),
        "rename_map": result.get("rename_map", {}),
    }
    path = Path(state_dir) / CENSUS_REL
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(path)
    except Exception:
        return {"ok": False, "written": 0, "path": str(path)}
    return {"ok": result["ok"], "written": len(rows), "path": str(path)}
