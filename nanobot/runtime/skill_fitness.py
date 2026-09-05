"""Skill-read fitness sidecar (#939 Part C).

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


def last_confirmed_skill_reads(state_dir: Path) -> dict[str, str]:
    """Return newest confirmed read timestamp per skill name."""
    try:
        data = _read_sidecar(Path(state_dir))
        latest: dict[str, str] = {}
        for row in data.get("reads", []):
            if not isinstance(row, dict) or row.get("confirmed") is not True:
                continue
            skill = str(row.get("skill") or "").strip()
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
        for row in reads:
            if not isinstance(row, dict) or row.get("confirmed") is not True:
                continue
            skill = str(row.get("skill") or "").strip()
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
        }
    except Exception:
        return {"ok": False, "reason": "census_error", "skills_total": 0, "zero_read": []}


def write_zero_read_census(
    state_dir: Path, selfevo_repo: Path, *, now: "datetime | None" = None
) -> dict[str, Any]:
    """Write ``state/demand/skill_census.json`` (atomic). Never raises."""
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
