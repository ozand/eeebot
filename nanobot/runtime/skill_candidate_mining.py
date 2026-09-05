"""Deterministic recurring-action skill candidate miner (#1006).

Architecture (F2): mining runs in the DAILY job (ExecStartPost on the
action-index service) and writes a sidecar at
``state/demand/skill_candidates.json``.  ``demand.py`` reads only that
sidecar — no mining occurs in the cycle path.

Sidecar schema: ``{"schema": "skill-candidates-v1", "written_at": <iso>,
"candidates": [{"sequence": [...], "cycles": N, "days": N, "samples": [...]}]}``.
The top-N cap (default 3, env ``SELFEVO_SKILL_CANDIDATE_TOP_N``) is applied
before writing, so demand presentation always has a bounded load.
"""
from __future__ import annotations

import gzip
import json
import os
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_DEFAULT_WINDOW_DAYS = 30
_DEFAULT_MIN_CYCLES = 8
_DEFAULT_MIN_DAYS = 3
_MAX_SAMPLES = 3
_DEFAULT_NGRAMS = (2, 3, 4, 5)
_DEFAULT_TOP_N = 3

_SIDECAR_SCHEMA = "skill-candidates-v1"

# F3: a candidate must contain at least one state-changing or executing action.
# Pure read/list n-grams are denied categorically (not via enumerated deny-list).
_MEANINGFUL_PREFIXES = ("exec:", "edit:", "write:")

# F4: legacy var/* templates polluted by pre-#1011 backfill.  Miner skips any
# action template that starts with "var/" regardless of suffix.
_LEGACY_VAR_PREFIX = "var/"

_TRIVIAL = frozenset({
    ("exec:pytest", "exec:git-commit"),
    ("exec:pytest", "exec:git-commit", "exec:git-push"),
})


def _row_actions(row: dict[str, Any]) -> list[str]:
    """Action tokens of an index row (#1348).

    Prefer ``actions_detail`` (argv head beyond the interpreter + one target,
    or the concrete edit/read path) when the row carries it with the same
    length as ``actions``; otherwise fall back to the coarse ``actions``
    templates exactly as before — rows written before #1348 keep working and
    are never rewritten.
    """
    actions = [str(a) for a in row.get("actions") or []]
    detail = row.get("actions_detail")
    if isinstance(detail, list) and len(detail) == len(actions) and all(isinstance(d, str) for d in detail):
        return list(detail)
    return actions


def _is_meaningful(sequence: tuple[str, ...]) -> bool:
    """F3: true iff the sequence contains at least one exec/edit/write action."""
    return any(a.startswith(_MEANINGFUL_PREFIXES) for a in sequence)


def _has_legacy_var(sequence: tuple[str, ...]) -> bool:
    """F4: true iff any action's path template is a legacy var/* form.

    Action templates have the form ``prefix:path_template`` (e.g.
    ``read:var/*.py``) or just a bare template (e.g. ``var/lib/*``).  Both
    patterns are matched: the part after the first colon must not start with
    ``var/``, and the whole token must not start with ``var/`` either.
    """
    for a in sequence:
        # bare var/* token (no colon)
        if a.startswith(_LEGACY_VAR_PREFIX):
            return True
        # prefix:var/* e.g. read:var/*.py
        if ":" in a and a.split(":", 1)[1].startswith(_LEGACY_VAR_PREFIX):
            return True
    return False


def _trivial_patterns() -> frozenset[tuple[str, ...]]:
    """Return built-in loop patterns plus operator comma-separated additions.

    Format: ``exec:pytest>exec:git-commit``. This is an operator environment
    knob, not instance state, so the instance cannot disable suppression.
    """
    extra = os.environ.get("SELFEVO_SKILL_CANDIDATE_DENY", "")
    patterns = set(_TRIVIAL)
    for raw in extra.split(","):
        parts = tuple(part.strip() for part in raw.split(">") if part.strip())
        if len(parts) >= 2:
            patterns.add(parts)
    return frozenset(patterns)


