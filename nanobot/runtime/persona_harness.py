"""Persona self-consistency harness — #1771.

Two cheap tests for a question the project could not answer before: does a
trait that is *written* in the release-owned context files actually change
behaviour?

* **Self-consistency regression set.** A set of first-person statements whose
  correct answer follows from the character definition, wrapped in a plausible
  fiction so asking does not knock the model out of role, answered yes/no and
  repeated so sampling noise can be separated from signal. A statement the
  model disagrees with is a *definition gap*: written, but not working.
* **Ablation.** Re-run the same set with one release block removed. A block
  whose removal moves nothing is a block that is not earning its place in
  every prompt.

Two invariants this module holds deliberately:

1. **The prompt comes from production.** The system prompt is assembled by
   :meth:`nanobot.agent.context.ContextBuilder.build_system_prompt` with
   ``loop_profile=True`` — the same call the bridge makes. This module holds
   no copy of the block list, the block order or the caps; the ablation
   candidates are *derived* from ``ContextBuilder.BOOTSTRAP_FILES``. A copy
   would measure a prompt nobody runs.
2. **Ablation goes through the loader, not around it.** A block is removed by
   assembling against a shadow release root that lacks the file, so the real
   ``[missing: X]`` degradation path runs. Nothing patches the builder.

Controls come first. A control statement is true or false independently of
the persona; if one of them does not come back correct, the plumbing is
broken — wrong model, wrong endpoint, unparseable replies — and no character
verdict is published, because none would mean anything.

The gateway is one local model on one workstation. Every run states its
planned call count up front and refuses to start over budget.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import statistics
import tempfile
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nanobot.agent.context import ContextBuilder

# ─── constants ──────────────────────────────────────────────────────────────

#: Repo-root-relative location of the statement set.
DEFAULT_STATEMENTS_PATH = Path(__file__).resolve().parents[2] / "persona" / "statements.json"

#: Agreement at or above this is a trait that works.
HOLDS_AT = 0.8
#: Agreement below this is a definition gap. Between the two is "weak".
GAP_BELOW = 0.5
#: A control must be answered correctly every single time. It is chosen to be
#: unmissable; a single miss is evidence about the plumbing, not the character.
CONTROL_MIN_AGREEMENT = 1.0

#: An ablation whose agreement moves by less than this on every statement is
#: reported as "no measurable effect".
ABLATION_NOISE_FLOOR = 0.2

DEFAULT_REPEATS = 3
DEFAULT_MAX_CALLS = 400
DEFAULT_TEMPERATURE = 0.7
DEFAULT_TIMEOUT = 180.0

_THINK_RE = re.compile(r"<think>.*?</think>", re.IGNORECASE | re.DOTALL)
_OPEN_THINK_RE = re.compile(r"<think>.*\Z", re.IGNORECASE | re.DOTALL)
_YESNO_RE = re.compile(r"\b(yes|no)\b", re.IGNORECASE)

UNPARSED = "unparsed"


class PersonaHarnessError(RuntimeError):
    """Configuration or budget problem — raised before any model call."""


# ─── statement set ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Statement:
    id: str
    kind: str          # "behaviour" | "control"
    text: str
    expected: str      # "yes" | "no"
    source: str = ""
    trait: str = ""

    @property
    def is_control(self) -> bool:
        return self.kind == "control"


@dataclass(frozen=True)
class StatementSet:
    fiction: str
    statements: tuple[Statement, ...]

    @property
    def controls(self) -> tuple[Statement, ...]:
        return tuple(s for s in self.statements if s.is_control)

    @property
    def behaviours(self) -> tuple[Statement, ...]:
        return tuple(s for s in self.statements if not s.is_control)


def load_statements(path: Path | str | None = None) -> StatementSet:
    """Load and validate the statement set.

    Raises :class:`PersonaHarnessError` with a specific reason rather than
    returning a half-valid set: a malformed set would silently change what is
    being measured, and this runs on demand with an operator watching.
    """
    target = Path(path) if path else DEFAULT_STATEMENTS_PATH
    try:
        raw = json.loads(Path(target).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PersonaHarnessError(f"statement set not found: {target}") from exc
    except json.JSONDecodeError as exc:
        raise PersonaHarnessError(f"statement set is not valid JSON: {target}: {exc}") from exc

    fiction = str(raw.get("fiction") or "").strip()
    if not fiction:
        raise PersonaHarnessError("statement set has no `fiction` wrapper")

    entries = raw.get("statements")
    if not isinstance(entries, list) or not entries:
        raise PersonaHarnessError("statement set has no `statements` list")

    seen: set[str] = set()
    parsed: list[Statement] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise PersonaHarnessError("every statement must be an object")
        sid = str(entry.get("id") or "").strip()
        kind = str(entry.get("kind") or "").strip()
        text = str(entry.get("text") or "").strip()
        expected = str(entry.get("expected") or "").strip().lower()
        if not sid or sid in seen:
            raise PersonaHarnessError(f"statement id missing or duplicated: {sid!r}")
        if kind not in ("behaviour", "control"):
            raise PersonaHarnessError(f"{sid}: kind must be 'behaviour' or 'control', got {kind!r}")
        if not text:
            raise PersonaHarnessError(f"{sid}: empty text")
        if expected not in ("yes", "no"):
            raise PersonaHarnessError(f"{sid}: expected must be 'yes' or 'no', got {expected!r}")
        seen.add(sid)
        parsed.append(Statement(
            id=sid, kind=kind, text=text, expected=expected,
            source=str(entry.get("source") or ""), trait=str(entry.get("trait") or ""),
        ))

    result = StatementSet(fiction=fiction, statements=tuple(parsed))
    if not result.controls:
        raise PersonaHarnessError("statement set has no control statements")
    if not result.behaviours:
        raise PersonaHarnessError("statement set has no behavioural statements")
    return result


# ─── prompt assembly (production assembler, never a copy) ───────────────────


def ablation_candidates() -> tuple[str, ...]:
    """Release-owned block filenames, derived from the production assembler.

    Read off ``ContextBuilder.BOOTSTRAP_FILES`` rather than listed here, so a
    block added to or removed from the ontology is picked up without an edit
    to this module — and so no second copy of the block list can drift from
    the real one.
    """
    return tuple(
        filename
        for root_kind, filename, _cap, _required in ContextBuilder.BOOTSTRAP_FILES
        if root_kind == "release"
    )


def _shadow_release_root(release_root: Path, ablate: str, replacement: str | None, into: Path) -> Path:
    """Copy the release blocks into ``into``, omitting (or replacing) one.

    The omitted file is simply absent, so the builder's own ``[missing: X]``
    path runs — the same degradation a genuinely missing file takes.
    """
    into.mkdir(parents=True, exist_ok=True)
    for filename in ablation_candidates():
        if filename == ablate:
            if replacement is not None:
                (into / filename).write_text(replacement, encoding="utf-8")
            continue
        src = Path(release_root) / filename
        if src.is_file():
            shutil.copyfile(src, into / filename)
    return into


@dataclass
class AssembledPrompt:
    text: str
    fit: dict[str, Any]
    ablated: str = ""

    @property
    def chars(self) -> int:
        return len(self.text)


def assemble_prompt(
    release_root: Path | str,
    workspace: Path | str,
    *,
    ablate: str = "",
    replacement: str | None = None,
    excluded_skill_names: Sequence[str] | None = None,
    _tmp_root: Path | None = None,
) -> AssembledPrompt:
    """Assemble the loop-profile system prompt through the production builder.

    ``ablate`` names a release block to remove (or replace, with
    ``replacement``) for this assembly only. Nothing on disk is modified.
    """
    if not ablate:
        return _assemble(Path(release_root), workspace, "", excluded_skill_names)

    known = ablation_candidates()
    if ablate not in known:
        raise PersonaHarnessError(f"unknown block {ablate!r}; ablatable blocks: {', '.join(known)}")

    if _tmp_root is not None:           # tests pin the shadow root so they can inspect it
        root = _shadow_release_root(Path(release_root), ablate, replacement, Path(_tmp_root))
        return _assemble(root, workspace, ablate, excluded_skill_names)

    # The builder reads every block eagerly, so the shadow root is only needed
    # for the duration of the call and is removed with it.
    with tempfile.TemporaryDirectory(prefix="persona-ablate-") as holder:
        root = _shadow_release_root(Path(release_root), ablate, replacement, Path(holder))
        return _assemble(root, workspace, ablate, excluded_skill_names)


def _assemble(
    release_root: Path,
    workspace: Path | str,
    ablated: str,
    excluded_skill_names: Sequence[str] | None,
) -> AssembledPrompt:
    builder = ContextBuilder(Path(workspace), release_root=release_root)
    text = builder.build_system_prompt(
        excluded_skill_names=list(excluded_skill_names) if excluded_skill_names else None,
        loop_profile=True,
        degrade_on_overflow=True,
    )
    fit = dict(getattr(builder, "last_fit", None) or {})
    return AssembledPrompt(text=text, fit=fit, ablated=ablated)


# ─── answering ──────────────────────────────────────────────────────────────


def build_user_message(fiction: str, statement: Statement) -> str:
    return f"{fiction}\n\nStatement: {statement.text}\n\nAnswer (yes or no):"


def parse_answer(raw: str) -> str:
    """Extract ``yes``/``no`` from a reply, or :data:`UNPARSED`.

    Reasoning blocks are stripped first — including an unterminated one, which
    is what a truncated reply leaves behind — so a model that thinks out loud
    is not graded on the contents of its thinking.
    """
    text = _THINK_RE.sub(" ", raw or "")
    text = _OPEN_THINK_RE.sub(" ", text)
    match = _YESNO_RE.search(text)
    if not match:
        return UNPARSED
    return match.group(1).lower()


#: A client takes (system_prompt, user_message) and returns a dict with
#: ``text`` and optionally ``tokens``, ``finish_reason`` and ``error``.
Client = Callable[[str, str], dict[str, Any]]


def litellm_client(
    *,
    model: str = "",
    temperature: float = DEFAULT_TEMPERATURE,
    max_tokens: int = 0,
    timeout: float = DEFAULT_TIMEOUT,
) -> Client:
    """Client against the operator's LiteLLM endpoint.

    Credentials come from the environment (``LITELLM_BASE_URL`` /
    ``LITELLM_API_KEY``), the same way every other harness in this repository
    reaches the gateway. Missing credentials produce an error row per call,
    never a crash and never a guessed endpoint.
    """
    base_url = os.environ.get("LITELLM_BASE_URL", "").strip()
    api_key = os.environ.get("LITELLM_API_KEY", "").strip()

    from nanobot.runtime.model_registry import resolve_harness_max_tokens, resolve_model

    resolved_model = model or resolve_model("executor", strip_openai=True)
    resolved_max_tokens = max_tokens or resolve_harness_max_tokens()

    def _call(system: str, user: str) -> dict[str, Any]:
        if not base_url or not api_key:
            return {"text": "", "tokens": 0, "error": "runner_not_configured"}
        try:
            from openai import OpenAI

            started = time.monotonic()
            response = OpenAI(base_url=base_url, api_key=api_key, timeout=timeout).chat.completions.create(
                model=resolved_model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                max_tokens=resolved_max_tokens,
                temperature=temperature,
            )
            choice = response.choices[0]
            usage = getattr(response, "usage", None)
            return {
                "text": getattr(getattr(choice, "message", None), "content", "") or "",
                "tokens": int(getattr(usage, "total_tokens", 0) or 0),
                "finish_reason": str(getattr(choice, "finish_reason", "") or ""),
                "duration": time.monotonic() - started,
            }
        except Exception as exc:  # noqa: BLE001 — one bad call must not end the run
            return {"text": "", "tokens": 0, "error": f"{type(exc).__name__}: {exc}"}

    _call.model = resolved_model  # type: ignore[attr-defined]
    return _call


# ─── results ────────────────────────────────────────────────────────────────


@dataclass
class StatementResult:
    statement: Statement
    answers: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    tokens: int = 0

    @property
    def valid(self) -> int:
        return sum(1 for a in self.answers if a != UNPARSED)

    @property
    def agreement(self) -> float | None:
        """Share of parseable answers matching the definition, or None."""
        if not self.valid:
            return None
        hits = sum(1 for a in self.answers if a == self.statement.expected)
        return hits / self.valid

    @property
    def verdict(self) -> str:
        rate = self.agreement
        if rate is None:
            return "no-signal"
        if rate >= HOLDS_AT:
            return "holds"
        if rate < GAP_BELOW:
            return "gap"
        return "weak"


@dataclass
class RunReport:
    label: str
    model: str
    repeats: int
    calls: int = 0
    tokens: int = 0
    duration_s: float = 0.0
    prompt_chars: int = 0
    prompt_sections: dict[str, Any] = field(default_factory=dict)
    ablated: str = ""
    plumbing_broken: bool = False
    plumbing_reason: str = ""
    controls: list[StatementResult] = field(default_factory=list)
    behaviours: list[StatementResult] = field(default_factory=list)

    @property
    def gaps(self) -> list[StatementResult]:
        return [r for r in self.behaviours if r.verdict in ("gap", "weak")]

    def rate_by_id(self) -> dict[str, float | None]:
        return {r.statement.id: r.agreement for r in self.behaviours}


# ─── the run ────────────────────────────────────────────────────────────────


def plan_calls(statement_set: StatementSet, repeats: int, ablations: Sequence[str] = ()) -> int:
    """Total model calls a run will make. Stated before anything is spent."""
    per_pass = len(statement_set.statements) * max(1, repeats)
    return per_pass * (1 + len(ablations))


def _ask(
    client: Client,
    system: str,
    statement_set: StatementSet,
    statements: Iterable[Statement],
    repeats: int,
    report: RunReport,
) -> list[StatementResult]:
    results: list[StatementResult] = []
    for statement in statements:
        result = StatementResult(statement=statement)
        user = build_user_message(statement_set.fiction, statement)
        for _ in range(max(1, repeats)):
            raw = client(system, user) or {}
            report.calls += 1
            tokens = int(raw.get("tokens") or 0)
            result.tokens += tokens
            report.tokens += tokens
            error = str(raw.get("error") or "")
            if error:
                result.errors.append(error)
                result.answers.append(UNPARSED)
                continue
            result.answers.append(parse_answer(str(raw.get("text") or "")))
        results.append(result)
    return results


def run_once(
    client: Client,
    statement_set: StatementSet,
    prompt: AssembledPrompt,
    *,
    repeats: int = DEFAULT_REPEATS,
    label: str = "baseline",
    model: str = "",
) -> RunReport:
    """Ask the whole set once (controls first) against one assembled prompt.

    A control miss stops the pass immediately: behavioural results are not
    collected, because a run whose plumbing is broken produces numbers that
    look like character findings and are not.
    """
    report = RunReport(
        label=label,
        model=model or str(getattr(client, "model", "") or ""),
        repeats=max(1, repeats),
        prompt_chars=prompt.chars,
        prompt_sections=dict(prompt.fit.get("sections") or {}),
        ablated=prompt.ablated,
    )
    started = time.monotonic()

    report.controls = _ask(client, prompt.text, statement_set, statement_set.controls, repeats, report)
    broken = []
    for result in report.controls:
        rate = result.agreement
        if rate is None:
            broken.append(f"{result.statement.id}: no parseable answer in {len(result.answers)} attempts")
        elif rate < CONTROL_MIN_AGREEMENT:
            broken.append(f"{result.statement.id}: {rate:.0%} correct, required {CONTROL_MIN_AGREEMENT:.0%}")
    if broken:
        report.plumbing_broken = True
        report.plumbing_reason = "; ".join(broken)
        report.duration_s = time.monotonic() - started
        return report

    report.behaviours = _ask(client, prompt.text, statement_set, statement_set.behaviours, repeats, report)
    report.duration_s = time.monotonic() - started
    return report


@dataclass
class AblationDelta:
    block: str
    report: RunReport
    per_statement: dict[str, float] = field(default_factory=dict)

    @property
    def moved(self) -> dict[str, float]:
        return {k: v for k, v in self.per_statement.items() if abs(v) >= ABLATION_NOISE_FLOOR}

    @property
    def mean_abs_delta(self) -> float:
        values = [abs(v) for v in self.per_statement.values()]
        return statistics.fmean(values) if values else 0.0

    @property
    def verdict(self) -> str:
        if self.report.plumbing_broken:
            return "plumbing-broken"
        return "no measurable effect" if not self.moved else "changes behaviour"


def diff_reports(baseline: RunReport, ablated: RunReport, block: str) -> AblationDelta:
    """Per-statement agreement delta, ablated minus baseline.

    A statement without a rate on either side is omitted rather than scored as
    zero: "did not move" and "was never measured" are different findings.
    """
    base = baseline.rate_by_id()
    other = ablated.rate_by_id()
    per: dict[str, float] = {}
    for sid, base_rate in base.items():
        other_rate = other.get(sid)
        if base_rate is None or other_rate is None:
            continue
        per[sid] = other_rate - base_rate
    return AblationDelta(block=block, report=ablated, per_statement=per)


def run(
    client: Client,
    *,
    release_root: Path | str,
    workspace: Path | str,
    statement_set: StatementSet | None = None,
    statements_path: Path | str | None = None,
    repeats: int = DEFAULT_REPEATS,
    ablations: Sequence[str] = (),
    max_calls: int = DEFAULT_MAX_CALLS,
    excluded_skill_names: Sequence[str] | None = None,
) -> tuple[RunReport, list[AblationDelta]]:
    """Baseline pass, then one pass per ablated block.

    Refuses to start when the planned call count exceeds ``max_calls`` — the
    budget is checked before the first call, not discovered halfway through.
    """
    statement_set = statement_set or load_statements(statements_path)

    for block in ablations:
        if block not in ablation_candidates():
            raise PersonaHarnessError(
                f"unknown block {block!r}; ablatable blocks: {', '.join(ablation_candidates())}"
            )

    planned = plan_calls(statement_set, repeats, ablations)
    if planned > max_calls:
        raise PersonaHarnessError(
            f"planned {planned} model calls exceeds the budget of {max_calls}; "
            f"lower --repeats, drop an ablation, or raise --max-calls deliberately"
        )

    baseline_prompt = assemble_prompt(
        release_root, workspace, excluded_skill_names=excluded_skill_names,
    )
    baseline = run_once(client, statement_set, baseline_prompt, repeats=repeats, label="baseline")

    deltas: list[AblationDelta] = []
    if baseline.plumbing_broken:
        return baseline, deltas

    for block in ablations:
        prompt = assemble_prompt(
            release_root, workspace, ablate=block, excluded_skill_names=excluded_skill_names,
        )
        ablated = run_once(
            client, statement_set, prompt, repeats=repeats, label=f"ablate:{block}",
        )
        deltas.append(diff_reports(baseline, ablated, block))
    return baseline, deltas


# ─── reporting ──────────────────────────────────────────────────────────────


def _rate(value: float | None) -> str:
    return "  n/a" if value is None else f"{value:5.0%}"


def format_report(baseline: RunReport, deltas: Sequence[AblationDelta] = ()) -> str:
    """Human-readable report. Call count and tokens are always printed."""
    out: list[str] = []
    total_calls = baseline.calls + sum(d.report.calls for d in deltas)
    total_tokens = baseline.tokens + sum(d.report.tokens for d in deltas)
    total_time = baseline.duration_s + sum(d.report.duration_s for d in deltas)

    out.append("# Persona harness (#1771)")
    out.append("")
    out.append(f"model            {baseline.model or '<unset>'}")
    out.append(f"repeats          {baseline.repeats} per statement")
    out.append(f"prompt           {baseline.prompt_chars} chars")
    out.append(f"model calls      {total_calls}")
    out.append(f"tokens           {total_tokens}")
    out.append(f"wall clock       {total_time:.0f}s")
    out.append("")

    if baseline.plumbing_broken:
        out.append("## PLUMBING BROKEN — no character verdict published")
        out.append("")
        out.append(baseline.plumbing_reason)
        out.append("")
        out.append("A control statement is true or false independently of the persona.")
        out.append("A control miss means the run cannot say anything about the character:")
        out.append("check the model, the endpoint and the reply format before reading further.")
        return "\n".join(out)

    out.append("## Controls")
    for result in baseline.controls:
        out.append(f"  {result.statement.id}  {_rate(result.agreement)}  ok")
    out.append("")

    out.append("## Statements")
    out.append("  id    expect  agree  verdict  source        trait")
    for result in baseline.behaviours:
        s = result.statement
        out.append(
            f"  {s.id:5} {s.expected:>6}  {_rate(result.agreement)}  "
            f"{result.verdict:<8} {s.source:<13} {s.trait}"
        )
    out.append("")

    gaps = baseline.gaps
    out.append(f"## Definition gaps ({len(gaps)} of {len(baseline.behaviours)})")
    if not gaps:
        out.append("  none — every written trait held at or above the threshold")
    for result in gaps:
        s = result.statement
        out.append(f"  {s.id}  {_rate(result.agreement)}  [{s.source} · {s.trait}] {s.text}")
    out.append("")

    if deltas:
        out.append("## Ablation")
        for delta in deltas:
            out.append(
                f"  {delta.block:<14} {delta.verdict:<22} "
                f"mean |Δ| {delta.mean_abs_delta:.2f}  moved {len(delta.moved)}/{len(delta.per_statement)}"
            )
            for sid, value in sorted(delta.moved.items(), key=lambda kv: -abs(kv[1])):
                out.append(f"      {sid}  {value:+.0%}")
        out.append("")
        dead = [d.block for d in deltas if d.verdict == "no measurable effect"]
        out.append(f"  blocks whose removal changed nothing measurable: {', '.join(dead) if dead else 'none'}")
    return "\n".join(out)


def report_to_dict(baseline: RunReport, deltas: Sequence[AblationDelta] = ()) -> dict[str, Any]:
    """Machine-readable form of the same report."""
    def _statement_rows(results: Sequence[StatementResult]) -> list[dict[str, Any]]:
        return [
            {
                "id": r.statement.id,
                "kind": r.statement.kind,
                "source": r.statement.source,
                "trait": r.statement.trait,
                "expected": r.statement.expected,
                "answers": list(r.answers),
                "agreement": r.agreement,
                "verdict": r.verdict,
                "errors": list(r.errors),
                "tokens": r.tokens,
            }
            for r in results
        ]

    return {
        "schema": "persona-harness-v1",
        "model": baseline.model,
        "repeats": baseline.repeats,
        "prompt_chars": baseline.prompt_chars,
        "prompt_sections": baseline.prompt_sections,
        "plumbing_broken": baseline.plumbing_broken,
        "plumbing_reason": baseline.plumbing_reason,
        "calls": baseline.calls + sum(d.report.calls for d in deltas),
        "tokens": baseline.tokens + sum(d.report.tokens for d in deltas),
        "duration_s": baseline.duration_s + sum(d.report.duration_s for d in deltas),
        "controls": _statement_rows(baseline.controls),
        "statements": _statement_rows(baseline.behaviours),
        "gaps": [r.statement.id for r in baseline.gaps],
        "ablations": [
            {
                "block": d.block,
                "verdict": d.verdict,
                "mean_abs_delta": d.mean_abs_delta,
                "per_statement": d.per_statement,
                "plumbing_broken": d.report.plumbing_broken,
                "calls": d.report.calls,
                "tokens": d.report.tokens,
            }
            for d in deltas
        ],
    }
