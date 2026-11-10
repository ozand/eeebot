"""Bounded in-process provenance for approved system-prompt sections.

Only callers that explicitly pass named sections opt in. Section content is
never persisted; only allowlisted digests and lengths enter telemetry.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import threading
import uuid
from collections import OrderedDict
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any, Iterator, Sequence

SCHEMA_VERSION = 1
MAX_BASELINES = 64
MAX_OBJECT_BYTES = 8 * 1024
MAX_SECTIONS = 32
MAX_NAME_CHARS = 48
MAX_OWNER_CHARS = 128
MAX_PRODUCER_CHARS = 64

# Only fixed role-file bodies for selected production roles are approved.
# Identity, soul, charter, workspace, memory, task and runtime material is
# deliberately absent. Tests pin these exact producers/files.
SAFE_ROLE_PRODUCERS = frozenset({
    "proposer", "demand-proposer", "goal-review", "planner", "reflector",
    "strategist", "curator", "narrator",
})

_CURRENT: ContextVar[dict[str, Any] | None] = ContextVar("_prompt_call", default=None)
_FAILED: ContextVar[dict[str, Any] | None] = ContextVar("_prompt_failed_call", default=None)

_OWNER: ContextVar[str | None] = ContextVar("_prompt_owner", default=None)
_ROLE_ASSEMBLY: ContextVar[dict[str, Any] | None] = ContextVar("_role_prompt_assembly", default=None)
_BASELINES: OrderedDict[tuple[str, str], dict[str, Any]] = OrderedDict()
_LOCK = threading.RLock()


@dataclass(frozen=True)
class PromptSection:
    name: str
    content: str
    evidence_safe: bool = False


@dataclass(frozen=True)
class PromptCall:
    call_id: str
    provenance: dict[str, Any]


@dataclass(frozen=True)
class PromptRequest:
    owner: str
    producer: str
    schema: str
    sections: tuple[PromptSection, ...]
    expected_system: str | None = None
    release_id: str | None = None


_REQUEST: ContextVar[PromptRequest | None] = ContextVar("_prompt_request", default=None)


def _bounded(value: str, limit: int) -> str:
    return str(value or "")[:limit]


def observe_prompt(
    *, owner: str, producer: str, schema: str, sections: Sequence[PromptSection],
    release_id: str | None = None, actual_system: str | None = None,
    expected_system: str | None = None, call_id: str | None = None,
) -> PromptCall:
    """Create one logical call ID and bounded snapshot/delta at dispatch."""
    call_id = call_id or uuid.uuid4().hex
    matches = actual_system is None or expected_system == actual_system
    safe_entries, order = _safe_entries(producer, sections if matches else ())
    owner = _bounded(owner, MAX_OWNER_CHARS) or "unknown"
    producer = _bounded(producer, MAX_PRODUCER_CHARS) or "unknown"
    schema = _bounded(schema, MAX_PRODUCER_CHARS) or "unknown"
    release = _bounded(release_id, MAX_PRODUCER_CHARS) or None
    key = (owner, f"{producer}:{schema}")
    with _LOCK:
        previous = _BASELINES.get(key)
        same_release_state = previous is not None and previous.get("release_id") == release
        if previous is None or previous.get("schema") != schema or not same_release_state:
            baseline_id = uuid.uuid4().hex
            kind = "snapshot"
            previous_call_id = None
            delta = safe_entries
        else:
            baseline_id = previous["baseline_id"]
            kind = "delta"
            previous_call_id = previous["call_id"]
            before = {entry["name"]: entry for entry in previous["sections"]}
            after = {entry["name"]: entry for entry in safe_entries}
            delta = [entry for name, entry in after.items() if before.get(name) != entry]
            delta.extend(
                {"name": name, "removed": True}
                for name in before if name not in after
            )
        _BASELINES.pop(key, None)
        _BASELINES[key] = {
            "baseline_id": baseline_id, "call_id": call_id, "schema": schema,
            "release_id": release, "sections": safe_entries,
        }
        evicted = []
        while len(_BASELINES) > MAX_BASELINES:
            evicted.append(_BASELINES.popitem(last=False)[0])
    provenance: dict[str, Any] = {
        "version": SCHEMA_VERSION,
        "call_id": call_id,
        "baseline_id": baseline_id,
        "kind": kind,
        "producer": producer,
        "schema": schema,
        "order": order,
        "sections": delta,
        "previous_call_id": previous_call_id,
        "release_id": release,
    }
    if evicted:
        provenance["baseline_evicted"] = True
    provenance["provenance_state"] = "observed" if safe_entries else "unavailable"
    if len(json.dumps(provenance, separators=(",", ":")).encode("utf-8")) > MAX_OBJECT_BYTES:
        provenance = {"version": SCHEMA_VERSION, "call_id": call_id, "provenance_state": "unavailable"}
    return PromptCall(call_id=call_id, provenance=provenance)


def make_role_request(
    *, role: str, owner: str, sections: Sequence[PromptSection],
    expected_system: str, release_id: str | None = None,
) -> PromptRequest:
    return PromptRequest(
        owner=owner, producer=role, schema=f"role-prompt-v1:{role}",
        sections=tuple(sections), expected_system=expected_system,
        release_id=release_id,
    )


def attach_current(provenance: dict[str, Any]) -> Token:
    """Make this invocation available to adjacent best-effort recorders."""
    return _CURRENT.set(provenance)


def reset_current(token: Token) -> None:
    with contextlib.suppress(Exception):
        _CURRENT.reset(token)


def current_provenance() -> dict[str, Any] | None:
    current = _CURRENT.get()
    return sanitize_provenance(current)


def take_failed_provenance() -> dict[str, Any] | None:
    current = _FAILED.get()
    _FAILED.set(None)
    return sanitize_provenance(current)


def sanitize_provenance(value: Any) -> dict[str, Any] | None:
    """Rebuild only the bounded schema this module emits; never serialize caller data."""
    if not isinstance(value, dict):
        return None
    allowed = {
        "version", "call_id", "baseline_id", "kind", "producer", "schema",
        "order", "sections", "previous_call_id", "release_id", "baseline_evicted",
        "provenance_state",
    }
    clean = {key: value[key] for key in allowed if key in value}
    order = clean.get("order")
    clean["order"] = [str(name)[:MAX_NAME_CHARS] for name in order[:MAX_SECTIONS]] if isinstance(order, list) else []
    entries = clean.get("sections")
    bounded_entries: list[dict[str, Any]] = []
    if isinstance(entries, list):
        for entry in entries[:MAX_SECTIONS]:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "")[:MAX_NAME_CHARS]
            if "removed" in entry:
                bounded_entries.append({"name": name, "removed": True})
            else:
                digest = str(entry.get("sha256") or "")
                chars = entry.get("chars")
                if len(digest) == 64 and all(c in "0123456789abcdef" for c in digest) and isinstance(chars, int) and not isinstance(chars, bool) and chars >= 0:
                    bounded_entries.append({"name": name, "sha256": digest, "chars": chars})
    clean["sections"] = bounded_entries
    for key, limit in (("call_id", 32), ("baseline_id", 32), ("previous_call_id", 32), ("producer", MAX_PRODUCER_CHARS), ("schema", MAX_PRODUCER_CHARS), ("release_id", MAX_PRODUCER_CHARS)):
        if key in clean and clean[key] is not None:
            clean[key] = str(clean[key])[:limit]
    if clean.get("kind") not in {"snapshot", "delta"}:
        clean["kind"] = "unavailable"
    if clean.get("provenance_state") not in {"observed", "unavailable"}:
        clean["provenance_state"] = "unavailable"
    if len(json.dumps(clean, separators=(",", ":")).encode("utf-8")) > MAX_OBJECT_BYTES:
        return {"version": SCHEMA_VERSION, "call_id": clean.get("call_id", ""), "provenance_state": "unavailable"}
    return clean


def current_owner() -> str | None:
    return _OWNER.get()


def current_request() -> PromptRequest | None:
    request = _REQUEST.get()
    return request if isinstance(request, PromptRequest) else None


@contextlib.contextmanager
def prompt_owner_context(owner: str) -> Iterator[None]:
    token = _OWNER.set(_bounded(owner, MAX_OWNER_CHARS))
    try:
        yield
    finally:
        with contextlib.suppress(Exception):
            _OWNER.reset(token)


def set_prompt_request(
    *, producer: str, schema: str, sections: Sequence[PromptSection],
    expected_system: str, release_id: str | None = None,
) -> None:
    """Stage builder-owned section metadata for the next actual provider dispatch."""
    _REQUEST.set(PromptRequest(
        owner=_OWNER.get() or uuid.uuid4().hex,
        producer=producer, schema=schema, sections=tuple(sections),
        expected_system=expected_system, release_id=release_id,
    ))


@contextlib.contextmanager
def prompt_request_context(request: PromptRequest) -> Iterator[None]:
    token = _REQUEST.set(request)
    try:
        yield
    finally:
        with contextlib.suppress(Exception):
            _REQUEST.reset(token)


def consume_request_for_system(system: str, owner: str) -> PromptRequest:
    request = _REQUEST.get()
    if isinstance(request, PromptRequest) and request.expected_system == system:
        return request
    return role_request_for_system(system, owner)


@contextlib.contextmanager
def provider_call_context(
    messages: Sequence[dict[str, Any]], *, owner: str | None = None,
) -> Iterator[PromptCall | None]:
    """Mint correlation once, immediately around an in-scope provider dispatch."""
    actual = next((str(m.get("content") or "") for m in messages if m.get("role") == "system"), None)
    request = _REQUEST.get()
    if not isinstance(request, PromptRequest) and actual is not None:
        request = role_request_for_system(actual, owner or _OWNER.get() or uuid.uuid4().hex)
    if not isinstance(request, PromptRequest):
        yield None
        return
    if request.expected_system is not None and actual != request.expected_system:
        unavailable_id = uuid.uuid4().hex
        call = PromptCall(
            call_id=unavailable_id,
            provenance={"version": SCHEMA_VERSION, "call_id": unavailable_id, "provenance_state": "unavailable"},
        )
    else:
        call = observe_prompt(
            owner=owner or request.owner, producer=request.producer, schema=request.schema,
            sections=request.sections, release_id=request.release_id,
            actual_system=actual,
            expected_system=request.expected_system if request.expected_system is not None else actual,
        )
    token = _CURRENT.set(call.provenance)
    _FAILED.set(None)
    try:
        yield call
    except BaseException:
        _FAILED.set(call.provenance)
        raise
    finally:
        with contextlib.suppress(Exception):
            _CURRENT.reset(token)


@contextlib.contextmanager
def prompt_call_context(call: PromptCall) -> Iterator[None]:
    token = attach_current(call.provenance)
    try:
        yield
    finally:
        reset_current(token)


def remember_role_assembly(
    system: str, producer: str, sections: Sequence[PromptSection],
    release_id: str | None = None, *, schema: str | None = None, owner: str | None = None,
) -> None:
    _ROLE_ASSEMBLY.set({
        "system": system, "producer": producer,
        "schema": schema or f"role-prompt-v1:{producer}",
        "sections": tuple(sections), "release_id": release_id, "owner": owner,
    })


def remember_prompt_assembly(
    system: str, producer: str, schema: str, sections: Sequence[PromptSection],
    *, owner: str | None = None, release_id: str | None = None,
) -> None:
    _ROLE_ASSEMBLY.set({
        "system": system, "producer": producer, "schema": schema,
        "sections": tuple(sections), "release_id": release_id, "owner": owner,
    })


def role_request_for_system(
    system: str, owner: str, *, producer_hint: str | None = None,
) -> PromptRequest:
    assembly = _ROLE_ASSEMBLY.get()
    if isinstance(assembly, dict) and assembly.get("system") == system:
        _ROLE_ASSEMBLY.set(None)
        request_owner = str(assembly.get("owner") or owner)
        return PromptRequest(request_owner, str(assembly.get("producer") or "unknown"), str(assembly.get("schema") or "unknown"), tuple(assembly.get("sections") or ()), system, assembly.get("release_id"))
    return PromptRequest(owner, producer_hint or "unknown", f"unknown:{producer_hint}" if producer_hint else "unknown", (), system, None)


def _safe_entries(producer: str, sections: Sequence[PromptSection]) -> tuple[list[dict[str, Any]], list[str]]:
    entries: list[dict[str, Any]] = []
    order: list[str] = []
    for section in sections:
        name = str(section.name)[:MAX_NAME_CHARS]
        # Role text is safe only when its caller explicitly identifies one of
        # the reviewed fixed role-file producers. No inferred safety by name.
        if (
            not section.evidence_safe
            or producer not in SAFE_ROLE_PRODUCERS
            or name != "role"
        ):
            continue
        raw = section.content.encode("utf-8")
        entries.append({"name": name, "sha256": hashlib.sha256(raw).hexdigest(), "chars": len(section.content)})
        order.append(name)
        if len(entries) >= MAX_SECTIONS:
            break
    return entries, order
