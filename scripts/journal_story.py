"""Deterministic journal-to-story preparation and provenance validation (#1622).

This module owns the harness-side trust boundary for narration. It reduces
ledger rows to bounded beats, carries the source line and sign into every
beat, builds a model prompt from those beats, and validates a model-shaped
response before an artifact can be assembled. It does not choose work,
render video, or publish.

Increment 2 (:func:`run_narrator_job`) adds the one piece increment 1/1b
deliberately left out: an actual model call, through the same gateway
client the strategist uses (:func:`nanobot.runtime.strategist._default_llm`
pattern -- ``LITELLM_BASE_URL``/``LITELLM_API_KEY`` from the unit
environment, bounded retries, a fixed timeout), and the write of the
resulting artifact to ``state/story/<day>.json``. This is a job, not a
service -- no publishing, no video, no channel figure anywhere in this
module (ADR-016 rule 1's call-graph constraint: the narrator has no reader
for any channel figure, and no figure is a parameter here for it to have
one).

Increment 3 (#1820) is the part that makes the job actually happen: a
``main`` entry point defaulting to yesterday UTC, a run journal written on
every path so a day nobody narrated cannot be mistaken for a quiet one, and
an ADR-026 stage record on every artifact. The schedule itself lives beside
it as ``host/eeepc/systemd/eeebot-narrator.{service,timer}`` -- still no
rendering and no publishing, so the stage this job reaches is always
``none``, recorded rather than omitted.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import sys
import tempfile
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from nanobot.runtime import day_key, state_access

SCHEMA_VERSION = "journal-story-v1"
JOB_SCHEMA_VERSION = "journal-story-job-v1"
PRODUCER = "scripts.journal_story"
DEFAULT_MAX_BEATS = 8
# ADR-026 decision 3: the day's deliverable is done in three stages, and only
# the last counts toward the charter's top rung. The narrator produces none of
# them -- a story artifact is text a renderer has not consumed yet -- so every
# run this module makes records `reached: "none"`. That is the intended first
# honest report ("the earliest days end at stage reached: none"), not a defect
# to route around, and it is recorded rather than omitted so the gap is
# countable. `published`/`observed` are "unknown", never False and never 0:
# ADR-026 "an observation is never inferred" and "`unknown` is never rendered
# as zero or as success". When a render pipeline exists it, not this module,
# is what moves `rendered` to True.
STAGES = ("rendered", "published", "observed")
STAGE_NONE = "none"
STAGE_UNKNOWN = "unknown"
# One row per invocation, whatever happened. The artifact alone cannot answer
# "did the job run?": a day with no beats writes an artifact that looks much
# like a quiet day, and a day the timer never fired writes nothing at all --
# the same absence a day with beats and a dead gateway would leave if the
# write itself failed. The run journal is what makes those distinguishable
# (#1820 AC 4 and AC 6).
RUNS_SUBPATH = ("story", "runs.jsonl")
# Narrator-specific override, falling back to the strategist role's own
# resolved model (env vars, then its built-in default) -- #1622's own
# instruction: "model from an env var with the strategist's default". No
# new model_registry.py role: this reuses "strategist" via resolve_model's
# `explicit` precedence rather than registering a parallel one.
NARRATOR_MODEL_ENV = "SELFEVO_NARRATOR_MODEL"
NARRATOR_MAX_RETRIES_ENV = "SELFEVO_NARRATOR_MAX_RETRIES"
DEFAULT_MAX_RETRIES = 3
# The journal source, named once (#1622 comment 2026-09-16): the cycle ledger
# under ``state/ledger`` — the live ``cycles.jsonl`` plus the dated
# ``cycles-YYYY-MM-DD.jsonl.gz`` archives the midnight rotation writes. Never
# ``state/reflector/reflections.jsonl``: that is model-written prose and may
# not supply a beat's sign or existence (ADR-015 rule 1).
LEDGER_SOURCE = "state/ledger"
_LIVE_LEDGER = "cycles.jsonl"
_MAX_LINE_BYTES = 256 * 1024
_UNKNOWN_MARKERS = frozenset({"probe_unavailable", "unavailable", "unknown"})
_POSITIVE_WORDS = frozenset({"fixed", "working", "succeeded", "success", "improved", "resolved"})
_NEGATIVE_WORDS = frozenset({"failed", "failure", "broken", "regressed", "rejected"})
_CONFIDENT_UNKNOWN_WORDS = frozenset({"finally", "clearly", "definitely", "fixed", "improved", "resolved", "succeeded"})


class StoryValidationError(ValueError):
    """Narration cannot be traced to the deterministic beat set."""


def _text(value: Any) -> str:
    return str(value or "").strip()


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _sign(row: dict[str, Any]) -> str:
    """Return the sign from the row only; prose never participates here."""
    values = {
        _text(row.get("outcome")).lower(),
        _text(row.get("status")).lower(),
        _text(row.get("reason")).lower(),
        _text(row.get("verdict")).lower(),
    }
    if values & _UNKNOWN_MARKERS or any("probe_unavailable" in value for value in values):
        return "unknown"
    outcome = _text(row.get("outcome")).lower()
    if outcome == "success":
        return "worked"
    if outcome in {"failed", "failure", "error"}:
        return "failed"
    if outcome.startswith("skipped") or outcome in {"idle", "unchanged"}:
        return "unchanged"
    if outcome == "partial":
        return "unknown"
    return "unknown"


def _event_text(row: dict[str, Any]) -> str:
    for key in ("task_title", "task", "reason", "expected_outcome_claim", "serves"):
        value = _text(row.get(key))
        if value:
            return value
    return ""


def _source(row: dict[str, Any]) -> dict[str, Any]:
    source_file = _text(row.get("_source_file"))
    source_line = row.get("_source_line")
    if not source_file or not isinstance(source_line, int):
        raise StoryValidationError("every selected row needs _source_file and _source_line")
    return {
        "file": source_file,
        "line": source_line,
        "cycle_id": _text(row.get("cycle_id")) or None,
        "phase": _text(row.get("phase")) or None,
    }


def _day_files(
    ledger_dir: Path, day: date, *, local_tz: Any = None
) -> tuple[list[Path], list[str]]:
    """Files that can hold rows stamped ``day``: neighbouring archives + live file.

    ADR-029 (#1831) step 3: resolves bounds across both UTC- and local-keyed
    archives through :func:`nanobot.runtime.state_access._ledger_sources`.
    """
    day_str = day.isoformat()
    cutover = day_key.load_cutover(day_key.cutover_marker_path(ledger_dir))
    start_utc, end_utc = day_key.archive_day_bounds(day_str, cutover, local_tz=local_tz)
    since = start_utc - timedelta(days=1)
    sources, notes = state_access._ledger_sources(ledger_dir, since, local_tz=local_tz)
    active = [p for p in sources if p.name == _LIVE_LEDGER]
    archives = []
    for p in sources:
        if p.name == _LIVE_LEDGER:
            continue
        m = state_access._DAY_RE.match(p.name)
        if m:
            a_start, a_end = day_key.archive_day_bounds(m.group(1), cutover, local_tz=local_tz)
            if a_start < end_utc and a_end >= since:
                archives.append(p)
    archives.sort(key=lambda p: p.name)
    return archives + active, notes


def load_day_journal(state_dir: str | Path, day: str, *, local_tz: Any = None) -> dict[str, Any]:
    """Read one day of ledger rows through the rotation-aware path.

    Every returned row carries ``_source_file`` and ``_source_line`` so a beat
    can cite it. Unreadable lines and unreadable files are reported, not
    dropped: ``status`` is ``incomplete`` whenever any row of the day may be
    missing, so a thin day (``complete``, few rows) and a partly unreadable
    day never look alike (ADR-015 rule 5). No model is involved.

    A ``proposed`` row is returned regardless of its own day, since it only
    ever supplies a title (#1841) -- a cycle straddling midnight can have its
    ``proposed`` row dated yesterday and its ``outcome`` row dated today.
    Every other row is still bound to ``day`` exactly.
    """
    requested = date.fromisoformat(day)
    ledger_dir = Path(state_dir) / "ledger"
    rows: list[dict[str, Any]] = []
    unreadable: list[dict[str, Any]] = []
    files_read: list[str] = []
    notes: list[str] = []
    if not ledger_dir.is_dir():
        notes.append("ledger_dir_missing")
        return {
            "source": LEDGER_SOURCE,
            "day": requested.isoformat(),
            "status": "incomplete",
            "files_read": [],
            "rows": [],
            "unreadable": [],
            "notes": notes,
        }
    cutover = day_key.load_cutover(day_key.cutover_marker_path(ledger_dir))
    start_utc, end_utc = day_key.archive_day_bounds(day, cutover, local_tz=local_tz)
    day_paths, source_notes = _day_files(ledger_dir, requested, local_tz=local_tz)
    notes.extend(source_notes)
    for path in day_paths:
        if not path.is_file():
            continue
        opener = gzip.open if path.name.endswith(".gz") else open
        try:
            with opener(path, "rt", encoding="utf-8", errors="replace") as handle:
                for line_no, line in enumerate(handle, start=1):
                    if not line.strip():
                        continue
                    if len(line) > _MAX_LINE_BYTES:
                        unreadable.append({"file": path.name, "line": line_no, "reason": "line_too_long"})
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        unreadable.append({"file": path.name, "line": line_no, "reason": "malformed_json"})
                        continue
                    if not isinstance(row, dict):
                        unreadable.append({"file": path.name, "line": line_no, "reason": "not_an_object"})
                        continue
                    stamp = _parse_ts(row.get("ts"))
                    # #1841: a ``proposed`` row only ever supplies a title
                    # (keyed by cycle_id, in select_beats) -- it is never
                    # itself an event. A cycle whose ``proposed`` row falls
                    # the day before its ``outcome`` row still needs that
                    # title to reach select_beats, so title rows are exempt
                    # from the day match; the file window above (day-1/0/+1)
                    # already bounds how far a title can drift. Event rows
                    # (every other phase) keep the strict day match: a
                    # neighbouring day's events must never enter this day's
                    # beats.
                    if stamp is None:
                        continue
                    if row.get("phase") == "proposed":
                        if stamp >= end_utc:
                            continue
                    elif not (start_utc <= stamp < end_utc):
                        continue
                    rows.append({**row, "_source_file": path.name, "_source_line": line_no})
        except PermissionError:
            notes.append(f"permission:{path.name}")
            continue
        except (OSError, EOFError, gzip.BadGzipFile):
            notes.append(f"unreadable_file:{path.name}")
            continue
        files_read.append(path.name)
    rows.sort(key=lambda row: (_parse_ts(row.get("ts")) or datetime.min.replace(tzinfo=timezone.utc), row["_source_file"], row["_source_line"]))
    status = "incomplete" if unreadable or notes else "complete"
    return {
        "source": LEDGER_SOURCE,
        "day": requested.isoformat(),
        "status": status,
        "files_read": files_read,
        "rows": rows,
        "unreadable": unreadable,
        "notes": notes,
    }


def select_beats(
    rows: Iterable[dict[str, Any]],
    *,
    day: str | None = None,
    max_beats: int = DEFAULT_MAX_BEATS,
    cutover_utc: Any = day_key._UNSET,
    local_tz: Any = None,
) -> list[dict[str, Any]]:
    """Select an ordered, deterministic, ledger-backed beat set.

    Terminal outcome rows are the event source. One row per cycle is retained
    — the cycle's *last* outcome row, because a retried cycle writes several
    and the last one is its terminal sign — sorted by timestamp and source
    location. When an outcome row carries no event text, the same cycle's
    ``proposed`` row supplies the title; the sign is never read from it. A
    quiet day therefore returns an empty list rather than padding material
    into a story.
    """
    if max_beats <= 0:
        raise ValueError("max_beats must be positive")
    bounds = (
        day_key.archive_day_bounds(
            day,
            day_key.CUTOVER_UTC if cutover_utc is day_key._UNSET else cutover_utc,
            local_tz=local_tz,
        )
        if day is not None
        else None
    )
    candidates: list[tuple[datetime, str, int, dict[str, Any]]] = []
    titles: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("phase") == "proposed":
            cycle_id, title = _text(row.get("cycle_id")), _text(row.get("task_title"))
            if cycle_id and title:
                titles.setdefault(cycle_id, title)
            continue
        if row.get("phase") != "outcome":
            continue
        timestamp = _parse_ts(row.get("ts"))
        if timestamp is None:
            continue
        if bounds is not None and not (bounds[0] <= timestamp < bounds[1]):
            continue
        try:
            source = _source(row)
        except StoryValidationError:
            continue
        candidates.append((timestamp, source["file"], source["line"], row))
    candidates.sort(key=lambda item: (item[0], item[1], item[2]))
    last_index: dict[str, int] = {}
    for index, (_timestamp, _file, _line, row) in enumerate(candidates):
        cycle_id = _text(row.get("cycle_id"))
        if cycle_id:
            last_index[cycle_id] = index
    beats: list[dict[str, Any]] = []
    for index, (_timestamp, _file, _line, row) in enumerate(candidates):
        cycle_id = _text(row.get("cycle_id"))
        if cycle_id and last_index[cycle_id] != index:
            continue
        source = _source(row)
        event = _event_text(row) or titles.get(cycle_id) or f"{_text(row.get('phase')) or 'journal'} event"
        beats.append({
            "beat_id": f"beat-{len(beats) + 1:03d}",
            "event": event,
            "sign": _sign(row),
            "cost": row.get("duration_ms") if isinstance(row.get("duration_ms"), (int, float)) else None,
            "source": source,
        })
        if len(beats) >= max_beats:
            break
    return beats


def build_story_prompt(beats: list[dict[str, Any]], glossary: dict[str, str] | None = None) -> str:
    """Build the only model input: selected beats, their signs and citations."""
    payload = {
        "instruction": "Write plain warm narration from these beats only. Do not add events.",
        "sign_rule": "The supplied sign is authoritative; preserve it exactly.",
        "glossary": glossary or {},
        "beats": beats,
        "response_shape": "list of {beat_id, text, sign, terms}",
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def _contains_any(text: str, words: frozenset[str]) -> bool:
    lowered = text.casefold()
    return any(re.search(rf"\b{re.escape(word)}\b", lowered) for word in words)


def _words(text: str) -> frozenset[str]:
    """Casefolded word tokens in *text* (#1840).

    Used to subtract a beat's own vocabulary from a forbidden-word list
    before matching the narration against it. A word the beat's own event
    text already carries is an echo of the source, not an invented tone —
    beat-007's event text ("Add failure search guidance...") names
    ``failure`` as the SUBJECT of the change; forbidding the narration from
    using that same word made faithful retelling impossible (#1840).
    """
    return frozenset(re.findall(r"\w+", text.casefold()))


def validate_narration(
    narration: Any,
    beats: list[dict[str, Any]],
    *,
    glossary: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Reject uncited, sign-drifting, unknown, or undefined-term claims."""
    if not isinstance(narration, list) or not narration:
        raise StoryValidationError("narration must be a non-empty list")
    beat_map = {str(beat.get("beat_id")): beat for beat in beats}
    allowed_terms = set((glossary or {}).keys())
    validated: list[dict[str, Any]] = []
    for item in narration:
        if not isinstance(item, dict):
            raise StoryValidationError("each narration item must be an object")
        beat_id = _text(item.get("beat_id"))
        if beat_id not in beat_map:
            raise StoryValidationError(f"narration item has no selected beat: {beat_id or 'missing'}")
        text = _text(item.get("text"))
        if not text:
            raise StoryValidationError(f"narration item {beat_id} is empty")
        expected = str(beat_map[beat_id]["sign"])
        claimed = _text(item.get("sign"))
        if claimed != expected:
            raise StoryValidationError(f"sign drift for {beat_id}: expected {expected}, got {claimed or 'missing'}")
        # #1840: a word the beat's own event text already carries is an
        # echo of the source, never an invented tone -- subtract the
        # beat's own vocabulary from each forbidden set before matching.
        # beat-007's event text ("Add failure search guidance...") makes
        # "failure" the SUBJECT of the change; a faithful retelling that
        # reuses the word must not be treated the same as a narration that
        # invents a negative/positive/confident word absent from its beat.
        beat_words = _words(str(beat_map[beat_id].get("event") or ""))
        if expected == "failed" and _contains_any(text, _POSITIVE_WORDS - beat_words):
            raise StoryValidationError(f"positive wording contradicts failed beat {beat_id}")
        if expected == "worked" and _contains_any(text, _NEGATIVE_WORDS - beat_words):
            raise StoryValidationError(f"negative wording contradicts worked beat {beat_id}")
        if expected == "unknown" and _contains_any(text, _CONFIDENT_UNKNOWN_WORDS - beat_words):
            raise StoryValidationError(f"confident wording contradicts unknown beat {beat_id}")
        terms = item.get("terms", [])
        if not isinstance(terms, list) or any(str(term) not in allowed_terms for term in terms):
            raise StoryValidationError(f"undefined glossary term in beat {beat_id}")
        validated.append({"beat_id": beat_id, "text": text, "sign": expected, "terms": [str(term) for term in terms]})
    return validated


def assemble_story_artifact(
    beats: list[dict[str, Any]],
    narration: Any,
    *,
    prompt: str,
    model_output: Any,
    glossary: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Validate and assemble the durable story artifact."""
    validated = validate_narration(narration, beats, glossary=glossary)
    return {
        "schema_version": SCHEMA_VERSION,
        "producer": PRODUCER,
        "beats": beats,
        "narration": validated,
        "prompt": prompt,
        "model_output": model_output,
        "citation_set": {item["beat_id"]: next(beat["source"] for beat in beats if beat["beat_id"] == item["beat_id"]) for item in validated},
    }


# ─── increment 2: one runnable job ──────────────────────────────────────────


def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return default


def _narrator_system_prompt() -> str:
    """#1729 (ADR-022 rule 2): the narrator's role text lives in
    ``roles/narrator.md`` at the release root, with identity (short form) and
    soul prepended. ADR-016 rule 1 still holds by construction: the assembler
    reads those release files and nothing else, so it has no reader for any
    channel figure."""
    from nanobot.runtime.role_prompt import build_role_system_prompt

    return build_role_system_prompt("narrator")[0]


def _default_narrator_llm(messages: list[dict[str, str]], model: str) -> str:
    """The strategist's own gateway-client pattern
    (:func:`nanobot.runtime.strategist._default_llm`), reused verbatim:
    ``LITELLM_BASE_URL``/``LITELLM_API_KEY`` from the unit's own
    environment, a fixed timeout, bounded retries. Telemetry is recorded
    the same way, under the ``narrator`` component name."""
    from openai import OpenAI

    from nanobot.observability.llm_telemetry import call_context, record_llm_call, record_llm_prompt
    from nanobot.runtime.role_prompt import system_chars

    base_url = os.environ.get("LITELLM_BASE_URL", "").strip()
    api_key = os.environ.get("LITELLM_API_KEY", "").strip()
    if not base_url or not api_key:
        raise RuntimeError("litellm credentials not configured; check the unit EnvironmentFile chain")
    started = time.monotonic()
    max_retries = _env_int(NARRATOR_MAX_RETRIES_ENV, DEFAULT_MAX_RETRIES)
    response = OpenAI(base_url=base_url, api_key=api_key, timeout=120, max_retries=max_retries).chat.completions.create(
        model=model, messages=messages, max_tokens=2_000, temperature=0.3
    )
    choice = response.choices[0]
    content = getattr(getattr(choice, "message", None), "content", "") or ""
    usage_obj = getattr(response, "usage", None)
    usage = {key: int(getattr(usage_obj, key, 0) or 0) for key in ("prompt_tokens", "completion_tokens", "total_tokens")}
    # The narrator runs as its own job (increment 3's future timer, or an
    # operator invocation), not inside a bridge cycle -- no cycle_id to
    # attribute, same reasoning as the strategist's own call site.
    with call_context(None, "narrator"):
        record_llm_call(model=model, duration_ms=(time.monotonic() - started) * 1000, usage=usage,
                        finish_reason=getattr(choice, "finish_reason", ""), retries=0,
                        system_prompt_chars=system_chars(messages))
        record_llm_prompt(messages=messages, content=content, reasoning_content=None,
                          finish_reason=getattr(choice, "finish_reason", ""), model=model,
                          prompt_tokens=usage["prompt_tokens"], completion_tokens=usage["completion_tokens"])
    return content


def _resolve_narrator_model() -> str:
    from nanobot.runtime.model_registry import resolve_model

    explicit = os.environ.get(NARRATOR_MODEL_ENV, "").strip() or None
    return resolve_model("strategist", explicit=explicit, strip_openai=True)


def _parse_model_output(raw: Any) -> Any:
    """Best-effort single parse of the model's response -- no retry, no
    second guess at the shape. A JSON list is returned as-is; a single pair
    of markdown code fences is stripped once (a common small-model habit,
    not a correction of content); anything else is returned unparsed so
    :func:`validate_narration` (or the job's own not-a-list check) reports
    it as a real violation instead of masking a malformed response as an
    empty one.
    """
    if hasattr(raw, "content"):
        raw = raw.content
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        if text.endswith("```"):
            text = text[: -3]
        text = text.strip()
    try:
        return json.loads(text)
    except ValueError:
        return text


def _write_story_artifact(state_dir: str | Path, day: str, artifact: dict[str, Any]) -> Path:
    """Write ``state/story/<day>.json`` atomically (tmp + ``os.replace``),
    the same pattern :func:`nanobot.runtime.strategist._atomic_json` uses."""
    path = Path(state_dir) / "story" / f"{day}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(artifact, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != "nt":
            os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return path


def run_narrator_job(
    state_dir: str | Path,
    day: str,
    *,
    llm: "Callable[[list[dict[str, str]], str], Any] | None" = None,
    glossary: dict[str, str] | None = None,
    max_beats: int = DEFAULT_MAX_BEATS,
    local_tz: Any = None,
) -> dict[str, Any]:
    """One runnable job, increment 2: load the day, select beats, prompt one
    model through the strategist's own gateway pattern, validate, and write
    ``state/story/<day>.json``.

    A day with no beats at all never reaches the model -- there is nothing
    for it to narrate, and every item :func:`validate_narration` could
    receive would fail the "no selected beat" check trivially, so it is not
    a real attempt, just a guaranteed-empty round trip. That day's artifact
    is written directly with ``status: "ok"`` and empty narration: a quiet
    day is still an honest one (ADR-016), and it costs nothing to say so.

    A rejected narration (:class:`StoryValidationError`) is written too --
    ``status: "rejected"`` plus the violation -- never silently dropped. This
    is the content gate doing its job: a completed run with a negative
    verdict, not a failure of the machine (#1842). Anything that keeps the
    job from reaching a verdict at all -- a gateway error, a parse failure
    before validation ran, an unwritable artifact -- is a distinct outcome,
    ``status: "error"``, so the two questions ("was the narration
    publishable" and "did the job run") stay answerable from the status
    field alone. There is no retry: at most the one model call this function
    makes, ever, per invocation. Never raises; a failure at any stage before
    the artifact is written is itself recorded as a rejected/error artifact.
    """
    generated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    journal = load_day_journal(state_dir, day, local_tz=local_tz)
    ledger_cutover = day_key.load_cutover(day_key.cutover_marker_path(Path(state_dir) / "ledger"))
    beats = select_beats(journal["rows"], day=day, max_beats=max_beats, cutover_utc=ledger_cutover, local_tz=local_tz)
    base: dict[str, Any] = {
        "schema_version": JOB_SCHEMA_VERSION,
        "producer": PRODUCER,
        "day": day,
        "generated_at": generated_at,
        "journal_status": journal["status"],
        "journal_notes": journal["notes"],
        "beats": beats,
    }
    if not beats:
        no_beats_result = {
            **base, "status": "ok", "model": None, "prompt": "", "model_output": None,
            "narration": [], "citation_set": {}, "violations": [],
            "note": "no beats recorded for this day; no model call made",
        }
        return _finish(state_dir, day, no_beats_result)
    model = _resolve_narrator_model()
    prompt = build_story_prompt(beats, glossary=glossary)
    messages = [
        {"role": "system", "content": _narrator_system_prompt()},
        {"role": "user", "content": prompt},
    ]
    raw: Any = None
    try:
        raw = (llm or _default_narrator_llm)(messages, model)
        narration = _parse_model_output(raw)
        artifact = assemble_story_artifact(beats, narration, prompt=prompt, model_output=raw, glossary=glossary)
        result = {**base, **artifact, "status": "ok", "model": model, "violations": []}
    except StoryValidationError as exc:
        result = {
            **base, "status": "rejected", "model": model, "prompt": prompt,
            "model_output": raw,
            "narration": [], "citation_set": {}, "violations": [str(exc)],
        }
    except Exception as exc:  # gateway/parse failure before validation ran at all -- the job did not run
        result = {
            **base, "status": "error", "model": model, "prompt": prompt,
            "model_output": None, "narration": [], "citation_set": {},
            "violations": [f"{exc.__class__.__name__}: {exc}"],
        }
    return _finish(state_dir, day, result)


# ---------------------------------------------------------------------------
# increment 3: the stage record, the run journal, and the daily entry point
# ---------------------------------------------------------------------------

def stage_record() -> dict[str, Any]:
    """What stage the day's deliverable reached, recorded rather than omitted.

    Always ``none`` from here. This module writes a story artifact; ADR-026's
    `rendered` wants "a sequence exists as an artifact on the host, produced
    by the loop's own pipeline", and no such pipeline exists. Saying so is the
    point: a recorded `none` is countable, an absent field is not.
    """
    return {
        "reached": STAGE_NONE,
        "rendered": False,
        "published": STAGE_UNKNOWN,
        "observed": STAGE_UNKNOWN,
        "note": (
            "narration only; no render pipeline exists, and no publish or "
            "observation channel exists to report on (ADR-026 decision 3)"
        ),
    }


def _read_consecutive_rejected_streak(state_dir: str | Path) -> int:
    """Read existing runs.jsonl to count the trailing streak of rejected runs."""
    path = Path(state_dir).joinpath(*RUNS_SUBPATH)
    if not path.is_file():
        return 0
    streak = 0
    try:
        with path.open("r", encoding="utf-8") as handle:
            lines = [line.strip() for line in handle if line.strip()]
        for line in reversed(lines):
            try:
                row = json.loads(line)
            except Exception:
                break
            if row.get("status") == "rejected":
                streak += 1
            else:
                break
    except Exception:
        return 0
    return streak


def _append_run_row(state_dir: str | Path, row: dict[str, Any]) -> None:
    """Append one line to ``state/story/runs.jsonl``. Never raises: a journal
    failure must not turn a completed run into a crashed one, and the crash
    would itself be the silence this journal exists to remove."""
    path = Path(state_dir).joinpath(*RUNS_SUBPATH)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    except Exception as exc:  # pragma: no cover - reported, not hidden
        print(f"narrator: run journal append failed: {exc!r}", file=sys.stderr)


def _finish(state_dir: str | Path, day: str, result: dict[str, Any]) -> dict[str, Any]:
    """Stamp the stage, write the artifact, and journal the run -- on every
    path, including the one where the artifact write itself fails. Without the
    last case a failed write is indistinguishable from a timer that never
    fired, which is exactly the confusion #1820 exists to end."""
    result["stage"] = stage_record()
    try:
        result["artifact_path"] = str(_write_story_artifact(state_dir, day, result))
    except Exception as exc:
        result["status"] = "error"  # the job could not do its work -- not a content-gate verdict
        result["artifact_path"] = None
        result.setdefault("violations", [])
        result["violations"] = list(result["violations"]) + [
            f"artifact_write_failed: {exc.__class__.__name__}: {exc}"
        ]
    prior_streak = _read_consecutive_rejected_streak(state_dir)
    status = result.get("status")
    consecutive_rejected = (prior_streak + 1) if status == "rejected" else 0
    result["consecutive_rejected"] = consecutive_rejected

    _append_run_row(
        state_dir,
        {
            "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "producer": PRODUCER,
            "day": day,
            "status": status,
            "beats": len(result.get("beats") or []),
            "model": result.get("model"),
            "journal_status": result.get("journal_status"),
            "violations": list(result.get("violations") or []),
            "artifact_path": result.get("artifact_path"),
            "stage_reached": result["stage"]["reached"],
            "consecutive_rejected": consecutive_rejected,
        },
    )
    return result


def default_day(now: datetime | None = None, *, local_tz: Any = None) -> str:
    """Yesterday, in host-local calendar (ADR-029). Closed at any local hour
    the timer could fire: the day before today is over by definition the moment
    today begins.
    """
    today_key = day_key.day_key(now, local_tz=local_tz)
    today_date = date.fromisoformat(today_key)
    return (today_date - timedelta(days=1)).isoformat()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="journal_story",
        description="Narrate one closed day into state/story/<day>.json (#1820).",
    )
    parser.add_argument(
        "--state-root",
        required=True,
        help="state directory, e.g. /var/lib/eeepc-agent/self-evolving-agent/state",
    )
    parser.add_argument("--day", default=None, help="YYYY-MM-DD; defaults to yesterday in host-local calendar")
    parser.add_argument("--max-beats", type=int, default=DEFAULT_MAX_BEATS)
    args = parser.parse_args(argv)
    day = args.day or default_day()
    result = run_narrator_job(args.state_root, day, max_beats=args.max_beats)
    print(
        json.dumps(
            {
                "day": day,
                "status": result.get("status"),
                "beats": len(result.get("beats") or []),
                "stage_reached": result["stage"]["reached"],
                "artifact_path": result.get("artifact_path"),
                "violations": result.get("violations") or [],
                "consecutive_rejected": result.get("consecutive_rejected", 0),
            },
            ensure_ascii=False,
        )
    )
    # Exit code answers "did the job run", not "was the artifact publishable"
    # (#1842). A validator rejection is a completed run with a negative
    # verdict -- ordinary, expected, and not a reason for systemd to mark the
    # unit failed. Only "error" -- the job could not do its work at all --
    # exits non-zero, so a dead gateway does not read as more of the same
    # once rejections are routine.
    return 0 if result.get("status") in ("ok", "rejected") else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
