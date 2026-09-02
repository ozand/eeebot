"""Bounded, auditable lesson-to-knowledge curation (#986/#1001).

The curator is deliberately a small adapter around the existing LLM and git
boundaries. It never deletes files, rewrites an index, or advances its
watermark before promotions are durably staged.

Safe write protocol (#1001, #1209):
- ``run_curation`` writes promoted facts to ``state/curator/staged/`` only;
  the repo checkout is NEVER touched by the curator process. The reflector
  v2 mint (``promote_reflector_recommendations_to_v2``) stages its cards the
  same way — it used to write ``lessons/lessons.yaml`` straight into the
  working tree, where the next cycle's ``git reset --hard`` erased it within
  minutes (#1209: measured lifetime 2 min 45 s on the host).
- The bridge picks up staged promotions at a safe cycle-start boundary
  (clean main, lock held) via ``_pickup_staged_promotions``, commits them on
  ``main`` and PUSHES to ``origin/main`` before the cycle branch is cut from
  ``origin/main``. A commit that stays on local ``main`` only is orphaned by
  the integration step's ``checkout -B main <origin base>`` (#986: six of
  seven pickup commits dangling), so the push is what makes it durable.
- Watermark advances only after the staging manifest is durably written;
  a staging failure leaves the watermark unchanged so the lesson is retried.
- ``decisions.jsonl`` records ``staged`` when the curator stages an item and
  ``promoted`` only when the bridge has pushed it; a pickup whose push fails
  records ``pickup_deferred`` and keeps the item in staging. A write that was
  discarded is never on record as a success.

``migrate_loose_lessons`` is an operator-triggered utility that stages through
the same protocol (#1214): it writes the staging manifest only, and the bridge's
cycle-start pickup commits and pushes the result. Leave the bridge timer
RUNNING when invoking it — the pickup is what applies the migration, so
stopping the timer leaves the staged batch unapplied. It supersedes the earlier
direct-checkout write, which the next cycle's ``git reset --hard`` discarded.
"""
from __future__ import annotations

import asyncio
import gzip
import inspect
import json
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Callable, Iterable

from nanobot.observability.llm_telemetry import call_context, record_llm_call, record_llm_prompt
from nanobot.runtime.lesson_v2 import (
    atomic_write_yaml,
    bounded_load_yaml,
    fill_related_links,
    find_duplicate,
    inline_related_slugs,
    keyword_set,
    related_hint,
    set_jaccard,
    validate_lesson_for_mint,
)
from nanobot.runtime.model_registry import resolve_model

MAX_WRITES_DEFAULT = 3
MAX_LESSONS_DEFAULT = 40
MAX_INPUT_CHARS = 48_000
MAX_OUTPUT_CHARS = 30_000
_DECISIONS = {
    "staged", "staged_unsupported", "promoted", "pickup_deferred",
    "duplicate", "unimportant", "rejected",
}
_ALLOWED_FACT_PREFIXES = ("memory/facts/", "docs/facts/")

# Staging directory name under state_dir/curator/.
_STAGED_DIR = "staged"
# Reflector v2 lesson cards travel through the same staging manifest (#1209):
# one entry of this kind, targeting the lessons store, whose payload is a JSON
# list of cards the bridge merges into the checkout at pickup time.
LESSONS_REL = "lessons/lessons.yaml"
LESSONS_KIND = "lessons_v2"
_LESSONS_PAYLOAD_SLUG = "lessons__lessons.yaml.cards.json"

# Evidence resolution constants (#1094).
_LEDGER_TAIL_LINES = 200  # bounded ledger tail read for cycle-id resolution
_MAX_EVIDENCE_SOURCE_BYTES = 32_000  # cap on evidence-source text read for overlap check
_MAX_LEDGER_TAIL_BYTES = 256_000
_ACTION_INDEX_SEGMENTS = 7  # fixed file-open budget for the durable fallback
_MAX_ACTION_INDEX_BYTES = 256_000
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
            if True:  # #1178: rotated archives first, then the live journal
                for line in _reflection_lines(ref_path):
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

def _reflection_archives(live: Path, newest: int = 2) -> list[Path]:
    """The newest rotated journals next to ``live`` (``archive/reflections-*.jsonl.gz``,
    #1178), oldest first."""
    archive_dir = live.parent / "archive"
    if not archive_dir.is_dir():
        return []
    names = sorted(p for p in archive_dir.glob("reflections-*.jsonl.gz") if p.is_file())
    return names[-newest:] if newest else []


def _reflection_lines(live: Path):
    """Lines of the newest rotated journals then the live journal (#1178)."""
    for path in [*_reflection_archives(live), live]:
        opener = gzip.open if path.name.endswith(".gz") else open
        try:
            with opener(path, "rt", encoding="utf-8") as fh:
                yield from fh
        except OSError:
            continue


