"""Deterministic, LLM-free demand collector for the demand-driven proposer (#760).

The loop was supply-driven: every ~10 minutes the timer asked the LLM to
"invent a task" over a value-poor workspace, and the model — asked to invent —
invented, burning 2-3 LLM calls per cycle on proposals its own self-dedup then
silently rejected (the saturation burn observed live 2026-07-15). This module
is the missing engine half of that loop inversion: a deterministic scan of
the operator queue and the loop's own telemetry that yields structured
**demand items** the LLM may only *select and refine from* — never invent
beyond. With no demand, the proposer makes ZERO LLM calls and the cycle
records an idle heartbeat instead (see ``llm_proposer.should_propose``).

Demand kinds, in trust order (see ``docs/changes/760-demand-driven-proposer/``):

- ``priority`` — remaining (non-completed) "Current priority targets" entries
  from the filtered goal_text. Reuses
  ``cycle_planning.filter_completed_priorities_from_goal_text`` verbatim —
  done-detection is NOT reimplemented here (#748 owns it).
- ``defect`` — real, recent failures found in state artifacts:
  (a) terminal ledger ``outcome`` rows with a failed/timeout outcome in the
  last 48h (``skipped-*`` outcomes are not defects — they are the dedup stack
  working); (b) failed/blocked subagent result files carrying error text
  (bounded to the most recent :data:`_MAX_RESULT_FILES` files, following
  ``existence_index._MAX_LEDGER_RESULTS``'s bounded-read discipline);
  (c) instance-repo scripts that fail to byte-compile — watermark-gated on
  the repo's git HEAD exactly like ``system_map.update_system_map`` (own
  sidecar under ``<state_dir>/demand/``), so the scan costs nothing when
  HEAD hasn't moved.
- ``hypothesis`` — ONLY hypotheses carrying measurement evidence: a
  non-empty ``evidence`` or ``metric`` field, or an ``acceptance`` text that
  references a file path actually present in the instance repo. The chronic
  boilerplate candidates ("Use one bounded subagent-assisted review...",
  "Synthesize one new bounded improvement candidate from retired lanes") have
  none of these and MUST NOT qualify (regression-pinned in tests).

Each item is ``{kind, id, summary, evidence, affected_path}`` with a stable
``id`` (hash of kind+summary) used for exhaustion tracking: once a demand
item's proposals have been self-dedup-rejected 2+ times (matched via the
``demand_id`` recorded on ``proposer_reject`` ledger rows, #762/#760), the
item is marked exhausted in ``<state_dir>/demand/exhausted.json``
(schema-versioned, like ``hypothesis_backlog``'s lifecycle sidecar) and no
longer presented. Exhaustion resets (#771, live deadlock 2026-07-15) on any
of: a successful integration (terminal ledger ``outcome: success`` row newer
than the entry's ``exhausted_at``), a runtime release change (the release id
recorded in the entry differs from the running one), a repo HEAD move (cheap
``git rev-parse`` re-check), or :data:`_EXHAUSTION_EXPIRY_HOURS` hours
elapsing. An expired entry flips to a ``reset`` record carrying ``reset_at``
so only rejects newer than the reset can re-exhaust the item (otherwise the
old ledger rows would re-exhaust it instantly, defeating expiry). A MISSING
entry behaves the same way — manually clearing ``entries`` is an honest
reset: only rejects newer than the newest of (last success, 24h ago) count,
so stale bug-era ledger rows cannot silently resurrect an exhaustion the
operator just cleared (#771).

Completed items (#773): the ledger chain — a ``proposed`` row carrying
``demand_id`` followed by a same-``cycle_id`` terminal ``outcome: success``
row — is the authoritative done-truth for demand items (the model refines
proposal titles in demand mode, so text-based git-log evidence structurally
cannot see these integrations). ``collect_demand`` folds new pairs from the
current ledger into ``<state_dir>/demand/completed.json`` (append-only,
rotation-proof by construction) and drops completed ids from ALL demand
kinds before the exhausted filter — a completed item needs no exhaustion
bookkeeping at all. ``cycle_planning.filter_completed_priorities_from_
goal_text`` consumes the same sidecar (via :func:`completed_demand_ids`)
when given a ``state_dir``.

Kill switch: ``SELFEVO_DEMAND_DRIVEN_ENABLED`` — #750 pattern, default ON
(absent, ``"1"``, or garbage all mean enabled); the literal ``"0"`` disables
demand-driven mode wholesale, restoring the pre-#760 proposer behavior (the
old code paths in ``llm_proposer`` stay intact behind this switch).

Everything here is deterministic (NO LLM call) and fail-open: a
missing/corrupt file, an unreadable directory, or any unexpected exception
degrades to "no demand from this source" — never raises into the caller.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ENABLED_ENV = "SELFEVO_DEMAND_DRIVEN_ENABLED"

_DEFECT_WINDOW_HOURS = 48
_MAX_RESULT_FILES = 50  # bounded read, same discipline as existence_index._MAX_LEDGER_RESULTS
_MAX_LEDGER_DEFECTS = 10
_MAX_COMPILE_DEFECTS = 10
_MAX_SUMMARY_CHARS = 160
_MAX_EVIDENCE_CHARS = 240

_EXHAUSTION_REJECTS = 2
_EXHAUSTION_EXPIRY_HOURS = 24  # was 7 days; shortened by #771 (deadlock escape)

_SCRIPT_DIRS = ("scripts", "surfaces")  # mirrors system_map._SCRIPT_DIRS

_EXHAUSTED_SCHEMA = "demand-exhausted-v1"
_COMPLETED_SCHEMA = "demand-completed-v1"
_COMPILE_WATERMARK_SCHEMA = "demand-py-compile-watermark-v1"

# Same regex family as llm_proposer._PRIORITY_PATTERN /
# cycle_planning._parse_backlog_task_from_goal_text — one entry per
# "(A) Priority N — Title: instructions" line.
_PRIORITY_PATTERN = re.compile(
    r"\([A-Za-z]\)\s*Priority\s+(\d+)\s*[—-]\s*(.+?):\s*(.+?)(?=\n\([A-Za-z]\)|\Z)",
    re.DOTALL,
)

# Loose "looks like a repo file path" matcher for hypothesis acceptance text:
# something with a slash or a dot-extension, e.g. scripts/foo.py, docs/x.md.
_PATH_TOKEN_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_\-./]*/[A-Za-z0-9_\-./]+|[A-Za-z0-9_\-]+\.[A-Za-z0-9]{1,5}")


def demand_driven_enabled() -> bool:
    """#750-pattern kill switch: default ON; only the literal ``"0"`` disables."""
    return os.environ.get(ENABLED_ENV, "1").strip() != "0"


