"""Bounded, auditable lesson-to-knowledge curation (#986/#1001).

The curator is deliberately a small adapter around the existing LLM and git
boundaries. It never deletes files, rewrites an index, or advances its
watermark before promotions are durably staged.

Safe write protocol (#1001):
- ``run_curation`` writes promoted facts to ``state/curator/staged/`` only;
  the repo checkout is NEVER touched by the curator process.
- The bridge picks up staged promotions at a safe cycle-start boundary
  (clean main, lock held) via ``_pickup_staged_promotions``.
- Watermark advances only after the staging manifest is durably written;
  a staging failure leaves the watermark unchanged so the lesson is retried.

``migrate_loose_lessons`` is an operator-only utility — run it only while the
bridge timer is stopped; it writes directly into the workspace checkout.
"""
from __future__ import annotations

import asyncio
import gzip
import inspect
import json
import os
import re
import shutil
import tempfile
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Callable, Iterable

from nanobot.observability.llm_telemetry import call_context, record_llm_call, record_llm_prompt
from nanobot.runtime.lesson_v2 import atomic_write_yaml, bounded_load_yaml, find_duplicate
from nanobot.runtime.model_registry import resolve_model

MAX_WRITES_DEFAULT = 3
MAX_LESSONS_DEFAULT = 40
MAX_INPUT_CHARS = 48_000
MAX_OUTPUT_CHARS = 30_000
_DECISIONS = {"promoted", "promoted_unsupported", "duplicate", "unimportant", "rejected"}
_ALLOWED_FACT_PREFIXES = ("memory/facts/", "docs/facts/")

# Staging directory name under state_dir/curator/.
_STAGED_DIR = "staged"