def _int_env(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return default


def _iter_rows(state_dir: Path, window_days: int) -> list[dict[str, Any]]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    index_dir = Path(state_dir) / "action_index"
    paths = sorted([*index_dir.glob("*.jsonl"), *index_dir.glob("*.jsonl.gz")])
    rows: list[dict[str, Any]] = []
    for path in paths:
        try:
            opener = gzip.open if path.name.endswith(".gz") else open
            with opener(path, "rt", encoding="utf-8") as fh:  # type: ignore[call-arg]
                for line in fh:
                    try:
                        row = json.loads(line)
                    except Exception:
                        continue
                    if not isinstance(row, dict) or not isinstance(row.get("actions"), list):
                        continue
                    ts = _parse_ts(row.get("ts"))
                    if ts is not None and ts >= cutoff:
                        rows.append(row)
        except (OSError, EOFError, gzip.BadGzipFile):
            continue
    return rows


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _grams(actions: list[str]) -> list[tuple[str, ...]]:
    result = []
    for n in _DEFAULT_NGRAMS:
        result.extend(tuple(actions[i:i+n]) for i in range(len(actions) - n + 1))
    return result


def _is_subsequence(short: tuple[str, ...], long: tuple[str, ...]) -> bool:
    return len(short) < len(long) and any(long[i:i+len(short)] == short for i in range(len(long) - len(short) + 1))


def _existing_skill_match(sequence: tuple[str, ...], selfevo_repo: Path | None) -> bool:
    if not selfevo_repo:
        return False
    try:
        needle = " ".join(sequence).lower()
        # #1348: a detail token's body ("python3 scripts/check_style.py") is
        # the nameable procedure step; a skill whose text carries every such
        # body already covers the sequence. Bare path mentions do not count.
        bodies = sorted({
            token.split(":", 1)[-1].lower()
            for token in sequence
            if " " in token
        })
        for path in (Path(selfevo_repo) / "skills").glob("*/SKILL.md"):
            text = path.read_text(encoding="utf-8", errors="replace").lower()
            if needle in text or all(token in text for token in sequence):
                return True
            if bodies and all(body in text for body in bodies):
                return True
    except Exception:
        return False
    return False


def mine(state_dir: Path, selfevo_repo: Path | None = None) -> list[dict[str, Any]]:
    """Return longest qualifying recurring n-grams; fail-open to empty.

    F3: candidates must contain at least one exec/edit/write action.
    F4: actions matching var/* legacy templates are rejected entirely.
    Results are ranked by (cycles × days) descending, then capped to top-N.
    """
    try:
        min_cycles = _int_env("SELFEVO_SKILL_CANDIDATE_MIN_CYCLES", _DEFAULT_MIN_CYCLES)
        min_days = _int_env("SELFEVO_SKILL_CANDIDATE_MIN_DAYS", _DEFAULT_MIN_DAYS)
        top_n = _int_env("SELFEVO_SKILL_CANDIDATE_TOP_N", _DEFAULT_TOP_N)
        stats: dict[tuple[str, ...], dict[str, Any]] = defaultdict(lambda: {"cycles": set(), "days": set(), "samples": []})
        for row in _iter_rows(Path(state_dir), _int_env("SELFEVO_SKILL_CANDIDATE_WINDOW_DAYS", _DEFAULT_WINDOW_DAYS)):
            cycle = str(row.get("cycle_id") or "")
            day = str(row.get("ts") or "")[:10]
            if not cycle or not day:
                continue
            seen = set(_grams(_row_actions(row)))
            for gram in seen:
                # F4: skip any gram containing a legacy var/* template
                if _has_legacy_var(gram):
                    continue
                stats[gram]["cycles"].add(cycle)
                stats[gram]["days"].add(day)
                if len(stats[gram]["samples"]) < _MAX_SAMPLES:
                    stats[gram]["samples"].append(cycle)
        trivial = _trivial_patterns()
        qualifying = {
            gram: data for gram, data in stats.items()
            if len(data["cycles"]) >= min_cycles
            and len(data["days"]) >= min_days
            and gram not in trivial
            and _is_meaningful(gram)  # F3: categorical pure-read/list denial
        }
        selected = {}
        for gram, data in qualifying.items():
            if any(_is_subsequence(gram, other) for other in qualifying):
                continue
            if _existing_skill_match(gram, selfevo_repo):
                continue
            selected[gram] = data
        # Rank by (cycles × days) descending for deterministic top-N selection
        ranked = sorted(
            selected.items(),
            key=lambda item: (-len(item[1]["cycles"]) * len(item[1]["days"]), item[0]),
        )
        return [
            {
                "sequence": list(gram),
                "cycles": len(data["cycles"]),
                "days": len(data["days"]),
                "samples": data["samples"],
            }
            for gram, data in ranked[:top_n]
        ]
    except Exception:
        return []


def _sidecar_path(state_dir: Path) -> Path:
    return Path(state_dir) / "demand" / "skill_candidates.json"


def write_sidecar(state_dir: Path, selfevo_repo: Path | None = None) -> dict[str, Any]:
    """Mine candidates and write the sidecar (F1+F2: daily job entry point).

    Returns a summary dict for CLI output.
    """
    candidates = mine(state_dir, selfevo_repo)
    sidecar = {
        "schema": _SIDECAR_SCHEMA,
        "written_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "candidates": candidates,
    }
    path = _sidecar_path(state_dir)
    temporary: str | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(sidecar, indent=2, ensure_ascii=False) + "\n"
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
        ) as fh:
            temporary = fh.name
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        if os.name != "nt":
            os.chmod(temporary, 0o644)  # NamedTemporaryFile creates 0600; normalize for non-agent readers (#1096)
        os.replace(temporary, path)
        temporary = None
    except OSError:
        pass
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass
    return {"written": len(candidates), "path": str(path)}


def read_sidecar(state_dir: Path) -> list[dict[str, Any]]:
    """Read candidates from the sidecar (F2: demand cycle path entry point).

    Returns empty list on any error or missing/stale sidecar.
    """
    try:
        path = _sidecar_path(state_dir)
        if not path.is_file():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("schema") != _SIDECAR_SCHEMA:
            return []
        candidates = data.get("candidates")
        if not isinstance(candidates, list):
            return []
        return candidates
    except Exception:
        return []


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Mine recurring action sequences and write skill-candidate sidecar")
    parser.add_argument("--state-root", type=Path, default=None)
    parser.add_argument("--repo", type=Path, default=None)
    args = parser.parse_args()
    state = args.state_root or Path(os.environ.get("STATE_DIR", Path.home() / ".nanobot"))
    summary = write_sidecar(state, args.repo)
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