def item_id(kind: str, summary: str) -> str:
    """Stable demand-item id: kind-prefixed short hash of kind+summary."""
    digest = hashlib.sha256(f"{kind}\x00{summary}".encode("utf-8", errors="replace")).hexdigest()
    return f"{kind}-{digest[:12]}"


def _make_item(kind: str, summary: str, evidence: str, affected_path: str = "") -> dict[str, str]:
    summary = (summary or "").strip()[:_MAX_SUMMARY_CHARS]
    return {
        "kind": kind,
        "id": item_id(kind, summary),
        "summary": summary,
        "evidence": (evidence or "").strip()[:_MAX_EVIDENCE_CHARS],
        "affected_path": (affected_path or "").strip()[:200],
    }


def _read_json(path: Path, default: Any) -> Any:
    try:
        if not path.is_file():
            return default
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, data: Any) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _parse_ts(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except Exception:
        return None


def _git_head(selfevo_repo: Path | None) -> str | None:
    if not selfevo_repo:
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(selfevo_repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None
    except Exception:
        return None


# ─── kind: priority ─────────────────────────────────────────────────────────


def _priority_items(state_dir: Path, selfevo_repo: Path | None) -> list[dict[str, str]]:
    """Remaining goal_text priorities, done-filtering delegated to
    ``cycle_planning.filter_completed_priorities_from_goal_text`` (#748) —
    this preserves R30: a freshly-seeded operator priority is always demand."""
    try:
        from nanobot.runtime.cycle_planning import filter_completed_priorities_from_goal_text

        path = Path(state_dir) / "goals" / "goal_text.json"
        data = _read_json(path, None)
        if not isinstance(data, dict):
            return []
        raw_text = str(data.get("text") or "")
        if not raw_text:
            return []
        filtered = filter_completed_priorities_from_goal_text(
            raw_text, selfevo_repo, state_dir=state_dir
        )
        marker = "Current priority targets:"
        idx = filtered.find(marker)
        if idx == -1:
            return []
        section = filtered[idx + len(marker):]
        items: list[dict[str, str]] = []
        for m in _PRIORITY_PATTERN.finditer(section):
            num, title, instructions = m.group(1), m.group(2).strip(), m.group(3).strip()
            items.append(
                _make_item(
                    "priority",
                    f"Priority {num} — {title}",
                    instructions,
                )
            )
        return items
    except Exception:
        return []


# ─── kind: defect ───────────────────────────────────────────────────────────


def _ledger_defects(state_dir: Path, now: datetime) -> list[dict[str, str]]:
    """Terminal ledger outcome rows with a real failure in the last 48h.
    ``skipped-*`` outcomes are the dedup stack working, not defects."""
    items: list[dict[str, str]] = []
    seen_summaries: set[str] = set()
    try:
        path = Path(state_dir) / "ledger" / "cycles.jsonl"
        if not path.is_file():
            return []
        cutoff = now - timedelta(hours=_DEFECT_WINDOW_HOURS)
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if not isinstance(row, dict) or row.get("phase") != "outcome":
                continue
            outcome = str(row.get("outcome") or "").strip().lower()
            if outcome.startswith("skipped"):
                continue
            if outcome not in ("failed", "timeout"):
                continue
            ts = _parse_ts(row.get("ts"))
            if ts is None or ts < cutoff:
                continue
            reason = str(row.get("reason") or "").strip()
            branch = str(row.get("branch") or row.get("cycle_id") or "").strip()
            summary = f"cycle outcome {outcome}: {reason or branch or '(no detail)'}"
            if summary in seen_summaries:
                continue
            seen_summaries.add(summary)
            items.append(
                _make_item(
                    "defect",
                    summary,
                    f"ledger outcome row cycle_id={row.get('cycle_id') or '?'} reason={reason or '(none)'}",
                )
            )
            if len(items) >= _MAX_LEDGER_DEFECTS:
                break
        return items
    except Exception:
        return items


def _skipped_cycle_ids(state_dir: Path, now: datetime) -> set[str]:
    """Cycle ids whose terminal ledger outcome is ``skipped-*`` in the defect
    window. Their result files are dedup bookkeeping, not defects (#760
    roll-out fix: a blocked-by-dedup result masqueraded as demand and kept
    the loop calling the LLM). Fail-open: errors yield an empty set — worst
    case a bookkeeping result is presented as demand again, never a crash."""
    skipped: set[str] = set()
    try:
        path = Path(state_dir) / "ledger" / "cycles.jsonl"
        if not path.is_file():
            return skipped
        cutoff = now - timedelta(hours=_DEFECT_WINDOW_HOURS)
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if not isinstance(row, dict) or row.get("phase") != "outcome":
                continue
            if not str(row.get("outcome") or "").strip().lower().startswith("skipped"):
                continue
            ts = _parse_ts(row.get("ts"))
            if ts is not None and ts < cutoff:
                continue
            cycle_id = str(row.get("cycle_id") or "").strip()
            if cycle_id:
                skipped.add(cycle_id)
        return skipped
    except Exception:
        return skipped


def _result_file_defects(state_dir: Path, now: datetime) -> list[dict[str, str]]:
    """Failed/blocked subagent result files with error text — bounded to the
    :data:`_MAX_RESULT_FILES` most recently modified files.

    Two exclusions keep dedup bookkeeping out of demand (#760 roll-out fix):
    results whose cycle terminally ``skipped-*`` in the ledger, and
    ``blocked`` results carrying no error text at all (the bridge writes
    such placeholder results for every pre-spawn skip)."""
    items: list[dict[str, str]] = []
    try:
        results_dir = Path(state_dir) / "subagents" / "results"
        if not results_dir.is_dir():
            return []
        cutoff_ts = (now - timedelta(hours=_DEFECT_WINDOW_HOURS)).timestamp()
        skipped_cycles = _skipped_cycle_ids(state_dir, now)
        entries = [p for p in results_dir.glob("*.json") if p.is_file()]
        try:
            entries.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        except Exception:
            pass
        for entry in entries[:_MAX_RESULT_FILES]:
            try:
                if entry.stat().st_mtime < cutoff_ts:
                    continue
                data = json.loads(entry.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            status = str(data.get("status") or "").strip().lower()
            if status not in ("failed", "blocked", "error"):
                continue
            if str(data.get("cycle_id") or "").strip() in skipped_cycles:
                continue  # dedup bookkeeping, not a defect (#760 roll-out fix)
            blocker = data.get("blocker")
            blocker_reason = blocker.get("reason", "") if isinstance(blocker, dict) else ""
            error_text = str(
                data.get("error") or data.get("error_text") or blocker_reason or ""
            ).strip()
            if status == "blocked" and not error_text:
                continue  # placeholder blocked result with no error signal
            title = str(data.get("backlog_title") or data.get("task_title") or entry.stem).strip()
            summary = f"subagent result {status}: {title}"
            items.append(
                _make_item(
                    "defect",
                    summary,
                    error_text or f"result file {entry.name} status={status}",
                )
            )
        return items
    except Exception:
        return items


def _compile_watermark_path(state_dir: Path) -> Path:
    return Path(state_dir) / "demand" / "py_compile_watermark.json"


def _compile_defects(state_dir: Path, selfevo_repo: Path | None, head: str | None) -> list[dict[str, str]]:
    """Instance-repo scripts that fail to byte-compile (syntax errors).

    Watermark-gated on the repo's git HEAD exactly like
    ``system_map.update_system_map``: when HEAD matches the sidecar's stored
    head, the cached findings are reused and NO file is even opened. Uses the
    builtin ``compile()`` (the same syntax check ``py_compile`` performs)
    rather than ``py_compile.compile`` so nothing is ever written to the
    instance repo (no ``__pycache__`` side effects).
    """
    if not selfevo_repo:
        return []
    try:
        repo = Path(selfevo_repo)
        if not repo.is_dir() or head is None:
            return []
        wm_path = _compile_watermark_path(state_dir)
        watermark = _read_json(wm_path, None)
        if (
            isinstance(watermark, dict)
            and watermark.get("git_head") == head
            and isinstance(watermark.get("failures"), list)
        ):
            failures = watermark["failures"]
        else:
            failures = []
            for dirname in _SCRIPT_DIRS:
                d = repo / dirname
                if not d.is_dir():
                    continue
                try:
                    py_files = sorted(d.glob("*.py"))
                except Exception:
                    continue
                for py_path in py_files:
                    try:
                        source = py_path.read_text(encoding="utf-8", errors="replace")
                        compile(source, str(py_path), "exec")
                    except SyntaxError as exc:
                        try:
                            rel = str(py_path.relative_to(repo))
                        except Exception:
                            rel = py_path.name
                        failures.append({"path": rel, "error": f"{type(exc).__name__}: {exc.msg} (line {exc.lineno})"})
                    except Exception:
                        continue
                    if len(failures) >= _MAX_COMPILE_DEFECTS:
                        break
            _write_json(
                wm_path,
                {
                    "schema_version": _COMPILE_WATERMARK_SCHEMA,
                    "git_head": head,
                    "scanned_at_utc": datetime.now(timezone.utc).isoformat(),
                    "failures": failures,
                },
            )
        items: list[dict[str, str]] = []
        for failure in failures[:_MAX_COMPILE_DEFECTS]:
            if not isinstance(failure, dict):
                continue
            rel = str(failure.get("path") or "").strip()
            err = str(failure.get("error") or "").strip()
            if not rel:
                continue
            items.append(
                _make_item(
                    "defect",
                    f"script fails to compile: {rel}",
                    err or "py_compile failure",
                    affected_path=rel,
                )
            )
        return items
    except Exception:
        return []


# ─── kind: hypothesis ───────────────────────────────────────────────────────


def _acceptance_references_repo_file(acceptance: str, selfevo_repo: Path | None) -> bool:
    if not acceptance or not selfevo_repo:
        return False
    try:
        repo = Path(selfevo_repo)
        if not repo.is_dir():
            return False
        for token in _PATH_TOKEN_RE.findall(acceptance)[:20]:
            token = token.strip().strip(".,;:")
            if not token or "/" not in token:
                continue
            if (repo / token).exists():
                return True
        return False
    except Exception:
        return False


def _hypothesis_has_evidence(entry: dict[str, Any], selfevo_repo: Path | None) -> bool:
    """A hypothesis qualifies as demand ONLY with measurement evidence: a
    non-empty ``evidence`` or ``metric`` field, or an ``acceptance`` text that
    references an existing repo file. Free-form musing (the boilerplate
    generator's output) has none of these and never qualifies."""
    for key in ("evidence", "metric"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return True
        if isinstance(value, (list, dict)) and value:
            return True
    acceptance = entry.get("acceptance")
    if isinstance(acceptance, str) and _acceptance_references_repo_file(acceptance, selfevo_repo):
        return True
    return False


def _hypothesis_items(state_dir: Path, selfevo_repo: Path | None) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    seen: set[str] = set()
    try:
        backlog = _read_json(Path(state_dir) / "hypotheses" / "backlog.json", None)
        entries = backlog.get("entries") if isinstance(backlog, dict) else None
        for entry in entries or []:
            if not isinstance(entry, dict):
                continue
            title = str(entry.get("task_title") or entry.get("title") or "").strip()
            if not title or title in seen:
                continue
            if not _hypothesis_has_evidence(entry, selfevo_repo):
                continue
            seen.add(title)
            evidence = str(entry.get("evidence") or entry.get("metric") or entry.get("acceptance") or "")
            items.append(_make_item("hypothesis", title, evidence))

        research = _read_json(Path(state_dir) / "research" / "hypotheses.json", None)
        if isinstance(research, list):
            for snapshot in research[:50]:
                if not isinstance(snapshot, dict):
                    continue
                for cand in snapshot.get("candidates") or []:
                    if not isinstance(cand, dict):
                        continue
                    title = str(cand.get("title") or cand.get("hypothesis") or "").strip()
                    if not title or title in seen:
                        continue
                    if not _hypothesis_has_evidence(cand, selfevo_repo):
                        continue
                    seen.add(title)
                    evidence = str(cand.get("evidence") or cand.get("metric") or cand.get("acceptance") or "")
                    items.append(_make_item("hypothesis", title, evidence))
        return items
    except Exception:
        return items


# ─── completed-demand ledger-chain done-truth (#773) ────────────────────────


def _completed_path(state_dir: Path) -> Path:
    return Path(state_dir) / "demand" / "completed.json"


def _load_completed(state_dir: Path) -> dict[str, Any]:
    data = _read_json(_completed_path(state_dir), None)
    if not isinstance(data, dict) or not isinstance(data.get("entries"), dict):
        return {"schema_version": _COMPLETED_SCHEMA, "entries": {}}
    return data


def completed_demand_ids(state_dir: Path) -> set[str]:
    """Read-only view of the completed-demand sidecar's ids (#773) — the
    ledger-chain done-truth consumed by
    ``cycle_planning.filter_completed_priorities_from_goal_text`` (lazy
    import there; this module already imports cycle_planning lazily, so
    cycle_planning must never import demand at module level). Fail-open:
    any error reads as "nothing completed"."""
    try:
        return set(_load_completed(Path(state_dir))["entries"].keys())
    except Exception:
        return set()


def _fold_completed(state_dir: Path) -> set[str]:
    """Fold (proposed(demand_id) → same-cycle terminal ``outcome: success``)
    ledger pairs into the completed-demand sidecar and return all completed
    ids (#773, live P14 evidence 2026-07-15/16).

    In demand mode the model *refines* proposal titles, so text-based
    done-evidence (git-log labels/basenames, #748/#769) structurally cannot
    retire a completed goal_text priority — the authoritative chain is the
    ledger's own ``proposed`` row carrying ``demand_id`` followed by a
    terminal ``outcome: success`` row for the same ``cycle_id``. One bounded
    pass over the CURRENT ``ledger/cycles.jsonl``; new pairs are merged into
    the sidecar append-only (existing entries are never overwritten), which
    makes done-truth rotation-proof by construction: once folded, an entry
    survives the midnight ledger rotation that blinds every single-file
    ledger reader (the #771/#772 blind spot). Fail-open: an unreadable
    ledger or sidecar degrades to whatever the sidecar already holds."""
    try:
        data = _load_completed(state_dir)
        entries: dict[str, Any] = data["entries"]
        path = Path(state_dir) / "ledger" / "cycles.jsonl"
        if not path.is_file():
            return set(entries.keys())
        demand_by_cycle: dict[str, str] = {}
        success_by_cycle: dict[str, dict[str, Any]] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if not isinstance(row, dict):
                continue
            cycle_id = str(row.get("cycle_id") or "").strip()
            if not cycle_id:
                continue
            phase = row.get("phase")
            if phase == "proposed":
                demand_id = str(row.get("demand_id") or "").strip()
                if demand_id:
                    demand_by_cycle[cycle_id] = demand_id
            elif phase == "outcome":
                if str(row.get("outcome") or "").strip().lower() == "success":
                    success_by_cycle[cycle_id] = row
        changed = False
        for cycle_id, demand_id in demand_by_cycle.items():
            if demand_id in entries:
                continue  # append-only: never overwrite an existing entry
            success = success_by_cycle.get(cycle_id)
            if success is None:
                continue  # no terminal success for this cycle — not done
            files = success.get("files_changed")
            entries[demand_id] = {
                "cycle_id": cycle_id,
                "ts": str(success.get("ts") or ""),
                "files_changed": files if isinstance(files, list) else [],
            }
            changed = True
        if changed:
            data["entries"] = entries
            _write_json(_completed_path(state_dir), data)
        return set(entries.keys())
    except Exception:
        return completed_demand_ids(state_dir)


# ─── exhaustion tracking ────────────────────────────────────────────────────


def _exhausted_path(state_dir: Path) -> Path:
    return Path(state_dir) / "demand" / "exhausted.json"


def _load_exhausted(state_dir: Path) -> dict[str, Any]:
    data = _read_json(_exhausted_path(state_dir), None)
    if not isinstance(data, dict) or not isinstance(data.get("entries"), dict):
        return {"schema_version": _EXHAUSTED_SCHEMA, "entries": {}}
    return data


def _runtime_release_id() -> str:
    """Identifier of the running runtime release (#771).

    On the eeepc host the runtime executes from
    ``/opt/eeepc-agent/runtimes/self-evolving-agent/releases/<id>/`` (the
    ``current`` symlink is on PYTHONPATH; ``resolve()`` dereferences it), so
    the path segment after ``releases`` IS the release id. Outside a release
    layout (dev checkout, tests) fall back to the product version. Empty
    string means "unknown" and never triggers a release-change reset —
    fail-open in the direction of not resetting spuriously."""
    try:
        parts = Path(__file__).resolve().parts
        for i, part in enumerate(parts[:-1]):
            if part == "releases":
                return parts[i + 1]
    except Exception:
        pass
    try:
        from nanobot import __version__

        return str(__version__)
    except Exception:
        return ""


def _latest_success_ts(state_dir: Path) -> datetime | None:
    """Timestamp of the newest terminal ``outcome: success`` ledger row, or
    ``None``. Any successful integration resets exhaustion (#771) — HEAD
    moves on integration anyway; this makes the reset explicit and immediate,
    closing the circularity where an exhausted-only demand set meant nothing
    ever integrated and HEAD never moved. Same single-pass bounded-read
    discipline as the other ledger readers; fail-open to ``None``."""
    latest: datetime | None = None
    try:
        path = Path(state_dir) / "ledger" / "cycles.jsonl"
        if not path.is_file():
            return None
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if not isinstance(row, dict) or row.get("phase") != "outcome":
                continue
            if str(row.get("outcome") or "").strip().lower() != "success":
                continue
            ts = _parse_ts(row.get("ts"))
            if ts is not None and (latest is None or ts > latest):
                latest = ts
        return latest
    except Exception:
        return latest


def _self_dedup_reject_ts_by_demand_id(state_dir: Path) -> dict[str, list[datetime]]:
    """Timestamps of ``proposer_reject``/``self_dedup`` ledger rows per
    ``demand_id`` (#762 rows extended with ``demand_id`` by #760)."""
    out: dict[str, list[datetime]] = {}
    try:
        path = Path(state_dir) / "ledger" / "cycles.jsonl"
        if not path.is_file():
            return out
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if not isinstance(row, dict):
                continue
            if row.get("phase") != "proposer_reject" or row.get("reason") != "self_dedup":
                continue
            demand_id = str(row.get("demand_id") or "").strip()
            if not demand_id:
                continue
            ts = _parse_ts(row.get("ts")) or datetime.now(timezone.utc)
            out.setdefault(demand_id, []).append(ts)
        return out
    except Exception:
        return out


def _filter_exhausted(
    state_dir: Path,
    items: list[dict[str, str]],
    head: str | None,
    *,
    now: datetime | None = None,
) -> list[dict[str, str]]:
    """Drop exhausted items; exhaustion resets (#771) on any of: a newer
    successful integration, a runtime release change, a repo HEAD move, or
    the 24h expiry.

    An expired entry becomes a ``reset`` record with ``reset_at`` so only
    self-dedup rejects NEWER than the reset count toward re-exhaustion —
    otherwise the old ledger rows would re-exhaust the item the moment its
    exhaustion expired. A MISSING entry gets the same protection (#771
    honest manual clear): only rejects newer than the newest of (last
    success outcome, 24h ago) count, so deleting ``entries`` behaves like a
    reset instead of being silently undone by stale ledger rows. Fail-open:
    any error returns ``items`` unchanged.
    """
    try:
        now = now or datetime.now(timezone.utc)
        data = _load_exhausted(state_dir)
        entries: dict[str, Any] = data["entries"]
        rejects = _self_dedup_reject_ts_by_demand_id(state_dir)
        success_ts = _latest_success_ts(state_dir)
        release = _runtime_release_id()
        now_iso = now.isoformat().replace("+00:00", "Z")
        changed = False
        out: list[dict[str, str]] = []
        for item in items:
            iid = item["id"]
            entry = entries.get(iid)
            reset_at: datetime | None = None
            if isinstance(entry, dict) and entry.get("status") == "exhausted":
                exhausted_at = _parse_ts(entry.get("exhausted_at")) or now
                head_moved = bool(
                    head and entry.get("git_head") and head != entry.get("git_head")
                )
                entry_release = str(entry.get("release") or "")
                release_changed = bool(release and entry_release and release != entry_release)
                success_reset = success_ts is not None and success_ts > exhausted_at
                expired = (
                    success_reset
                    or release_changed
                    or head_moved
                    or (now - exhausted_at) >= timedelta(hours=_EXHAUSTION_EXPIRY_HOURS)
                )
                if not expired:
                    continue  # still exhausted — item stays hidden
                reset_iso = (
                    success_ts.isoformat().replace("+00:00", "Z")
                    if success_reset and success_ts is not None
                    else now_iso
                )
                entry = {"status": "reset", "reset_at": reset_iso}
                entries[iid] = entry
                changed = True
            if isinstance(entry, dict) and entry.get("status") == "reset":
                reset_at = _parse_ts(entry.get("reset_at"))
            elif not isinstance(entry, dict):
                # No sidecar entry: fresh item OR a manual `entries` clear
                # (#771). Floor the countable rejects at 24h ago so stale
                # bug-era ledger rows cannot resurrect a cleared exhaustion.
                reset_at = now - timedelta(hours=_EXHAUSTION_EXPIRY_HOURS)
            # Any successful integration raises the floor further (#771).
            if success_ts is not None and (reset_at is None or success_ts > reset_at):
                reset_at = success_ts

            item_rejects = rejects.get(iid, [])
            if reset_at is not None:
                item_rejects = [ts for ts in item_rejects if ts > reset_at]
            if len(item_rejects) >= _EXHAUSTION_REJECTS:
                entries[iid] = {
                    "status": "exhausted",
                    "exhausted_at": now_iso,
                    "git_head": head or "",
                    "release": release,
                    "rejects": len(item_rejects),
                }
                changed = True
                continue
            out.append(item)
        if changed:
            data["entries"] = entries
            _write_json(_exhausted_path(state_dir), data)
        return out
    except Exception:
        return items


# ─── public entrypoint ──────────────────────────────────────────────────────


def collect_demand(state_dir: Path, selfevo_repo: Path | None) -> list[dict[str, str]]:
    """Collect all current demand items, trust order (priority > defect >
    hypothesis), exhausted items filtered out. Deterministic, no LLM call.
    Fail-open: any error degrades to fewer (possibly zero) items, never
    raises."""
    try:
        state_dir = Path(state_dir)
        now = datetime.now(timezone.utc)
        head = _git_head(selfevo_repo)

        items: list[dict[str, str]] = []
        seen_ids: set[str] = set()
        for batch in (
            _priority_items(state_dir, selfevo_repo),
            _ledger_defects(state_dir, now),
            _result_file_defects(state_dir, now),
            _compile_defects(state_dir, selfevo_repo, head),
            _hypothesis_items(state_dir, selfevo_repo),
        ):
            for item in batch:
                if item["id"] in seen_ids:
                    continue
                seen_ids.add(item["id"])
                items.append(item)

        # #773: ledger-chain done-truth — fold (proposed(demand_id) →
        # same-cycle success) pairs into the completed sidecar, then drop
        # completed ids from ALL demand kinds BEFORE the exhausted filter:
        # a completed item needs no exhaustion bookkeeping at all (this
        # also supersedes #772's success-reset blind spot for these items).
        completed = _fold_completed(state_dir)
        if completed:
            items = [item for item in items if item["id"] not in completed]

        return _filter_exhausted(state_dir, items, head, now=now)
    except Exception:
        return []