# Evidence resolution constants (#1094).
_LEDGER_TAIL_LINES = 200  # bounded ledger tail read for cycle-id resolution
_MAX_EVIDENCE_SOURCE_BYTES = 32_000  # cap on evidence-source text read for overlap check
_ISSUE_REF_RE = re.compile(
    r"^(?:#\d+|https://github\.com/[^/\s]+/[^/\s#]+/issues/\d+)$",
    re.IGNORECASE,
)
_CYCLE_ID_RE = re.compile(r"^cycle-[A-Za-z0-9][A-Za-z0-9._-]*$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(value, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(name, path)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def _append_jsonl(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(value, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def _entry_id(entry: dict[str, Any]) -> str:
    return str(entry.get("id") or entry.get("lesson_id") or "").strip()


def _entry_key(entry: dict[str, Any]) -> str:
    return _entry_id(entry) or str(entry.get("timestamp") or entry.get("date") or "")


def _yaml_entries(raw: str) -> list[dict[str, Any]]:
    try:
        import yaml
        value = yaml.safe_load(raw)
    except Exception:
        return []
    if isinstance(value, dict):
        value = value.get("lessons") or value.get("errors") or []
    return [x for x in value if isinstance(x, dict)] if isinstance(value, list) else []


def iter_lessons(workspace: Path, state_dir: Path | None = None) -> Iterable[dict[str, Any]]:
    """Yield archived lessons oldest-first, then current live journals and reflections (#1041)."""
    workspace = Path(workspace)
    lessons = workspace / "lessons"
    for path in sorted((lessons / "archive").glob("*.yaml.gz")):
        try:
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                yield from _yaml_entries(fh.read())
        except Exception:
            continue
    for name in ("lessons.yaml", "errors.yaml"):
        path = lessons / name
        try:
            yield from _yaml_entries(path.read_text(encoding="utf-8"))
        except Exception:
            continue

    # Third source: reflector reflections.jsonl (#1041)
    candidates: list[Path] = []
    if state_dir is not None:
        s_path = Path(state_dir)
        candidates.extend([s_path / "reflector" / "reflections.jsonl", s_path / "reflections.jsonl"])
    # Fallback to workspace state if state_dir not passed
    candidates.extend([workspace / "state" / "reflector" / "reflections.jsonl", workspace / "state" / "reflections.jsonl"])

    seen_reflection_paths: set[Path] = set()
    for ref_path in candidates:
        if ref_path in seen_reflection_paths or not ref_path.is_file():
            continue
        seen_reflection_paths.add(ref_path)
        try:
            with ref_path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except Exception:
                        continue
                    if not isinstance(row, dict):
                        continue
                    if isinstance(row.get("status"), str) and row.get("status", "").lower() == "consumed":
                        continue
                    rec_list = row.get("recommendations")
                    if isinstance(rec_list, list) and rec_list:
                        for idx, rec in enumerate(rec_list):
                            if isinstance(rec, dict):
                                if str(rec.get("status") or "").lower() == "consumed":
                                    continue
                                detail = str(
                                    rec.get("detail")
                                    or rec.get("recommendation")
                                    or rec.get("actionable_step")
                                    or rec.get("reason")
                                    or ""
                                ).strip()
                                kind = str(rec.get("kind") or "").strip()
                                evidence = str(rec.get("evidence") or "").strip()
                                actionable = str(
                                    rec.get("actionable_step")
                                    or rec.get("detail")
                                    or rec.get("reason")
                                    or detail
                                ).strip()
                                rec_str = f"[{kind}] {detail}" if kind and detail else detail
                            else:
                                rec_str = str(rec).strip()
                                detail = rec_str
                                kind = ""
                                evidence = ""
                                actionable = rec_str
                            if not rec_str:
                                continue
                            cycle_id = str(row.get("cycle_id") or "")
                            rec_id = f"REFL-{cycle_id[-12:]}-{idx}" if cycle_id else f"REFL-{idx}"
                            yield {
                                "id": rec_id,
                                "date": str(row.get("timestamp") or "")[:10],
                                "timestamp": str(row.get("timestamp") or ""),
                                "cycle_id": cycle_id,
                                "hypothesis": rec_str,
                                "approach": detail or rec_str,
                                "generalized_insight": f"{kind}: {detail}".strip(": ") if kind else rec_str,
                                "reusable_insight": actionable or rec_str,
                                "result": f"Reflection on {cycle_id or 'recent cycles'}: {str(row.get('summary') or '')}" + (f" (evidence: {evidence})" if evidence else ""),
                            }
                    elif row.get("summary"):
                        if str(row.get("status") or "").lower() == "consumed":
                            continue
                        cycle_id = str(row.get("cycle_id") or "")
                        rec_id = f"REFL-{cycle_id[-12:]}" if cycle_id else "REFL"
                        summary_str = str(row.get("summary")).strip()
                        yield {
                            "id": rec_id,
                            "date": str(row.get("timestamp") or "")[:10],
                            "timestamp": str(row.get("timestamp") or ""),
                            "cycle_id": cycle_id,
                            "hypothesis": summary_str,
                            "approach": summary_str,
                            "generalized_insight": summary_str,
                            "reusable_insight": summary_str,
                            "result": f"Reflection on {cycle_id or 'recent cycles'}: {summary_str}",
                        }
        except Exception:
            continue



def _entry_sort_key(entry: dict[str, Any]) -> str:
    """Return a sort key for chronological ordering across lesson sources (#1041)."""
    ts = str(entry.get("timestamp") or "").strip()
    dt = str(entry.get("date") or "").strip()
    key = ts or dt
    entry_id = _entry_id(entry)
    return f"{key} {entry_id}" if key else f"9999-99-99 {entry_id}"


def lessons_after(
    workspace: Path,
    watermark: str,
    *,
    limit: int = MAX_LESSONS_DEFAULT,
    state_dir: Path | None = None,
) -> list[dict[str, Any]]:
    entries = list(iter_lessons(workspace, state_dir=state_dir))
    entries.sort(key=_entry_sort_key)

    found = not bool(watermark)
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in entries:
        key = _entry_key(entry)
        if key and key == watermark:
            found = True
            continue
        if not found or (key and key in seen):
            continue
        if key:
            seen.add(key)
        result.append(entry)
        if len(result) >= max(1, limit):
            break
    return result



def _read_index(workspace: Path) -> str:
    parts = []
    for rel in ("memory/index.md", "docs/index.md"):
        path = workspace / rel
        try:
            parts.append(f"## {rel}\n{path.read_text(encoding='utf-8')[:12_000]}")
        except Exception:
            parts.append(f"## {rel}\n(missing)")
    return "\n\n".join(parts)


def _fact_path(path: str) -> Path | None:
    normalized = str(path or "").replace("\\", "/").strip()
    if ".." in Path(normalized).parts or normalized.startswith("/"):
        return None
    if not normalized.startswith(_ALLOWED_FACT_PREFIXES) or not normalized.endswith(".md"):
        return None
    if normalized.count("/") != 2:
        return None
    return Path(normalized)


# ---------------------------------------------------------------------------
# Evidence resolution (#1094) — deterministic, fail-closed per item
# ---------------------------------------------------------------------------

def _read_ledger_cycle_ids(state_dir: Path, limit: int = _LEDGER_TAIL_LINES) -> set[str]:
    """Return the set of cycle_ids seen in the bounded ledger tail. Fail-open → empty set."""
    ids: set[str] = set()
    ledger_path = Path(state_dir) / "ledger" / "cycles.jsonl"
    try:
        if not ledger_path.is_file():
            return ids
        stat = ledger_path.stat()
        if stat.st_size == 0:
            return ids
        # Keep only the bounded tail in memory; do not parse the full ledger.
        lines = deque(maxlen=max(1, int(limit)))
        with ledger_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                lines.append(line)
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                cid = str(row.get("cycle_id") or "")
                if cid:
                    ids.add(cid)
            except Exception:
                continue
    except Exception:
        pass
    return ids


def _evidence_refs(evidence: Any) -> list[str]:
    """Normalize only supported evidence-reference shapes, without free text."""
    if isinstance(evidence, str):
        return [part.strip() for part in re.split(r"[,;|]", evidence) if part.strip()]
    if isinstance(evidence, (list, tuple)):
        refs: list[str] = []
        for value in evidence:
            if isinstance(value, str) and value.strip():
                refs.append(value.strip())
            elif isinstance(value, dict):
                for key in ("ref", "id", "path", "url"):
                    candidate = value.get(key)
                    if isinstance(candidate, str) and candidate.strip():
                        refs.append(candidate.strip())
                        break
        return refs
    return []


def _resolve_evidence_ref(
    ref: str,
    workspace: Path,
    cycle_ids: set[str],
) -> str | None:
    """Check one evidence ref; unknown or dangling references fail closed."""
    ref = ref.strip()
    if not ref:
        return "empty evidence ref"
    if _ISSUE_REF_RE.fullmatch(ref):
        return None
    if _CYCLE_ID_RE.fullmatch(ref):
        if ref not in cycle_ids:
            return f"cycle_id not in ledger tail: {ref[:60]}"
        return None
    normalized = ref.replace("\\", "/").strip()
    path = Path(normalized)
    if (
        not normalized
        or normalized.startswith("/")
        or PurePosixPath(normalized).is_absolute()
        or PureWindowsPath(normalized).is_absolute()
        or ".." in path.parts
    ):
        return f"unsafe evidence path: {ref[:120]}"
    if not (workspace / path).is_file():
        return f"file not found: {ref[:120]}"
    return None


def _check_evidence_refs(
    evidence: Any,
    workspace: Path,
    state_dir: Path,
) -> str | None:
    """Return a failure reason if evidence is absent/dangling, else None.

    Fail-closed: missing evidence list rejects the item.  Dangling refs also
    reject.  Fail-open only for ledger read errors (cycle_ids will be empty).
    """
    if not evidence:
        return "no evidence refs provided"
    refs = _evidence_refs(evidence)
    if not refs:
        return "no evidence refs provided"
    cycle_ids = _read_ledger_cycle_ids(state_dir)
    for ref in refs:
        reason = _resolve_evidence_ref(ref, workspace, cycle_ids)
        if reason:
            return reason
    return None


# ---------------------------------------------------------------------------
# Semantic support check (#1094) — advisory, recorded on staged entry
# ---------------------------------------------------------------------------

def _read_evidence_source_text(workspace: Path, ref: str, state_dir: Path | None = None) -> str:
    """Read bounded text from an evidence ref for overlap checking.

    For file refs: read the workspace-relative file (bounded).
    For cycle refs: read matching lines from the bounded ledger tail.
    Issue refs and free-text refs return empty string (no local body).
    """
    ref = ref.strip()
    if not ref:
        return ""
    # Cycle ref: read matching lines from ledger tail for source text.
    if _CYCLE_ID_RE.fullmatch(ref):
        if state_dir is None:
            return ""
        ledger_path = Path(state_dir) / "ledger" / "cycles.jsonl"
        try:
            if not ledger_path.is_file():
                return ""
            lines = deque(maxlen=_LEDGER_TAIL_LINES)
            with ledger_path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    lines.append(line)
            matched: list[str] = []
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    if str(row.get("cycle_id") or "") == ref:
                        # Collect summary/result fields as source text.
                        for field in ("result", "summary", "hypothesis", "approach",
                                      "reusable_insight", "generalized_insight"):
                            val = str(row.get(field) or "")
                            if val:
                                matched.append(val)
                except Exception:
                    continue
            return " ".join(matched)[:_MAX_EVIDENCE_SOURCE_BYTES]
        except Exception:
            return ""
    # Issue refs have no local body; the support claim is the bounded quoted line.
    if _ISSUE_REF_RE.fullmatch(ref):
        return ""
    # File ref: read a bounded workspace-relative regular file.
    normalized = ref.replace("\\", "/")
    if (
        not normalized
        or normalized.startswith("/")
        or PurePosixPath(normalized).is_absolute()
        or PureWindowsPath(normalized).is_absolute()
        or ".." in Path(normalized).parts
    ):
        return ""
    try:
        p = workspace / normalized
        if p.is_file() and p.stat().st_size <= _MAX_EVIDENCE_SOURCE_BYTES:
            return p.read_text(encoding="utf-8", errors="replace")[:_MAX_EVIDENCE_SOURCE_BYTES]
    except Exception:
        pass
    return ""


def _fact_has_keyword_overlap(fact_text: str, source_text: str) -> bool:
    """True if fact and source share at least one meaningful keyword."""
    import re as _re

    word_re = _re.compile(r"[a-z]{3,}")
    fact_words = set(word_re.findall(fact_text.lower()))
    source_words = set(word_re.findall(source_text.lower()))
    return bool(fact_words & source_words)


def _parse_output(value: Any) -> list[dict[str, Any]] | None:
    if isinstance(value, dict):
        value = value.get("decisions", value.get("writes", value))
    if isinstance(value, str):
        if len(value) > MAX_OUTPUT_CHARS:
            return None
        try:
            value = json.loads(value)
        except Exception:
            return None
        return _parse_output(value)
    if not isinstance(value, list):
        return None
    clean = []
    for item in value:
        if not isinstance(item, dict):
            return None
        action = str(item.get("action") or item.get("decision") or "").lower().strip()
        if action in {"promote", "create"}:
            action = "create"
        if action in {"update_fact", "update"}:
            action = "update"
        if action in {"duplicate", "unimportant", "rejected", "delete", "remove"}:
            # Delete is represented as an auditable rejection, never an operation.
            if action in {"delete", "remove"}:
                action = "rejected"
        elif action not in {"create", "update"}:
            return None
        item = dict(item)
        item["action"] = action
        # Preserve support_claim from LLM output for semantic support recording.
        if "support_claim" in item:
            item["support_claim"] = str(item["support_claim"])[:500]
        clean.append(item)
    return clean


def _messages(lessons: list[dict[str, Any]], index: str, facts: str) -> list[dict[str, str]]:
    body = json.dumps(lessons, ensure_ascii=False, separators=(",", ":"))
    body = body[:MAX_INPUT_CHARS]
    system = (
        "You are the eeebot knowledge curator. Return ONLY a JSON array. "
        "Each item must be one of: "
        "{action:create,path,title,content,index_line,lesson_id,reason,support_claim}, "
        "{action:update,path,content,lesson_id,reason,support_claim}, or "
        "{action:duplicate|unimportant,lesson_id,reason}. "
        "Create/update paths must be memory/facts/*.md or docs/facts/*.md. Never delete or rewrite an index. "
        "At most three create/update items; every item needs a one-line reason. "
        "For create/update items, include support_claim: a brief quote or reference from the lesson "
        "evidence that directly supports the fact being written."
    )
    user = f"NEW LESSONS:\n{body}\n\nKB INDEXES:\n{index[:24000]}\n\nTOUCHED FACT BODIES:\n{facts[:12000]}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _touched_facts(workspace: Path, decisions: list[dict[str, Any]]) -> str:
    out = []
    for d in decisions:
        path = _fact_path(str(d.get("path") or ""))
        if path and (workspace / path).is_file():
            try:
                out.append(f"## {path}\n{(workspace / path).read_text(encoding='utf-8')[:8000]}")
            except Exception:
                pass
    return "\n\n".join(out)


def _default_llm(messages: list[dict[str, str]], model: str) -> Any:
    """Production OpenAI-compatible call; tests should inject ``llm``."""
    from openai import OpenAI
    base_url = os.environ.get("LITELLM_BASE_URL", "").strip()
    api_key = os.environ.get("LITELLM_API_KEY", "").strip()
    if not base_url or not api_key:
        # Distinct, actionable error: without this the missing-credentials
        # case surfaced as "malformed curator output" (#986 first run) and
        # was indistinguishable from a bad model response.
        raise RuntimeError(
            "litellm credentials not configured (LITELLM_BASE_URL/LITELLM_API_KEY) — "
            "check the unit's EnvironmentFile chain"
        )
    client = OpenAI(base_url=base_url, api_key=api_key, timeout=120)
    started = __import__("time").monotonic()
    response = client.chat.completions.create(model=model, messages=messages, max_tokens=1200, temperature=0.2)
    usage_obj = getattr(response, "usage", None)
    usage = {k: int(getattr(usage_obj, k, 0) or 0) for k in ("prompt_tokens", "completion_tokens", "total_tokens")}
    choice = response.choices[0]
    content = getattr(getattr(choice, "message", None), "content", "") or ""
    with call_context(None, "curator"):
        record_llm_call(model=model, duration_ms=(__import__("time").monotonic() - started) * 1000,
                        usage=usage, finish_reason=getattr(choice, "finish_reason", ""), retries=0)
        record_llm_prompt(messages=messages, content=content, reasoning_content=None,
                          finish_reason=getattr(choice, "finish_reason", ""), model=model,
                          prompt_tokens=usage["prompt_tokens"], completion_tokens=usage["completion_tokens"])
    return content


def _write_decision(state: Path, lesson_id: str, decision: str, reason: str, target: str = "") -> None:
    _append_jsonl(state / "curator" / "decisions.jsonl", {
        "timestamp": _now(), "lesson_id": lesson_id, "decision": decision,
        "reason": str(reason or "")[:300], "target_file": target,
    })


def _stage_promotions(
    state_dir: Path, items: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Write staged promotion payloads under state_dir/curator/staged/ (#1001).

    Each item is a dict with keys: path, content, action, index_line (optional).
    Returns the list of manifest entries actually written.
    Atomic per-file: write to temp, fsync, rename. Fails loudly on any error
    so the caller can keep the watermark unmoved.
    """
    staged_dir = state_dir / "curator" / _STAGED_DIR
    staged_dir.mkdir(parents=True, exist_ok=True)
    manifest_entries: list[dict[str, Any]] = []
    for item in items:
        rel = str(item["path"]).replace("\\", "/")
        content = str(item["content"]).rstrip() + "\n"
        action = str(item["action"])
        index_line = str(item.get("index_line") or "")
        # Payload file: flatten the relative path to a safe filename.
        slug = rel.replace("/", "__").replace("\\", "__")
        payload_path = staged_dir / slug
        # Atomic write: temp → fsync → rename.
        fd, tmp = tempfile.mkstemp(prefix=".stg.", dir=str(staged_dir))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(content)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, payload_path)
        finally:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
        manifest_entries.append({
            "path": rel,
            "action": action,
            "payload_file": slug,
            "index_line": index_line,
            "index_rel": str(item.get("index_rel") or ""),
            # #1094: evidence verification fields (advisory)
            "evidence": item.get("evidence", []),
            "support_claim": str(item.get("support_claim") or "")[:500],
            "verification_status": "unsupported" if item.get("overlap_flag", False) else "supported",
            "overlap_flag": bool(item.get("overlap_flag", False)),
        })
    # Write manifest atomically.
    existing_manifest = staged_dir / "manifest.json"
    prev: list[dict[str, Any]] = []
    try:
        prev = json.loads(existing_manifest.read_text(encoding="utf-8"))
        if not isinstance(prev, list):
            prev = []
    except Exception:
        prev = []
    # Merge: replace entries with the same path, append new ones.
    path_to_idx = {e["path"]: i for i, e in enumerate(prev)}
    for entry in manifest_entries:
        if entry["path"] in path_to_idx:
            prev[path_to_idx[entry["path"]]] = entry
        else:
            prev.append(entry)
    _atomic_json(existing_manifest, prev)
    return manifest_entries


def load_staged_manifest(state_dir: Path) -> list[dict[str, Any]]:
    """Return the current staging manifest, or [] if none. (#1001)"""
    manifest = state_dir / "curator" / _STAGED_DIR / "manifest.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def clear_staged_manifest(state_dir: Path, *, retain_overlap_flag: bool = False) -> None:
    """Remove staging manifest and payload files after a successful pickup. (#1001)

    With ``retain_overlap_flag=True`` (#1094): entries with ``overlap_flag=True``
    (unsupported entries skipped by pickup) are retained in staging for audit;
    only successfully picked-up entries are removed.  This prevents unsupported
    entries from being silently lost by the clear that follows a normal pickup.
    """
    staged_dir = state_dir / "curator" / _STAGED_DIR
    manifest = staged_dir / "manifest.json"
    try:
        entries = json.loads(manifest.read_text(encoding="utf-8"))
    except Exception:
        entries = []
    if not isinstance(entries, list):
        entries = []
    if retain_overlap_flag:
        # Separate unsupported entries (to be kept) from supported (to be removed).
        to_keep = [e for e in entries if e.get("overlap_flag")]
        to_remove = [e for e in entries if not e.get("overlap_flag")]
        for entry in to_remove:
            slug = str(entry.get("payload_file") or "")
            if slug:
                try:
                    (staged_dir / slug).unlink(missing_ok=True)
                except Exception:
                    pass
        if to_keep:
            # Rewrite manifest with only the retained (unsupported) entries.
            _atomic_json(manifest, to_keep)
        else:
            try:
                manifest.unlink(missing_ok=True)
            except Exception:
                pass
    else:
        for entry in entries:
            slug = str(entry.get("payload_file") or "")
            if slug:
                try:
                    (staged_dir / slug).unlink(missing_ok=True)
                except Exception:
                    pass
        try:
            manifest.unlink(missing_ok=True)
        except Exception:
            pass


def _collect_stage_items(
    workspace: Path,
    state_dir: Path,
    decisions: list[dict[str, Any]],
    max_writes: int,
    entries: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Validate decisions, record non-write outcomes, return items to stage + write count. (#1001)

    Does NOT touch workspace — only reads it to check create/update preconditions.
    Adds two-tier verification (#1094):
    - Tier 1: evidence refs must resolve (fail-closed: dangling/absent → rejected).
      Evidence is taken from the LLM decision's 'evidence' field first; if absent,
      lifted from the matching source lesson's 'cycle_id'/'evidence' field.
      If no evidence can be found in either, the item is rejected.
    - Tier 2: keyword overlap between support_claim and the evidence SOURCE TEXT
      (file content or ledger lines for cycle refs) is advisory: zero overlap
      → staged with overlap_flag=True; pickup will skip these entries.
    """
    items: list[dict[str, Any]] = []
    writes = 0
    # Build lookup of source lessons by id for Tier-1 evidence lifting.
    _entry_by_id: dict[str, dict[str, Any]] = {}
    for e in (entries or []):
        eid = _entry_id(e)
        if eid:
            _entry_by_id[eid] = e
    for d in decisions:
        action = d["action"]
        lesson_id = str(d.get("lesson_id") or "")
        reason = str(d.get("reason") or "")
        if action in {"duplicate", "unimportant", "rejected"}:
            _write_decision(state_dir, lesson_id, action, reason)
            continue
        if writes >= max_writes:
            break
        rel = _fact_path(str(d.get("path") or ""))
        content = str(d.get("content") or "").strip()
        if rel is None or not content or len(content) > 12_000:
            _write_decision(state_dir, lesson_id, "rejected", "invalid bounded fact path/content", str(d.get("path") or ""))
            continue
        # Check preconditions against workspace (read-only check).
        exists = (workspace / rel).exists()
        if action == "update" and not exists:
            _write_decision(state_dir, lesson_id, "rejected", "update target does not exist", str(rel))
            continue
        if action == "create" and exists:
            _write_decision(state_dir, lesson_id, "duplicate", "fact already exists", str(rel))
            continue

        # --- Tier 1: evidence resolution (fail-closed) ---
        # Evidence comes from the LLM decision's 'evidence' field first, or is
        # lifted from the source lesson's cycle_id/evidence field.
        # If no evidence can be found in either source, reject fail-closed.
        evidence = d.get("evidence") or []
        if not evidence:
            # Try to lift evidence from the source lesson.
            src_lesson = _entry_by_id.get(lesson_id)
            if src_lesson:
                src_ev = src_lesson.get("evidence") or []
                src_cycle = str(src_lesson.get("cycle_id") or "").strip()
                if src_ev:
                    evidence = src_ev
                elif src_cycle:
                    evidence = [src_cycle]
        ev_fail: str | None = _check_evidence_refs(evidence, workspace, state_dir)
        if ev_fail is not None:
            _write_decision(
                state_dir, lesson_id, "rejected",
                f"evidence ref rejected: {ev_fail}",
                str(rel if rel else d.get("path") or ""),
            )
            continue

        # --- Tier 2: keyword overlap (advisory) ---
        # The claim is required and is compared with the resolved evidence
        # source, not trusted as evidence by itself. Issue refs have no local
        # body, so the claim is the bounded quoted evidence line.
        support_claim = str(d.get("support_claim") or "").strip()
        if not support_claim:
            _write_decision(state_dir, lesson_id, "rejected", "missing support_claim", str(rel))
            continue
        ev_refs = _evidence_refs(evidence)
        source_parts: list[str] = []
        for ev_ref in ev_refs:
            source_text = _read_evidence_source_text(workspace, ev_ref, state_dir)
            if source_text:
                source_parts.append(source_text)
            elif _ISSUE_REF_RE.fullmatch(ev_ref):
                source_parts.append(support_claim)
        source_text = " ".join(source_parts)
        overlap_flag = not (
            _fact_has_keyword_overlap(content, source_text)
            and _fact_has_keyword_overlap(support_claim, source_text)
        )

        rel_str = str(rel).replace("\\", "/")
        index_rel = "memory/index.md" if rel.parts[0] == "memory" else "docs/index.md"
        index_line = str(
            d.get("index_line")
            or f"- [{d.get('title') or rel.stem}]({rel.as_posix()})"
        ) if action == "create" else ""
        items.append({
            "path": rel_str,
            "action": action,
            "content": content,
            "index_line": index_line,
            "index_rel": index_rel if action == "create" else "",
            "lesson_id": lesson_id,
            "reason": reason,
            "evidence": evidence,
            "support_claim": support_claim,
            "verification_status": "unsupported" if overlap_flag else "supported",
            "overlap_flag": overlap_flag,
        })
        writes += 1
        decision_label = "promoted_unsupported" if overlap_flag else "promoted"
        _write_decision(state_dir, lesson_id, decision_label, reason, rel_str)
    return items, writes


def promote_reflector_recommendations_to_v2(
    workspace: Path, state_dir: Path, *, max_items: int = 2,
) -> int:
    """Promote bounded reflector error/approach deltas into v2 lessons."""
    state_dir = Path(state_dir)
    candidates = [
        state_dir / "reflector" / "reflections.jsonl",
        state_dir / "state" / "reflector" / "reflections.jsonl",
        state_dir / "reflections.jsonl",
    ]
    path = next((p for p in candidates if p.is_file()), None)
    if not path:
        # Fallback glob for nested state paths
        matches = list(state_dir.glob("**/reflector/reflections.jsonl"))
        if matches:
            path = matches[0]
        else:
            return 0
    try:
        stat = path.stat()
        if stat.st_size > 512 * 1024 or time.time() - stat.st_mtime > 90 * 86400:
            return 0
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()[-50:] if line.strip()]
    except Exception:
        return 0
    target = Path(workspace) / "lessons" / "lessons.yaml"
    existing = bounded_load_yaml(target)
    if not existing and target.exists():
        try:
            import yaml

            raw = yaml.safe_load(target.read_text(encoding="utf-8"))
            existing = raw.get("lessons", []) if isinstance(raw, dict) else raw if isinstance(raw, list) else []
        except Exception:
            existing = []
    count = 0
    for row in reversed(rows):
        if count >= max_items or not isinstance(row, dict):
            break
        for item in row.get("recommendations", []):
            if count >= max_items or not isinstance(item, dict) or item.get("kind") not in {"error_pattern", "approach_hint"}:
                continue
            detail = str(item.get("detail") or "").strip()
            if not detail:
                continue
            card = {"schema_version": 2, "id": f"LESS-REF-{row.get('cycle_id', 'unknown')[-12:]}",
                    "title": detail[:200], "problem": detail[:400],
                    "solution": f"Apply the reflected {item['kind'].replace('_', ' ')}.",
                    "tags": ["reflector"], "severity": "medium", "seen_count": 1,
                    "first_seen": datetime.now(timezone.utc).date().isoformat(),
                    "last_seen": datetime.now(timezone.utc).date().isoformat(),
                    "evidence": [str(row.get("cycle_id") or "reflector")]}
            duplicate = find_duplicate(card["problem"], existing)
            if duplicate is not None:
                duplicate["seen_count"] = int(duplicate.get("seen_count") or 1) + 1
                duplicate["last_seen"] = card["last_seen"]
            else:
                existing.insert(0, card)
            count += 1
    if count:
        atomic_write_yaml(target, {"lessons": existing})
    return count


def _parse_output_with_diag(
    raw: Any,
) -> tuple[list[dict[str, Any]] | None, dict[str, Any]]:
    """Like _parse_output but also returns redacted parse-failure diagnostics (#1094/#986).

    Returns (decisions, diag) where diag is always a dict.  On success, diag
    contains only ``output_length``; on failure it additionally has
    ``parse_failure_category`` (a short classifier string) so future incidents
    are distinguishable.  Never retains the raw response body.
    """
    finish_reason = None
    payload = raw
    if isinstance(raw, dict) and ("content" in raw or "output" in raw):
        payload = raw.get("content", raw.get("output"))
        finish_reason = raw.get("finish_reason")
    output_length = len(str(payload)) if payload is not None else 0
    diag: dict[str, Any] = {"output_length": min(output_length, MAX_OUTPUT_CHARS + 1)}
    if isinstance(finish_reason, str) and finish_reason.strip():
        diag["finish_reason"] = finish_reason.strip()[:80]

    if payload is None:
        diag["parse_failure_category"] = "null_output"
        return None, diag

    try:
        raw_str = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    except Exception:
        diag["parse_failure_category"] = "invalid_schema"
        return None, diag

    if len(raw_str) > MAX_OUTPUT_CHARS:
        diag["parse_failure_category"] = "oversized_output"
        return None, diag

    # Try JSON parse
    if isinstance(payload, str):
        try:
            parsed_json = json.loads(payload)
        except Exception:
            diag["parse_failure_category"] = "non_json_output"
            return None, diag
    else:
        parsed_json = payload

    decisions = _parse_output(parsed_json)
    if decisions is None:
        diag["parse_failure_category"] = "invalid_schema"
        return None, diag

    return decisions, diag


def run_curation(
    workspace: Path,
    state_dir: Path,
    *,
    llm: Callable[[list[dict[str, str]], str], Any] | None = None,
    gate: Callable[[Path, list[str]], bool] | None = None,  # kept for API compat; ignored (#1001)
    max_writes: int = MAX_WRITES_DEFAULT,
    max_lessons: int = MAX_LESSONS_DEFAULT,
) -> dict[str, Any]:
    """Run one fail-open curator pass. Writes staged dir only — never the workspace. (#1001)"""
    workspace, state_dir = Path(workspace), Path(state_dir)
    promote_reflector_recommendations_to_v2(workspace, state_dir, max_items=2)
    malformed_diagnostic_written = False
    wm_path = state_dir / "curator" / "watermark.json"
    old = _safe_json(wm_path, {})
    watermark = str(old.get("last_processed") or old.get("last_processed_id") or "") if isinstance(old, dict) else ""
    entries = lessons_after(workspace, watermark, limit=max_lessons, state_dir=state_dir)
    if not entries:
        return {"ok": True, "processed": 0, "writes": 0}
    try:
        model = resolve_model("curator", strip_openai=True)
        messages = _messages(entries, _read_index(workspace), "")
        result = llm(messages, model) if llm else _default_llm(messages, model)
        if inspect.isawaitable(result):
            result = asyncio.run(result)
        decisions, diag = _parse_output_with_diag(result)
        if decisions is None:
            _append_jsonl(state_dir / "curator" / "errors.jsonl", {
                "timestamp": _now(),
                "error": "malformed curator output",
                **{k: v for k, v in diag.items()},
            })
            malformed_diagnostic_written = True
            raise ValueError("malformed curator output")
        # Fetch only bodies named by the draft, then re-run the same bounded
        # request with those bodies. This keeps the full KB out of both calls.
        facts = _touched_facts(workspace, decisions)
        if facts:
            caller = llm or _default_llm
            result = caller(_messages(entries, _read_index(workspace), facts), model)
            if inspect.isawaitable(result):
                result = asyncio.run(result)
            decisions, diag = _parse_output_with_diag(result)
            if decisions is None:
                _append_jsonl(state_dir / "curator" / "errors.jsonl", {
                    "timestamp": _now(),
                    "error": "malformed curator output",
                    **{k: v for k, v in diag.items()},
                })
                malformed_diagnostic_written = True
                raise ValueError("malformed curator output")
        items, writes = _collect_stage_items(workspace, state_dir, decisions, max(0, int(max_writes)), entries=entries)
        staged: list[dict[str, Any]] = []
        if items:
            # _stage_promotions raises on failure; watermark stays unmoved.
            staged = _stage_promotions(state_dir, items)
        # Watermark advances only after staging is durable. (#1001 B)
        last = _entry_key(entries[-1])
        if writes >= max(0, int(max_writes)) and max_writes > 0:
            consumed = 0
            for decision in decisions:
                if decision.get("action") in {"create", "update"}:
                    consumed += 1
                    if consumed >= max_writes:
                        wanted = str(decision.get("lesson_id") or "")
                        if wanted:
                            last = wanted
                        break
        _atomic_json(wm_path, {"last_processed": last, "last_processed_id": last, "timestamp": _now()})
        staged_paths = [e["path"] for e in staged]
        unsupported = sum(1 for e in staged if e.get("overlap_flag"))
        result_dict: dict[str, Any] = {
            "ok": True, "processed": len(entries), "writes": writes, "staged": staged_paths,
        }
        if unsupported:
            result_dict["unsupported"] = unsupported
        return result_dict
    except Exception as exc:
        if not malformed_diagnostic_written:
            _append_jsonl(state_dir / "curator" / "errors.jsonl", {
                "timestamp": _now(),
                "error": str(exc)[:500],
            })
        return {"ok": False, "processed": 0, "writes": 0, "error": str(exc)[:500]}


def migrate_loose_lessons(workspace: Path, state_dir: Path | None = None) -> dict[str, Any]:
    """Deterministically consolidate duplicate loose notes and archive originals."""
    workspace = Path(workspace)
    loose = sorted((workspace / "lessons").glob("*.md"))
    archive = workspace / "lessons" / "archive" / "loose"
    groups: dict[str, list[Path]] = {}
    for path in loose:
        try:
            text = path.read_text(encoding="utf-8").strip()
        except Exception:
            continue
        key = re.sub(r"\W+", " ", text.lower()).strip()
        groups.setdefault(key, []).append(path)
    created = 0
    for key, paths in groups.items():
        if not key:
            continue
        slug = re.sub(r"[^a-z0-9]+", "-", paths[0].stem.lower()).strip("-")[:60] or "lesson"
        target = workspace / "memory" / "facts" / f"{slug}.md"
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f"# {paths[0].stem}\n\n{paths[0].read_text(encoding='utf-8').strip()}\n", encoding="utf-8")
            with (workspace / "memory" / "index.md").open("a", encoding="utf-8") as fh:
                fh.write(f"\n- [{paths[0].stem}](memory/facts/{slug}.md)\n")
            created += 1
            try:
                from nanobot.runtime.lessons_rotation import rotate_index_file
                rotate_index_file(workspace / "memory" / "index.md")
            except Exception:
                pass
        for path in paths:
            archive.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(archive / path.name))
    return {"migrated": len(loose), "facts_created": created}


def main() -> int:
    workspace = Path(os.environ.get("TARGET_WORKSPACE", "/var/lib/eeepc-agent/self-evolving-agent/eeebot-self-evolving"))
    state = Path(os.environ.get("STATE_DIR", "/var/lib/eeepc-agent/self-evolving-agent/state"))
    print(json.dumps(run_curation(workspace, state), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