def _bounded_tail_lines(path: Path, max_lines: int) -> list[str]:
    """Read only a bounded byte/line tail from a JSONL file."""
    with path.open("rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(max(0, size - _MAX_LEDGER_TAIL_BYTES))
        data = fh.read(_MAX_LEDGER_TAIL_BYTES)
    if size > _MAX_LEDGER_TAIL_BYTES:
        parts = data.splitlines(keepends=True)
        data = b"".join(parts[1:]) if parts else b""
    return data.decode("utf-8", errors="replace").splitlines()[-max(1, int(max_lines)):]


def _read_ledger_cycle_ids(state_dir: Path, limit: int = _LEDGER_TAIL_LINES) -> set[str]:
    """Return cycle IDs from a bounded ledger tail. Fail-open → empty set."""
    ids: set[str] = set()
    ledger_path = Path(state_dir) / "ledger" / "cycles.jsonl"
    try:
        if not ledger_path.is_file() or ledger_path.stat().st_size == 0:
            return ids
        for line in _bounded_tail_lines(ledger_path, limit):
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


def _read_action_index_cycle_text(state_dir: Path, cycle_id: str) -> str | None:
    """Resolve a cycle from a bounded set of newest action-index segments.

    The action index is the durable per-cycle source built by #1005.  Select
    files before opening any of them so archive growth cannot turn this into an
    archive-wide scan.  A malformed or oversized segment is simply skipped.
    """
    index_dir = Path(state_dir) / "action_index"
    try:
        paths = sorted(
            {*index_dir.glob("*.jsonl"), *index_dir.glob("*.jsonl.gz")},
            key=lambda path: path.name,
            reverse=True,
        )[:_ACTION_INDEX_SEGMENTS]
    except OSError:
        return None
    for path in paths:
        try:
            if path.stat().st_size > _MAX_ACTION_INDEX_BYTES:
                continue
            opener = gzip.open if path.name.endswith(".gz") else open
            with opener(path, "rt", encoding="utf-8") as fh:  # type: ignore[call-arg]
                for line in fh:
                    try:
                        row = json.loads(line)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
                    if not isinstance(row, dict) or str(row.get("cycle_id") or "") != cycle_id:
                        continue
                    parts = []
                    for field in ("outcome", "task_title", "actions"):
                        value = row.get(field)
                        if value:
                            parts.append(json.dumps(value, ensure_ascii=False) if isinstance(value, list) else str(value))
                    return " ".join(parts)[:_MAX_EVIDENCE_SOURCE_BYTES]
        except (OSError, EOFError, gzip.BadGzipFile):
            continue
    return None


def _resolve_evidence_ref(
    ref: str,
    workspace: Path,
    cycle_ids: set[str],
    state_dir: Path,
) -> tuple[str | None, str | None]:
    """Check one evidence ref; unknown or dangling references fail closed."""
    ref = ref.strip()
    if not ref:
        return "empty evidence ref", None
    if _ISSUE_REF_RE.fullmatch(ref):
        return None, "issue"
    if _CYCLE_ID_RE.fullmatch(ref):
        if ref in cycle_ids:
            return None, "ledger_tail"
        if _read_action_index_cycle_text(state_dir, ref) is not None:
            return None, "action_index"
        return f"cycle_id not in ledger tail: {ref[:60]}", None
    normalized = ref.replace("\\", "/").strip()
    path = Path(normalized)
    if (
        not normalized
        or normalized.startswith("/")
        or PurePosixPath(normalized).is_absolute()
        or PureWindowsPath(normalized).is_absolute()
        or ".." in path.parts
    ):
        return f"unsafe evidence path: {ref[:120]}", None
    if not (workspace / path).is_file():
        return f"file not found: {ref[:120]}", None
    return None, "file"


def _check_evidence_refs(
    evidence: Any,
    workspace: Path,
    state_dir: Path,
) -> tuple[str | None, str | None]:
    """Return a failure reason and resolving source, preserving fail-closed."""
    if not evidence:
        return "no evidence refs provided", None
    refs = _evidence_refs(evidence)
    if not refs:
        return "no evidence refs provided", None
    cycle_ids = _read_ledger_cycle_ids(state_dir)
    for ref in refs:
        reason, source = _resolve_evidence_ref(ref, workspace, cycle_ids, state_dir)
        if reason:
            return reason, None
    return None, source




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
            matched: list[str] = []
            for line in _bounded_tail_lines(ledger_path, _LEDGER_TAIL_LINES):
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
            if matched:
                return " ".join(matched)[:_MAX_EVIDENCE_SOURCE_BYTES]
            indexed = _read_action_index_cycle_text(Path(state_dir), ref)
            return indexed or ""
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
        entry = {
            "path": rel,
            "action": action,
            "payload_file": slug,
            # #1209: the pickup records the durable outcome against this id.
            "lesson_id": str(item.get("lesson_id") or ""),
            "index_line": index_line,
            "related": related_hint(item),
            "unknown_related": list(item.get("unknown_related") or []),
            "index_rel": str(item.get("index_rel") or ""),
            # #1094: evidence verification fields (advisory)
            "evidence": item.get("evidence", []),
            "support_claim": str(item.get("support_claim") or "")[:500],
            "verification_status": "unsupported" if item.get("overlap_flag", False) else "supported",
            "overlap_flag": bool(item.get("overlap_flag", False)),
        }
        if item.get("kind"):
            entry["kind"] = str(item["kind"])
        if item.get("source_path"):
            entry["source_path"] = str(item["source_path"]).replace("\\", "/")
        manifest_entries.append(entry)
    _append_manifest_entries(staged_dir, manifest_entries)
    return manifest_entries


def _append_manifest_entries(staged_dir: Path, manifest_entries: list[dict[str, Any]]) -> None:
    """Merge *manifest_entries* into ``manifest.json`` atomically: an entry with
    the same ``path`` replaces the previous one, new paths are appended."""
    existing_manifest = staged_dir / "manifest.json"
    prev: list[dict[str, Any]] = []
    try:
        prev = json.loads(existing_manifest.read_text(encoding="utf-8"))
        if not isinstance(prev, list):
            prev = []
    except Exception:
        prev = []
    path_to_idx = {e["path"]: i for i, e in enumerate(prev)}
    for entry in manifest_entries:
        if entry["path"] in path_to_idx:
            prev[path_to_idx[entry["path"]]] = entry
        else:
            prev.append(entry)
    _atomic_json(existing_manifest, prev)


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
        to_keep = [
            e for e in entries
            if e.get("overlap_flag") or e.get("verification_status") == "unsupported"
        ]
        to_remove = [
            e for e in entries
            if not (e.get("overlap_flag") or e.get("verification_status") == "unsupported")
        ]
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
        ev_fail, ev_source = _check_evidence_refs(evidence, workspace, state_dir)
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
        raw_related = d.get("related") or inline_related_slugs(content)
        related = ", ".join(
            s for s in (raw_related if isinstance(raw_related, list) else [raw_related])
            if isinstance(s, str) and s.strip()
        )[:500]
        index_line = str(
            d.get("index_line")
            or f"- [{d.get('title') or rel.stem}]({rel.as_posix()})"
            + (f" — related: {related}" if related else "")
        ) if action == "create" else ""
        items.append({
            "path": rel_str,
            "action": action,
            "content": content,
            "index_line": index_line,
            "related": raw_related if isinstance(raw_related, list) else ([raw_related] if raw_related else []),
            "unknown_related": [],
            "tags": d.get("tags") or d.get("glossary_tags") or [],
            "demand_lineage": d.get("demand_lineage") or d.get("demand_id") or d.get("delta_evidence") or "",
            "index_rel": index_rel if action == "create" else "",
            "lesson_id": lesson_id,
            "reason": reason,
            "evidence": evidence,
            "support_claim": support_claim,
            "verification_status": "unsupported" if overlap_flag else "supported",
            "overlap_flag": overlap_flag,
        })
        writes += 1
        # #1209: staging is not promotion. ``promoted`` is written by the bridge
        # pickup once the commit is on origin/main; until then the record says
        # what actually happened — the item was staged.
        decision_label = "staged_unsupported" if overlap_flag else "staged"
        decision_reason = reason
        if ev_source and ev_source not in {"ledger_tail", "file", "issue"}:
            decision_reason = f"{reason} (evidence source: {ev_source})"
        _write_decision(state_dir, lesson_id, decision_label, decision_reason, rel_str)

    # Build a bounded in-memory graph for the facts staged in this pass. This
    # is deliberately mechanical: no LLM call, no archive scan, and unknown
    # inline/explicit targets are retained and reported in the manifest.
    if items:
        linked, unknown = fill_related_links(items)
        for item in linked:
            item["unknown_related"] = sorted(unknown)
            hint = related_hint(item)
            if hint and item.get("index_line") and "related:" not in str(item["index_line"]):
                item["index_line"] = f'{item["index_line"].rstrip()} — {hint}'
        items = linked
    return items, writes


# Newest reflector rows considered for promotion. The tail read that finds
# them is bounded by _MAX_LEDGER_TAIL_BYTES, which is a window into the end of
# the file, not a limit on how large the log may grow (#1183).
_REFLECTOR_MAX_AGE_SECONDS = 90 * 86400

# ─── #1171: recurrence earns a card ──────────────────────────────────────────
#
# The reflector journal receives ~120 rows/day carrying ~70 promotable
# `approach_hint` items (live store 2026-08-27..09-02: 502 items in 738 rows).
# Almost all are task-specific one-offs — 448 of the 502 are lexical
# singletons at the threshold below — and those already reach the proposer
# through `build_reflection_hints`. The ~21 that recur across independent
# cycles are the general lessons ("configure a LiteLLM fallback for the model
# group", "commit early in the turn budget", "emit the skipped verdict as soon
# as inspection shows the feature exists"). So a recommendation earns a card
# on RECURRENCE, folds into an existing card when one already says the same
# thing, and otherwise waits in a bounded pool. Minting everything would copy
# the journal into the lesson base at ~70 cards/day, unread past the
# proposer's 200-card scan.
#
# _REFLECTOR_FOLD_THRESHOLD was calibrated on ONE week of ONE loop's output
# (the store above, 2026-09-03): every cluster at 0.35 was a coherent theme
# when read by eye; 0.30 gave 34 clusters, 0.40 gave 15, still coherent. It
# is a dial nobody can see the shoulder of unless the near-miss band below it
# is counted, so each run records how many items landed in
# [_REFLECTOR_NEAR_MISS_FLOOR, _REFLECTOR_FOLD_THRESHOLD) against their best
# match. Same-day-only recurrences (an incident echo — seven cycles failing
# the same way one afternoon is one event, not a lesson learned twice) are
# not minted while _REFLECTOR_MIN_DAYS is 2 and are counted separately.
_REFLECTOR_FOLD_THRESHOLD = 0.35
_REFLECTOR_NEAR_MISS_FLOOR = 0.25
_REFLECTOR_MIN_CYCLES = 2
_REFLECTOR_MIN_DAYS = 2
_REFLECTOR_MAX_ROWS_PER_RUN = 600  # ≈5 days of journal; the 738-row backlog clears in two nightly runs
_REFLECTOR_POOL_MAX = 400
_REFLECTOR_POOL_MAX_AGE_DAYS = 14  # every recurrence in the calibration store spanned ≤ 4 days
_REFLECTOR_POOL_SLUG = "reflector_pool.json"
_REFLECTOR_POOL_SCHEMA = "curator-reflector-pool-v1"
_REFLECTOR_CARD_EVIDENCE_CAP = 8
_REFLECTOR_KINDS = frozenset({"error_pattern", "approach_hint"})


def _reflector_pool_path(state_dir: Path) -> Path:
    return Path(state_dir) / "curator" / _REFLECTOR_POOL_SLUG


def load_reflector_pool(state_dir: Path) -> dict[str, Any]:
    """The mint's sidecar: cursor, waiting clusters, last-run counts. Missing
    or corrupt → a fresh pool (the cursor restarts from the oldest readable
    row, which is idempotent: folds by id, pool cycles are a set)."""
    raw = _safe_json(_reflector_pool_path(state_dir), None)
    if not isinstance(raw, dict) or raw.get("schema") != _REFLECTOR_POOL_SCHEMA:
        return {"schema": _REFLECTOR_POOL_SCHEMA, "cursor": "", "clusters": [], "last_run": {}, "last_staged_at": ""}
    raw.setdefault("cursor", "")
    raw["clusters"] = [c for c in (raw.get("clusters") or []) if isinstance(c, dict) and c.get("detail")]
    raw.setdefault("last_run", {})
    raw.setdefault("last_staged_at", "")
    return raw


def _reflector_rows_after(path: Path, cursor: str, limit: int) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Rows across the newest archives and the live journal whose ``timestamp``
    is after ``cursor``, oldest first, at most ``limit`` (#1171 cursor). Rows
    without a timestamp are always new — the cursor cannot place them."""
    counts = {"rows_read": 0, "rows_after_cursor": 0, "unparseable": 0}
    rows: list[dict[str, Any]] = []
    for line in _reflection_lines(path):
        if not line.strip():
            continue
        counts["rows_read"] += 1
        try:
            row = json.loads(line)
        except ValueError:
            counts["unparseable"] += 1
            continue
        if not isinstance(row, dict):
            continue
        ts = str(row.get("timestamp") or "")
        if ts and cursor and ts <= cursor:
            continue
        counts["rows_after_cursor"] += 1
        if len(rows) < limit:
            rows.append(row)
    return rows, counts


def _reflector_card(
    *, card_id: str, detail: str, problem: str, cycles: list[str], days: list[str],
    first_seen: str, last_seen: str,
) -> dict[str, Any]:
    return {
        "schema_version": 2, "id": card_id,
        "title": detail[:200],
        # problem = what was observed (the item's `evidence`), solution = the
        # recommendation. The pre-#1171 mint put the cycle NARRATIVE here
        # ("Added a doctest suite to tests/…"), and `find_duplicate` compares
        # `problem`, so two recommendations from one cycle folded into each
        # other and the second one's solution was discarded (179 of 502 items
        # in the live store were such same-row siblings).
        "problem": problem[:400],
        "solution": detail[:500],
        "tags": ["reflector"], "severity": "medium",
        "seen_count": max(1, len(cycles)),
        "first_seen": first_seen, "last_seen": last_seen,
        "evidence": list(cycles[:_REFLECTOR_CARD_EVIDENCE_CAP]),
        # How many calendar days the recurrence spans — 1 means an incident
        # echo (#1171); recorded so raising _REFLECTOR_MIN_DAYS/_MIN_CYCLES
        # later is a decision on data.
        "distinct_days": max(1, len(days)),
    }


def _new_card_id(existing_ids: set[str], first_cycle: str, detail: str) -> str:
    import hashlib

    base_id = f"LESS-REF-{(first_cycle or 'unknown')[-12:]}"
    digest = hashlib.sha1(detail.encode("utf-8")).hexdigest()[:4]
    card_id = f"{base_id}-{digest}"
    idx = 0
    while card_id in existing_ids:  # #1138: never reuse an id already in the store
        card_id = f"{base_id}-{digest}{idx}"
        idx += 1
    return card_id


def _prune_pool(clusters: list[dict[str, Any]], newest_ts: str) -> int:
    """Drop clusters idle past _REFLECTOR_POOL_MAX_AGE_DAYS, then the oldest
    beyond _REFLECTOR_POOL_MAX. Returns the number evicted."""
    before = len(clusters)
    cutoff = ""
    try:
        ref = datetime.fromisoformat(newest_ts.replace("Z", "+00:00")) if newest_ts else datetime.now(timezone.utc)
        cutoff = (ref.timestamp() - _REFLECTOR_POOL_MAX_AGE_DAYS * 86400)
        clusters[:] = [
            c for c in clusters
            if not c.get("last_seen")
            or datetime.fromisoformat(str(c["last_seen"]).replace("Z", "+00:00")).timestamp() >= cutoff
        ]
    except (ValueError, TypeError):
        pass
    if len(clusters) > _REFLECTOR_POOL_MAX:
        clusters.sort(key=lambda c: str(c.get("last_seen") or ""), reverse=True)
        del clusters[_REFLECTOR_POOL_MAX:]
    return before - len(clusters)


def promote_reflector_recommendations_to_v2(
    workspace: Path, state_dir: Path, *, max_items: int = 5,
) -> int:
    """Stage v2 lesson cards from reflector recommendations that RECUR (#1171).

    For every promotable item (``kind`` in :data:`_REFLECTOR_KINDS`, non-empty
    ``detail``) after the sidecar cursor:

    1. **Fold** into an existing v2 card whose solution says the same thing
       (keyword Jaccard ≥ :data:`_REFLECTOR_FOLD_THRESHOLD`) or whose problem
       matches (:func:`find_duplicate`): ``seen_count``/``last_seen``/evidence
       advance, a filler solution is upgraded (#1106), a narrative ``problem``
       written by the pre-#1171 mint is repaired when the item is the card's
       own origin. Folds never grow the base and are not capped.
    2. Else **pool**: join the cluster it matches or start one. A cluster
       **graduates** to a new card once it holds ≥ :data:`_REFLECTOR_MIN_CYCLES`
       distinct cycles across ≥ :data:`_REFLECTOR_MIN_DAYS` distinct days —
       at most ``max_items`` new cards per run (a valve, not a rate).

    Cards are staged for the bridge pickup (#1209); the cursor and pool are
    written only after staging succeeded. Returns the number of cards staged
    (folds + new). Per-stage counts land in the sidecar's ``last_run`` and one
    stdout line, so zero staged with candidates is distinguishable from an
    idle run (#1216).
    """
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
    pool = load_reflector_pool(state_dir)
    try:
        stat = path.stat()
        # The staleness guard stays. A reflector log untouched for 90 days
        # describes a loop that stopped running, and promoting from it would
        # mint lessons about a dead past. It is a claim about freshness, and it
        # un-trips by itself the moment the loop writes again — unlike the
        # 512 KiB size cap #1183 removed, which an append-only file could only
        # trip once and which silently switched this path off for four days.
        if time.time() - stat.st_mtime > _REFLECTOR_MAX_AGE_SECONDS:
            return 0
        rows, counts = _reflector_rows_after(path, str(pool.get("cursor") or ""), _REFLECTOR_MAX_ROWS_PER_RUN)
    except OSError:
        # Unreadable is not the same as "nothing to promote" (#1183). Both
        # still return 0 — the caller counts promotions — but the reason is
        # recorded so a dead path cannot pass for an idle one.
        import logging as _logging

        _logging.getLogger(__name__).warning(
            "promote_reflector_recommendations_to_v2: cannot read %s; "
            "no promotions attempted (unreadable, not empty)", path,
        )
        return 0
    if counts["unparseable"]:
        import logging as _logging

        _logging.getLogger(__name__).warning(
            "promote_reflector_recommendations_to_v2: skipped %d unparseable "
            "row(s) in %s", counts["unparseable"], path,
        )
    stats: dict[str, Any] = dict(counts)
    stats.update({
        "rows_processed": len(rows), "items": 0, "folded": 0, "repaired": 0,
        "pooled_new": 0, "pooled_recurrence": 0, "same_day_only_waiting": 0,
        "graduated": 0, "deferred_by_cap": 0, "near_misses": 0, "rejected": 0,
    })

    # Read-only baseline: the checkout's current store decides ids and
    # duplicates, but nothing below writes to it (#1209).
    existing = _load_lessons_list(Path(workspace) / LESSONS_REL)
    existing_ids = {str(e.get("id")) for e in existing if e.get("id")}
    card_words: dict[int, frozenset[str]] = {
        id(e): keyword_set(e.get("solution")) for e in existing if isinstance(e.get("solution"), str)
    }
    clusters: list[dict[str, Any]] = pool["clusters"]
    for cluster in clusters:
        cluster["_words"] = keyword_set(cluster.get("detail"))

    minted: list[dict[str, Any]] = []
    new_cards = 0
    today = datetime.now(timezone.utc).date().isoformat()
    newest_ts = str(pool.get("cursor") or "")
    for row in rows:
        cycle_id = str(row.get("cycle_id") or "reflector")
        ts = str(row.get("timestamp") or "")
        if ts > newest_ts:
            newest_ts = ts
        day = ts[:10] if len(ts) >= 10 else today
        for item in row.get("recommendations") or []:
            if not isinstance(item, dict) or item.get("kind") not in _REFLECTOR_KINDS:
                continue
            detail = str(item.get("detail") or "").strip()
            if not detail:
                continue
            stats["items"] += 1
            problem = str(item.get("evidence") or row.get("summary") or f"Reflected issue: {detail[:60]}").strip()
            words = keyword_set(detail)

            # 1) fold into an existing card
            best_card, best_score = None, 0.0
            for entry in existing:
                score = set_jaccard(words, card_words.get(id(entry), frozenset()))
                if score > best_score:
                    best_card, best_score = entry, score
            by_problem = find_duplicate(problem, existing)
            if by_problem is not None or best_score >= _REFLECTOR_FOLD_THRESHOLD:
                card = _reflector_card(
                    card_id=_new_card_id(existing_ids, cycle_id, detail), detail=detail, problem=problem,
                    cycles=[cycle_id], days=[day], first_seen=day, last_seen=day,
                )
                if not validate_lesson_for_mint(card):
                    stats["rejected"] += 1
                    continue
                target = by_problem if by_problem is not None else best_card
                before_problem = target.get("problem") if target is not None else None
                if _merge_card_into(existing, card) is None:
                    continue
                if target is not None:
                    if target.get("problem") != before_problem:
                        stats["repaired"] += 1
                    card_words[id(target)] = keyword_set(target.get("solution"))  # a filler solution may have been upgraded
                stats["folded"] += 1
                existing_ids.add(card["id"])
                minted.append(card)
                continue

            # 2) pool: join a cluster or start one
            best_cluster, best_cluster_score = None, 0.0
            for cluster in clusters:
                score = set_jaccard(words, cluster["_words"])
                if score > best_cluster_score:
                    best_cluster, best_cluster_score = cluster, score
            if best_cluster is None or best_cluster_score < _REFLECTOR_FOLD_THRESHOLD:
                if max(best_score, best_cluster_score) >= _REFLECTOR_NEAR_MISS_FLOOR:
                    stats["near_misses"] += 1
                clusters.append({
                    "detail": detail, "problem": problem, "kind": item.get("kind"),
                    "cycles": [cycle_id], "days": [day],
                    "first_seen": ts or day, "last_seen": ts or day, "_words": words,
                })
                stats["pooled_new"] += 1
                continue
            cluster = best_cluster
            if cycle_id not in cluster["cycles"]:
                cluster["cycles"].append(cycle_id)
            if day not in cluster["days"]:
                cluster["days"].append(day)
            cluster["last_seen"] = ts or day
            stats["pooled_recurrence"] += 1

    # 3) graduate: every cluster that now recurs across enough cycles AND days,
    # most-recurrent first, up to the per-run cap. Evaluated over the whole
    # pool (not only clusters touched this run) so a cluster deferred by the
    # cap, or waiting on a second day, graduates on a later run without needing
    # another matching item to arrive.
    ready = [
        c for c in clusters
        if len(c.get("cycles") or []) >= _REFLECTOR_MIN_CYCLES and len(c.get("days") or []) >= _REFLECTOR_MIN_DAYS
    ]
    stats["same_day_only_waiting"] = sum(
        1 for c in clusters
        if len(c.get("cycles") or []) >= _REFLECTOR_MIN_CYCLES and len(c.get("days") or []) < _REFLECTOR_MIN_DAYS
    )
    ready.sort(key=lambda c: (-len(c["cycles"]), str(c.get("first_seen") or "")))
    for cluster in ready:
        if new_cards >= max(0, int(max_items)):
            stats["deferred_by_cap"] += 1  # stays in the pool for the next run
            continue
        last_day = str(cluster.get("last_seen") or today)[:10]
        card = _reflector_card(
            card_id=_new_card_id(existing_ids, cluster["cycles"][0], cluster["detail"]),
            detail=str(cluster["detail"]), problem=str(cluster.get("problem") or f"Reflected issue: {cluster['detail'][:60]}"),
            cycles=list(cluster["cycles"]), days=list(cluster["days"]),
            first_seen=str(cluster.get("first_seen") or last_day)[:10], last_seen=last_day,
        )
        if not validate_lesson_for_mint(card):
            stats["rejected"] += 1
            clusters.remove(cluster)
            continue
        # Merge into the in-memory baseline only, so the next card sees this
        # one's id; the checkout is written by the bridge at pickup (#1209).
        if _merge_card_into(existing, card) is None:
            clusters.remove(cluster)
            continue
        card_words[id(card)] = cluster["_words"]
        existing_ids.add(card["id"])
        minted.append(card)
        clusters.remove(cluster)
        new_cards += 1
        stats["graduated"] += 1

    if minted:
        _stage_lesson_cards(state_dir, minted)
        for card in minted:
            _write_decision(
                state_dir, str(card["id"]), "staged",
                "reflector recommendation staged for bridge pickup (#1209)", LESSONS_REL,
            )
        pool["last_staged_at"] = _now()
    stats["staged"] = len(minted)
    stats["evicted"] = _prune_pool(clusters, newest_ts)
    for cluster in clusters:
        cluster.pop("_words", None)
    stats["pool_size"] = len(clusters)
    stats["at"] = _now()
    # Cursor and pool advance only after staging succeeded (above); a crash
    # before this write re-processes the same rows next run, which is
    # idempotent (folds by id, pool cycles are a set, staged payload merges by id).
    pool["cursor"] = newest_ts
    pool["last_run"] = stats
    _atomic_json(_reflector_pool_path(state_dir), pool)
    print(
        "curator-reflector: "
        + " ".join(f"{k}={stats[k]}" for k in (
            "rows_read", "rows_processed", "items", "folded", "repaired", "pooled_new",
            "pooled_recurrence", "same_day_only_waiting", "graduated", "deferred_by_cap",
            "near_misses", "rejected", "staged", "pool_size",
        ))
        + f" cursor={newest_ts or '-'}"
    )
    return len(minted)


def _load_lessons_list(target: Path) -> list[dict[str, Any]]:
    """The v2 lessons store as a list — bounded read first, plain YAML fallback."""
    existing = bounded_load_yaml(target)
    if not existing and target.exists():
        try:
            import yaml

            raw = yaml.safe_load(target.read_text(encoding="utf-8"))
            existing = raw.get("lessons", []) if isinstance(raw, dict) else raw if isinstance(raw, list) else []
        except Exception:
            existing = []
    return [entry for entry in existing if isinstance(entry, dict)]


def _merge_card_into(existing: list[dict[str, Any]], card: dict[str, Any]) -> str | None:
    """Merge one minted card into *existing* in place.

    Returns ``"inserted"`` (new card, newest-first), ``"folded"`` (a near-
    duplicate absorbed it: seen_count/last_seen advance and a filler solution is
    upgraded in place, #1106) or ``None`` when the card's id is already present
    — the idempotent case a retried pickup relies on. One implementation for
    stage time (in-memory baseline) and pickup time (the checkout).
    """
    card_id = str(card.get("id") or "")
    if card_id and any(str(entry.get("id") or "") == card_id for entry in existing):
        return None
    duplicate = _fold_target(existing, card)
    if duplicate is not None:
        from nanobot.runtime.lesson_v2 import solution_is_meaningful
        if not solution_is_meaningful(duplicate.get("problem"), duplicate.get("solution")):
            duplicate["solution"] = card.get("solution")
        incoming_cycles = [str(e) for e in (card.get("evidence") or []) if e]
        known_cycles = [str(e) for e in (duplicate.get("evidence") or []) if e]
        new_cycles = [c for c in incoming_cycles if c not in known_cycles]
        # #1171: the incoming card is the same recommendation seen in cycles
        # this card already counts (typically its own origin item re-read
        # after a cursor reset) — repair, do not re-count.
        same_origin = bool(incoming_cycles) and bool(known_cycles) and not new_cycles
        if same_origin:
            if (
                card.get("problem") and duplicate.get("problem") != card.get("problem")
                and set_jaccard(keyword_set(card.get("solution")), keyword_set(duplicate.get("solution"))) >= 0.8
            ):
                # The pre-#1171 mint stored the cycle narrative as `problem`;
                # the origin item carries the observation it should have had.
                duplicate["problem"] = card["problem"]
        else:
            increment = len(new_cycles) if incoming_cycles and known_cycles else int(card.get("seen_count") or 1)
            duplicate["seen_count"] = int(duplicate.get("seen_count") or 1) + max(1, increment)
            if new_cycles:
                duplicate["evidence"] = (known_cycles + new_cycles)[:_REFLECTOR_CARD_EVIDENCE_CAP]
            if card.get("distinct_days") or duplicate.get("distinct_days"):
                duplicate["distinct_days"] = max(
                    int(duplicate.get("distinct_days") or 1), int(card.get("distinct_days") or 1),
                )
        duplicate["last_seen"] = card.get("last_seen")
        return "folded"
    existing.insert(0, card)
    return "inserted"


def _fold_target(existing: list[dict[str, Any]], card: dict[str, Any]) -> dict[str, Any] | None:
    """The card an incoming card folds into, or ``None`` (#1171). Same problem
    (:func:`find_duplicate`: hash or Jaccard ≥ 0.8) first; else the entry whose
    solution says the same thing (keyword Jaccard ≥
    :data:`_REFLECTOR_FOLD_THRESHOLD`), best match. One rule for stage time
    and pickup time, so the pickup agrees with the curator's decision."""
    duplicate = find_duplicate(str(card.get("problem") or ""), existing)
    if duplicate is not None:
        return duplicate
    words = keyword_set(card.get("solution"))
    if not words:
        return None
    best, best_score = None, 0.0
    for entry in existing:
        if not isinstance(entry, dict) or not isinstance(entry.get("solution"), str):
            continue
        score = set_jaccard(words, keyword_set(entry["solution"]))
        if score > best_score:
            best, best_score = entry, score
    return best if best_score >= _REFLECTOR_FOLD_THRESHOLD else None


def _stage_lesson_cards(state_dir: Path, cards: list[dict[str, Any]]) -> dict[str, Any]:
    """Stage reflector v2 cards for the bridge pickup (#1209).

    One payload file per staging dir holds the pending cards (merged by id with
    whatever a previous, not yet picked-up run staged); one manifest entry of
    kind :data:`LESSONS_KIND` points at it. Atomic like the fact payloads.
    """
    staged_dir = Path(state_dir) / "curator" / _STAGED_DIR
    staged_dir.mkdir(parents=True, exist_ok=True)
    payload_path = staged_dir / _LESSONS_PAYLOAD_SLUG
    pending: list[dict[str, Any]] = []
    try:
        loaded = json.loads(payload_path.read_text(encoding="utf-8"))
        pending = [c for c in (loaded.get("cards") if isinstance(loaded, dict) else loaded) or [] if isinstance(c, dict)]
    except Exception:
        pending = []
    by_id = {str(c.get("id") or ""): i for i, c in enumerate(pending)}
    for card in cards:
        cid = str(card.get("id") or "")
        if cid in by_id:
            pending[by_id[cid]] = card
        else:
            pending.append(card)
    _atomic_json(payload_path, {"schema": "curator-staged-lessons-v1", "cards": pending})
    entry = {
        "path": LESSONS_REL,
        "kind": LESSONS_KIND,
        "action": "merge",
        "payload_file": _LESSONS_PAYLOAD_SLUG,
        "lesson_id": "",
        "lesson_ids": [str(c.get("id") or "") for c in pending],
        "index_line": "",
        "related": "",
        "unknown_related": [],
        "index_rel": "",
        "evidence": sorted({str(e) for c in pending for e in (c.get("evidence") or [])}),
        "support_claim": "",
        "verification_status": "supported",
        "overlap_flag": False,
    }
    _append_manifest_entries(staged_dir, [entry])
    return entry


def apply_staged_lesson_cards(repo_root: Path, payload: Any) -> list[str]:
    """Merge staged v2 cards into ``<repo_root>/lessons/lessons.yaml`` (#1209).

    Called by the bridge pickup with the checkout on clean ``main``. Returns
    the ids that changed the store (inserted or folded); an empty list means
    every card was already present and the file was left untouched. The
    caller commits and pushes; this function only edits the working tree.
    """
    cards = payload.get("cards") if isinstance(payload, dict) else payload
    cards = [c for c in (cards or []) if isinstance(c, dict) and c.get("id")]
    if not cards:
        return []
    target = Path(repo_root) / LESSONS_REL
    existing = _load_lessons_list(target)
    applied: list[str] = []
    for card in cards:
        if _merge_card_into(existing, card) is not None:
            applied.append(str(card["id"]))
    if applied:
        existing, _unknown = fill_related_links(existing)
        atomic_write_yaml(target, {"lessons": existing})
    return applied


def record_pickup_outcome(
    state_dir: Path, lesson_ids: Iterable[str], decision: str, reason: str, target: str = "",
) -> None:
    """Append one ``decisions.jsonl`` row per id for what the bridge pickup did
    (#1209): ``promoted`` once the commit is on ``origin/main``,
    ``pickup_deferred`` when the push failed and the item stays in staging."""
    for lesson_id in lesson_ids:
        if lesson_id:
            _write_decision(Path(state_dir), str(lesson_id), decision, reason, target)


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
    promote_reflector_recommendations_to_v2(workspace, state_dir, max_items=5)
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
    """Stage the legacy loose-note migration for bridge pickup (#1214).

    This preserves the old deterministic behavior — one fact per unique note
    body, an index line for newly-created facts, and every source note moved to
    ``lessons/archive/loose`` — without writing the workspace checkout. The
    existing #1209 staging manifest is the only durable handoff.
    """
    workspace, state_dir = Path(workspace), Path(state_dir) if state_dir is not None else None
    if state_dir is None:
        return {"ok": False, "migrated": 0, "facts_created": 0, "error": "state_dir is required for staging"}
    loose = sorted((workspace / "lessons").glob("*.md"))
    if not loose:
        return {"ok": True, "migrated": 0, "facts_created": 0, "staged": []}

    groups: dict[str, list[tuple[Path, str]]] = {}
    for path in loose:
        try:
            content = path.read_text(encoding="utf-8").strip()
        except Exception:
            return {"ok": False, "migrated": 0, "facts_created": 0, "error": f"could not read {path.name}"}
        key = re.sub(r"\W+", " ", content.lower()).strip()
        if key:
            groups.setdefault(key, []).append((path, content))

    items: list[dict[str, Any]] = []
    planned_facts: set[str] = set()
    for paths in groups.values():
        first_path, first_content = paths[0]
        slug = re.sub(r"[^a-z0-9]+", "-", first_path.stem.lower()).strip("-")[:60] or "lesson"
        target = workspace / "memory" / "facts" / f"{slug}.md"
        if not target.exists() and str(target) not in planned_facts:
            rel = f"memory/facts/{slug}.md"
            items.append({
                "path": rel,
                "action": "create",
                "content": f"# {first_path.stem}\n\n{first_content}\n",
                "lesson_id": first_path.stem,
                "index_line": f"- [{first_path.stem}]({rel})",
                "index_rel": "memory/index.md",
            })
            planned_facts.add(str(target))

    for path in loose:
        try:
            content = path.read_text(encoding="utf-8").strip()
        except Exception:
            return {"ok": False, "migrated": 0, "facts_created": 0, "error": f"could not read {path.name}"}
        items.append({
            "path": f"lessons/archive/loose/{path.name}",
            "source_path": f"lessons/{path.name}",
            "action": "move",
            "kind": "loose_lesson",
            "content": content + "\n",
            "lesson_id": path.stem,
        })

    try:
        staged = _stage_promotions(state_dir, items)
    except Exception as exc:
        return {"ok": False, "migrated": 0, "facts_created": 0, "error": f"staging failed: {exc}"}
    for entry in staged:
        _write_decision(
            state_dir,
            str(entry.get("lesson_id") or entry.get("path") or ""),
            "staged",
            "loose lesson migration staged for bridge pickup",
            str(entry.get("path") or ""),
        )
    return {
        "ok": True,
        "migrated": len(loose),
        "facts_created": sum(1 for item in items if item.get("kind") != "loose_lesson"),
        "staged": [str(entry.get("path") or "") for entry in staged],
    }


def main() -> int:
    workspace = Path(os.environ.get("TARGET_WORKSPACE", "/var/lib/eeepc-agent/self-evolving-agent/eeebot-self-evolving"))
    state = Path(os.environ.get("STATE_DIR", "/var/lib/eeepc-agent/self-evolving-agent/state"))
    print(json.dumps(run_curation(workspace, state), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
