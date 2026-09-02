"""Decoupled per-cycle transcript reflector (#1007)."""
from __future__ import annotations

import argparse
import gzip
import inspect
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from nanobot.observability.llm_telemetry import call_context, record_llm_call, record_llm_prompt
from nanobot.runtime.model_registry import resolve_model

_MAX_CYCLES = 3
_MAX_RECOMMENDATIONS = 3
_MAX_CONSECUTIVE_ERRORS = 3
_MAX_RUNTIME_SECONDS = 600
_JOURNAL_TAIL = 10
# #1178: reflections.jsonl rotation. The journal was append-only with no
# rotation and crossed 512 KiB on the host around 2026-08-29 (738,050 B on
# 2026-09-02), which is what switched the curator's reflector promotion off
# (#1183). At the cap the live file is gzip-archived whole to
# reflector/archive/reflections-YYYY-MM-DD.jsonl.gz and truncated; archives
# older than the retention are removed; readers consult the newest
# _ARCHIVE_READ_FILES archives in addition to the live file.
_MAX_JOURNAL_BYTES = 512 * 1024
_ARCHIVE_RETENTION_DAYS = 90
_ARCHIVE_READ_FILES = 2
_ARCHIVE_RE = re.compile(r"^reflections-(\d{4}-\d{2}-\d{2})(?:-(\d+))?\.jsonl\.gz$")
_MAX_TRANSCRIPT_CHARS = 48_000
_MAX_LEDGER_CHARS = 12_000
_MAX_JOURNAL_CHARS = 12_000
_VALID_FINDINGS = {"wasted_steps", "error_pattern", "tool_misuse", "good_practice"}
_VALID_RECOMMENDATIONS = {"skill_candidate", "instruction_change", "approach_hint"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _iter_jsonl(path: Path, byte_needles: tuple[bytes, ...] = ()):
    opener = gzip.open if path.name.endswith(".gz") else open
    try:
        mode = "rb" if byte_needles else "rt"
        with opener(path, mode) as fh:  # type: ignore[call-arg]
            for line in fh:
                if byte_needles and not any(needle in line for needle in byte_needles):
                    continue
                try:
                    value = json.loads(line.decode("utf-8") if byte_needles else line)
                    if isinstance(value, dict):
                        yield value
                except Exception:
                    continue
    except Exception:
        return


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return list(_iter_jsonl(path))


def _files(directory: Path, pattern: str) -> list[Path]:
    return sorted([*directory.glob(pattern), *directory.glob(pattern + ".gz")])


def _ledger_rows(state_dir: Path) -> list[dict[str, Any]]:
    directory = state_dir / "ledger"
    rows: list[dict[str, Any]] = []
    for path in [directory / "cycles.jsonl", *_files(directory, "cycles-*.jsonl")]:
        if path.is_file():
            rows.extend(_read_jsonl(path))
    return rows


def _file_contains(path: Path, needles: set[str]) -> bool:
    """Fast substring prefilter over raw file bytes to avoid full json parse."""
    if not needles:
        return False
    try:
        opener = gzip.open if path.name.endswith(".gz") else open
        with opener(path, "rb") as fh:  # type: ignore[call-arg]
            for line in fh:
                if any(needle.encode("utf-8") in line for needle in needles):
                    return True
        return False
    except Exception:
        return True


def _prompt_records(
    state_dir: Path,
    candidates: list[dict[str, Any]],
    deadline: float | None = None,
) -> dict[str, dict[str, Any]]:
    """Stream only prompt files near the bounded candidates' ledger dates."""
    directory = state_dir / "llm_calls" / "prompts"
    candidate_ids = {str(row.get("cycle_id") or "") for row in candidates if row.get("cycle_id")}
    if not candidate_ids:
        return {}
    days: set[str] = set()
    for row in candidates:
        try:
            date = datetime.fromisoformat(str(row.get("ts")).replace("Z", "+00:00")).date()
            for offset in (-1, 0, 1):
                days.add((date + timedelta(days=offset)).isoformat())
        except (TypeError, ValueError):
            continue
    latest: dict[str, dict[str, Any]] = {}
    paths = [path for path in _files(directory, "*.jsonl") if path.stem[:10] in days]
    byte_needles = tuple(needle.encode("utf-8") for needle in candidate_ids)
    supports_filter = len(inspect.signature(_iter_jsonl).parameters) >= 2
    for path in paths:
        if deadline is not None and time.monotonic() >= deadline:
            break
        if not supports_filter and not _file_contains(path, candidate_ids):
            continue
        records = _iter_jsonl(path, byte_needles) if supports_filter else _iter_jsonl(path)
        for record in records:
            if deadline is not None and time.monotonic() >= deadline:
                return latest
            cycle_id = str(record.get("cycle_id") or "").strip()
            if cycle_id not in candidate_ids:
                continue
            sequence = record.get("seq") if isinstance(record.get("seq"), int) else -1
            previous = latest.get(cycle_id)
            if previous is None or sequence >= int(previous.get("seq") or -1):
                latest[cycle_id] = record
    return latest


def _watermark_path(state_dir: Path) -> Path:
    return state_dir / "reflector" / "watermark.json"


def _load_watermark(state_dir: Path) -> str:
    try:
        value = json.loads(_watermark_path(state_dir).read_text(encoding="utf-8"))
        return str(value.get("last_processed") or "") if isinstance(value, dict) else ""
    except Exception:
        return ""


def _save_watermark(state_dir: Path, cycle_id: str) -> None:
    path = _watermark_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".watermark.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"last_processed": cycle_id, "timestamp": _now()}, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _journal_path(state_dir: Path | str) -> Path:
    return Path(state_dir) / "reflector" / "reflections.jsonl"


def _archives(state_dir: Path | str) -> list[Path]:
    """Rotated journals, oldest first (by the date and sequence in the name)."""
    archive_dir = _journal_path(state_dir).parent / "archive"
    found: list[tuple[str, int, Path]] = []
    if archive_dir.is_dir():
        for candidate in archive_dir.iterdir():
            match = _ARCHIVE_RE.match(candidate.name)
            if match and candidate.is_file():
                found.append((match.group(1), int(match.group(2) or 0), candidate))
    return [path for _, _, path in sorted(found)]


def _rotate_journal(state_dir: Path | str) -> Path | None:
    """Archive and truncate the live journal once it passes :data:`_MAX_JOURNAL_BYTES`.

    Archive first (gzip of the whole live file, written via tmp + replace under
    a date name that is never reused), truncate second (empty tmp + replace),
    so an interruption between the two leaves a duplicate archive, never a
    lost row. Then drop archives older than :data:`_ARCHIVE_RETENTION_DAYS`.
    Returns the archive path when a rotation happened."""
    path = _journal_path(state_dir)
    if not path.is_file() or path.stat().st_size <= _MAX_JOURNAL_BYTES:
        return None
    payload = path.read_bytes()
    archive_dir = path.parent / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    dest = archive_dir / f"reflections-{today}.jsonl.gz"
    sequence = 1
    while dest.exists():
        dest = archive_dir / f"reflections-{today}-{sequence}.jsonl.gz"
        sequence += 1
    fd, temporary = tempfile.mkstemp(prefix=".reflections.", suffix=".gz.tmp", dir=str(archive_dir))
    os.close(fd)
    try:
        with gzip.open(temporary, "wb") as fh:
            fh.write(payload)
        os.replace(temporary, dest)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    fd, temporary = tempfile.mkstemp(prefix=".reflections.", dir=str(path.parent))
    os.close(fd)
    os.replace(temporary, path)  # the live journal starts empty
    cutoff = (datetime.now(timezone.utc) - timedelta(days=_ARCHIVE_RETENTION_DAYS)).strftime("%Y-%m-%d")
    for old in _archives(state_dir):
        match = _ARCHIVE_RE.match(old.name)
        if match and match.group(1) < cutoff:
            try:
                old.unlink()
            except OSError:
                pass
    return dest


def _append_journal(state_dir: Path, row: dict[str, Any]) -> None:
    path = _journal_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    try:
        _rotate_journal(state_dir)
    except Exception as exc:  # the append succeeded; a rotation problem is reported, not hidden
        print(f"reflector: journal rotation failed: {exc!r}", file=sys.stderr)


def reflection_files(state_dir: Path | str, *, archives: int = _ARCHIVE_READ_FILES) -> list[Path]:
    """The newest ``archives`` rotated journals (oldest first) followed by the live journal."""
    files = _archives(state_dir)[-max(0, archives):] if archives else []
    live = _journal_path(state_dir)
    if live.is_file():
        files.append(live)
    return files


def iter_reflection_rows(state_dir: Path | str, byte_needles: tuple[bytes, ...] = (), *, archives: int = _ARCHIVE_READ_FILES):
    """Rows across :func:`reflection_files`, oldest first; the rotation-aware
    read every consumer of the journal should use (#1178)."""
    for path in reflection_files(state_dir, archives=archives):
        yield from _iter_jsonl(path, byte_needles)


def _journal_tail(state_dir: Path) -> list[dict[str, Any]]:
    return list(iter_reflection_rows(state_dir))[-_JOURNAL_TAIL:]


def _completed_cycles(rows: list[dict[str, Any]], watermark: str) -> list[dict[str, Any]]:
    outcomes = [row for row in rows if row.get("phase") == "outcome" and row.get("cycle_id")]
    outcomes.sort(key=lambda row: str(row.get("ts") or ""))
    if not outcomes:
        return []
    if not watermark:
        return outcomes
    for index, row in enumerate(outcomes):
        if str(row.get("cycle_id")) == watermark:
            return outcomes[index + 1 :]
    # Watermark points to an unknown/ancient cycle: fast-forward to latest to avoid backlog loop
    return outcomes[-1:]


def _messages(cycle_id: str, transcript: dict[str, Any], ledger: list[dict[str, Any]], journal: list[dict[str, Any]]) -> list[dict[str, str]]:
    system = (
        "Analyze every message, skill, command, tool call/result, error, retry, and detour in this completed cycle. "
        "Return ONLY strict JSON with keys cycle_id, summary, findings, recommendations, followed_previous, and optional mermaid. "
        "finding kind must be wasted_steps, error_pattern, tool_misuse, or good_practice. "
        "recommendation kind must be skill_candidate, instruction_change, or approach_hint. "
        "Each finding has kind/detail; each recommendation has kind/detail/evidence; return at most three recommendations. "
        "Recommendations are steering only: do not edit files, invent evidence, or claim scorecard value."
    )
    user = (
        f"CYCLE_ID: {cycle_id}\nTRANSCRIPT:\n{json.dumps(transcript, ensure_ascii=False)[:_MAX_TRANSCRIPT_CHARS]}\n"
        f"LEDGER:\n{json.dumps(ledger, ensure_ascii=False)[:_MAX_LEDGER_CHARS]}\n"
        f"PRIOR REFLECTIONS:\n{json.dumps(journal, ensure_ascii=False)[:_MAX_JOURNAL_CHARS]}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _parse_output(value: Any, cycle_id: str) -> dict[str, Any] | None:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            return None
    if not isinstance(value, dict) or str(value.get("cycle_id") or "") != cycle_id:
        return None
    if not isinstance(value.get("summary"), str) or not isinstance(value.get("findings"), list) or not isinstance(value.get("recommendations"), list):
        return None
    findings = []
    for item in value["findings"]:
        if not isinstance(item, dict) or item.get("kind") not in _VALID_FINDINGS or not str(item.get("detail") or "").strip():
            return None
        findings.append({"kind": item["kind"], "detail": str(item["detail"])[:1000]})
    recommendations = []
    for item in value["recommendations"][:_MAX_RECOMMENDATIONS]:
        if not isinstance(item, dict) or item.get("kind") not in _VALID_RECOMMENDATIONS or not str(item.get("detail") or "").strip():
            return None
        recommendations.append({"kind": item["kind"], "detail": str(item["detail"])[:1000], "evidence": str(item.get("evidence") or "")[:1000]})
    result = {
        "cycle_id": cycle_id,
        "summary": value["summary"][:2000],
        "findings": findings,
        "recommendations": recommendations,
        "followed_previous": value.get("followed_previous") if isinstance(value.get("followed_previous"), list) else [],
    }
    if isinstance(value.get("mermaid"), str):
        result["mermaid"] = value["mermaid"][:4000]
    return result


def _default_llm(messages: list[dict[str, str]], model: str, cycle_id: str) -> str:
    from openai import OpenAI
    base_url = os.environ.get("LITELLM_BASE_URL", "").strip()
    api_key = os.environ.get("LITELLM_API_KEY", "").strip()
    if not base_url or not api_key:
        raise RuntimeError("litellm credentials not configured")
    started = time.monotonic()
    response = OpenAI(base_url=base_url, api_key=api_key, timeout=120).chat.completions.create(model=model, messages=messages, max_tokens=1600, temperature=0.2)
    choice = response.choices[0]
    content = getattr(getattr(choice, "message", None), "content", "") or ""
    usage_obj = getattr(response, "usage", None)
    usage = {key: int(getattr(usage_obj, key, 0) or 0) for key in ("prompt_tokens", "completion_tokens", "total_tokens")}
    with call_context(cycle_id, "reflector"):
        record_llm_call(model=model, duration_ms=(time.monotonic() - started) * 1000, usage=usage, finish_reason=getattr(choice, "finish_reason", ""), retries=0)
        record_llm_prompt(messages=messages, content=content, reasoning_content=None, finish_reason=getattr(choice, "finish_reason", ""), model=model, prompt_tokens=usage["prompt_tokens"], completion_tokens=usage["completion_tokens"])
    return content


def run_reflector(
    state_dir: Path,
    *,
    llm: Callable[[list[dict[str, str]], str], Any] | None = None,
    max_cycles: int = _MAX_CYCLES,
    max_consecutive_errors: int = _MAX_CONSECUTIVE_ERRORS,
    max_runtime_seconds: float = _MAX_RUNTIME_SECONDS,
) -> dict[str, int]:
    state_dir = Path(state_dir)
    deadline = time.monotonic() + max(1.0, float(max_runtime_seconds))
    rows = _ledger_rows(state_dir)
    outcomes = [row for row in rows if row.get("phase") == "outcome" and row.get("cycle_id")]
    outcomes.sort(key=lambda row: str(row.get("ts") or ""))
    watermark = _load_watermark(state_dir)
    known_watermarks = {str(row.get("cycle_id")) for row in outcomes}
    if outcomes and watermark and watermark not in known_watermarks:
        watermark = str(outcomes[-1]["cycle_id"])
        _save_watermark(state_dir, watermark)
    candidates = _completed_cycles(rows, watermark)[:max(1, int(max_cycles))]
    result = {
        "candidates": len(candidates),
        "processed": 0,
        "skipped_pruned": 0,
        "errors": 0,
        "consecutive_errors": 0,
    }
    skipped_ids = {
        str(row.get("cycle_id") or "")
        for row in iter_reflection_rows(state_dir, (b"skipped_pruned",))
        if row.get("status") == "skipped_pruned"
    }
    # A previously skipped cycle is eligible again when its retained plain or
    # gzip transcript is now discoverable; only keep the skip terminal when no
    # transcript exists in either archive form.
    result["candidates"] = len(candidates)
    prompts = _prompt_records(state_dir, candidates, deadline=deadline)
    candidates = [
        row for row in candidates
        if str(row.get("cycle_id") or "") not in skipped_ids
        or str(row.get("cycle_id") or "") in prompts
    ]
    result["candidates"] = len(candidates)
    proposed = {str(row.get("cycle_id")): row for row in rows if row.get("phase") == "proposed"}
    consecutive_errs = 0
    for outcome in candidates:
        if time.monotonic() >= deadline and result["processed"] > 0:
            break
        cycle_id = str(outcome["cycle_id"])
        transcript = prompts.get(cycle_id)
        if not transcript:
            _append_journal(state_dir, {"cycle_id": cycle_id, "timestamp": _now(), "summary": "Transcript already pruned; cycle skipped.", "findings": [], "recommendations": [], "followed_previous": [], "status": "skipped_pruned"})
            _save_watermark(state_dir, cycle_id)
            result["skipped_pruned"] += 1
            consecutive_errs = 0
            continue
        context_rows = [row for row in rows if str(row.get("cycle_id") or "") == cycle_id]
        if cycle_id in proposed:
            context_rows.insert(0, proposed[cycle_id])
        try:
            messages = _messages(cycle_id, transcript, context_rows, _journal_tail(state_dir))
            model = resolve_model("reflector", strip_openai=True)
            response = llm(messages, model) if llm else _default_llm(messages, model, cycle_id)
            parsed = _parse_output(response, cycle_id)
            if parsed is None:
                raise ValueError("malformed reflector output")
            _append_journal(state_dir, {**parsed, "timestamp": _now()})
            _save_watermark(state_dir, cycle_id)
            result["processed"] += 1
            consecutive_errs = 0
        except Exception as exc:
            _append_journal(state_dir, {"cycle_id": cycle_id, "timestamp": _now(), "summary": "Reflector error; cycle will be retried.", "findings": [], "recommendations": [], "followed_previous": [], "status": "error", "error": f"{type(exc).__name__}: {exc}"[:500]})
            result["errors"] += 1
            consecutive_errs += 1
            result["consecutive_errors"] = consecutive_errs
            if consecutive_errs >= max(1, int(max_consecutive_errors)):
                break
    return result


def mark_reflection_consumed(
    state_dir: Path | str,
    recommendation_detail: str = "",
    demand_id: str = "",
    cycle_id: str = "",
    summary: str = "",
) -> bool:
    """Mark a specific reflection recommendation as consumed in reflections.jsonl (#1038).

    Matches by exact recommendation ``detail`` (or matching demand identity ``demand_id``),
    or falls back to entry ``summary`` if detail is not provided.
    Writes atomically via a temporary file with fsync and os.replace. Since
    #1178 a row that has rotated into ``reflector/archive/`` is marked there
    (the live file first, then the newest archives).
    """
    candidates = [_journal_path(state_dir), Path(state_dir) / "reflections.jsonl"]
    candidates += list(reversed(_archives(state_dir)))[:_ARCHIVE_READ_FILES]
    for p in candidates:
        if p.is_file() and _mark_in_file(p, recommendation_detail, demand_id, cycle_id, summary):
            return True
    return False


def _mark_in_file(p: Path, recommendation_detail: str, demand_id: str, cycle_id: str, summary: str) -> bool:
    """:func:`mark_reflection_consumed` over one journal file (plain or ``.gz``)."""
    is_gz = p.name.endswith(".gz")
    try:
        if is_gz:
            with gzip.open(p, "rt", encoding="utf-8") as fh:
                lines = fh.read().splitlines()
        else:
            lines = p.read_text(encoding="utf-8").splitlines()
    except Exception:
        return False

    detail_target = str(recommendation_detail or "").strip()
    id_target = str(demand_id or "").strip()
    summary_target = str(summary or "").strip()

    updated = False
    new_lines: list[str] = []
    matched_once = False
    for line in lines:
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except Exception:
            new_lines.append(line)
            continue

        if not isinstance(entry, dict):
            new_lines.append(line)
            continue
        if cycle_id and str(entry.get("cycle_id") or "") != cycle_id:
            new_lines.append(line)
            continue

        entry_matched = False
        recs = entry.get("recommendations")
        if isinstance(recs, list) and (detail_target or id_target):
            for rec in recs:
                if not isinstance(rec, dict):
                    continue
                rec_detail = str(rec.get("detail") or "").strip()
                match_detail = bool(detail_target and rec_detail == detail_target)
                match_id = False
                if id_target:
                    from nanobot.runtime.demand import _make_item
                    item_id = _make_item("reflection", rec_detail, "")["id"]
                    match_id = (item_id == id_target)
                if not matched_once and (match_detail or match_id):
                    if rec.get("status") != "consumed":
                        rec["status"] = "consumed"
                        rec["consumed_at"] = _now()
                        entry_matched = True
                        updated = True
                        matched_once = True

        if not matched_once and not entry_matched and summary_target and entry.get("summary") == summary_target:
            if entry.get("status") != "consumed":
                entry["status"] = "consumed"
                entry["consumed_at"] = _now()
                entry_matched = True
                updated = True

        if entry_matched:
            # If all recommendations in entry are consumed, also mark entry status
            if isinstance(recs, list) and recs and all(isinstance(r, dict) and r.get("status") == "consumed" for r in recs):
                entry["status"] = "consumed"
            new_lines.append(json.dumps(entry, ensure_ascii=False, separators=(",", ":")))
        else:
            new_lines.append(line)

    if updated:
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".reflections.", dir=str(p.parent))
        try:
            payload = "\n".join(new_lines) + "\n"
            if is_gz:
                os.close(fd)
                with gzip.open(temporary, "wt", encoding="utf-8") as fh:
                    fh.write(payload)
            else:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(payload)
                    fh.flush()
                    os.fsync(fh.fileno())
            os.replace(temporary, p)
            return True
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Reflect on completed self-evolving cycles")
    parser.add_argument("--state-root", type=Path, default=None)
    args = parser.parse_args()
    state = args.state_root or Path(os.environ.get("STATE_DIR", "/var/lib/eeepc-agent/self-evolving-agent/state"))
    print("reflector: " + json.dumps(run_reflector(state), ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
