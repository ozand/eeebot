"""Bounded, auditable lesson-to-knowledge curation (#986).

The curator is deliberately a small adapter around the existing LLM and git
boundaries. It never deletes files, rewrites an index, or advances its
watermark before the proposed changes pass the caller-supplied gate.
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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from nanobot.observability.llm_telemetry import call_context, record_llm_call, record_llm_prompt
from nanobot.runtime.model_registry import resolve_model

MAX_WRITES_DEFAULT = 3
MAX_LESSONS_DEFAULT = 40
MAX_INPUT_CHARS = 48_000
MAX_OUTPUT_CHARS = 30_000
_DECISIONS = {"promoted", "duplicate", "unimportant", "rejected"}
_ALLOWED_FACT_PREFIXES = ("memory/facts/", "docs/facts/")


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


def iter_lessons(workspace: Path) -> Iterable[dict[str, Any]]:
    """Yield archived lessons oldest-first, then the current live journal."""
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


def lessons_after(workspace: Path, watermark: str, *, limit: int = MAX_LESSONS_DEFAULT) -> list[dict[str, Any]]:
    found = not bool(watermark)
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in iter_lessons(workspace):
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
        clean.append(item)
    return clean


def _messages(lessons: list[dict[str, Any]], index: str, facts: str) -> list[dict[str, str]]:
    body = json.dumps(lessons, ensure_ascii=False, separators=(",", ":"))
    body = body[:MAX_INPUT_CHARS]
    system = (
        "You are the eeebot knowledge curator. Return ONLY a JSON array. "
        "Each item must be one of: {action:create,path,title,content,index_line,lesson_id,reason}, "
        "{action:update,path,content,lesson_id,reason}, or {action:duplicate|unimportant,lesson_id,reason}. "
        "Create/update paths must be memory/facts/*.md or docs/facts/*.md. Never delete or rewrite an index. "
        "At most three create/update items; every item needs a one-line reason."
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
        return None
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


def _gate(repo: Path, changed: list[str], gate: Callable[[Path, list[str]], bool] | None) -> bool:
    if gate is not None:
        return bool(gate(repo, changed))
    try:
        from nanobot.runtime import bridge
        if bridge._validate_mutation_surfaces(changed):
            return False
        ok, _ = bridge._run_smoke_tests(repo, changed_files=changed, timeout=300)
        return bool(ok)
    except Exception:
        return False


def _write_decision(state: Path, lesson_id: str, decision: str, reason: str, target: str = "") -> None:
    _append_jsonl(state / "curator" / "decisions.jsonl", {
        "timestamp": _now(), "lesson_id": lesson_id, "decision": decision,
        "reason": str(reason or "")[:300], "target_file": target,
    })


def _apply(workspace: Path, state_dir: Path, decisions: list[dict[str, Any]], max_writes: int) -> tuple[list[str], int]:
    changed: list[str] = []
    writes = 0
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
        path = workspace / rel
        exists = path.exists()
        if action == "update" and not exists:
            _write_decision(state_dir, lesson_id, "rejected", "update target does not exist", str(rel))
            continue
        if action == "create" and exists:
            _write_decision(state_dir, lesson_id, "duplicate", "fact already exists", str(rel))
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content.rstrip() + "\n", encoding="utf-8")
        changed.append(str(rel).replace("\\", "/"))
        if action == "create":
            line = str(d.get("index_line") or f"- [{d.get('title') or path.stem}]({rel.as_posix()})")
            index = workspace / ("memory/index.md" if rel.parts[0] == "memory" else "docs/index.md")
            with index.open("a", encoding="utf-8") as fh:
                fh.write("\n" + line.rstrip() + "\n")
            changed.append(str(index.relative_to(workspace)).replace("\\", "/"))
        writes += 1
        _write_decision(state_dir, lesson_id, "promoted", reason, str(rel))
    return changed, writes


def run_curation(
    workspace: Path,
    state_dir: Path,
    *,
    llm: Callable[[list[dict[str, str]], str], Any] | None = None,
    gate: Callable[[Path, list[str]], bool] | None = None,
    max_writes: int = MAX_WRITES_DEFAULT,
    max_lessons: int = MAX_LESSONS_DEFAULT,
) -> dict[str, Any]:
    """Run one fail-open curator pass. Never raises to the timer."""
    workspace, state_dir = Path(workspace), Path(state_dir)
    wm_path = state_dir / "curator" / "watermark.json"
    old = _safe_json(wm_path, {})
    watermark = str(old.get("last_processed") or old.get("last_processed_id") or "") if isinstance(old, dict) else ""
    entries = lessons_after(workspace, watermark, limit=max_lessons)
    if not entries:
        return {"ok": True, "processed": 0, "writes": 0}
    try:
        model = resolve_model("curator", strip_openai=True)
        messages = _messages(entries, _read_index(workspace), "")
        result = llm(messages, model) if llm else _default_llm(messages, model)
        if inspect.isawaitable(result):
            result = asyncio.run(result)
        decisions = _parse_output(result)
        if decisions is None:
            raise ValueError("malformed curator output")
        # Fetch only bodies named by the draft, then re-run the same bounded
        # request with those bodies. This keeps the full KB out of both calls.
        facts = _touched_facts(workspace, decisions)
        if facts:
            caller = llm or _default_llm
            result = caller(_messages(entries, _read_index(workspace), facts), model)
            if inspect.isawaitable(result):
                result = asyncio.run(result)
            decisions = _parse_output(result)
            if decisions is None:
                raise ValueError("malformed curator output")
        snapshot_paths: set[Path] = set()
        for decision in decisions:
            rel = _fact_path(str(decision.get("path") or ""))
            if rel is not None:
                snapshot_paths.add(workspace / rel)
                if str(decision.get("action") or "").lower() in {"create", "promote"}:
                    snapshot_paths.add(workspace / ("memory/index.md" if rel.parts[0] == "memory" else "docs/index.md"))
        snapshots = {path: path.read_bytes() if path.exists() else None for path in snapshot_paths}
        changed, writes = _apply(workspace, state_dir, decisions, max(0, int(max_writes)))
        # The gate runs after materialization, but a failed gate must leave no KB mutation.
        if changed and not _gate(workspace, changed, gate):
            for path, original in snapshots.items():
                try:
                    if original is None:
                        path.unlink(missing_ok=True)
                    else:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(original)
                except Exception:
                    pass
            raise PermissionError("curator output rejected by mutation/smoke gate")
        # Index lines are the only allowed index mutation; never accept a
        # model-provided wholesale index path or an arbitrary changed path.
        if any(path not in {"memory/index.md", "docs/index.md"} and not _fact_path(path) for path in changed):
            raise PermissionError("curator output contained an unbounded path")
        # When the hard write cap is hit, stop the watermark at the lesson
        # that filled the cap so later candidates remain eligible next run.
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
        return {"ok": True, "processed": len(entries), "writes": writes, "changed": changed}
    except Exception as exc:
        _append_jsonl(state_dir / "curator" / "errors.jsonl", {"timestamp": _now(), "error": str(exc)[:500]})
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
