#!/usr/bin/env python3
"""Replay a semantic dedup key against actual later outcomes (#1451).

#1328 replayed name-based cooling keys against what actually happened next and
disqualified every one of them. The semantic key (wave 2 item S2) has no such
number, and a rule not replayed against actual later outcomes is a guess. This
script is the replay, in four stages that are deliberately separable:

1. ``extract``  — rebuild the historical candidate set from the ledger alone.
2. ``audit``    — decide whether the comparison set the issue names can be
                  built at all, from the surviving result artifacts.
3. ``embed``    — batched gateway embedding calls (the host is i386/2 GB, so
                  embeddings are gateway calls, never local computation).
4. ``score``    — the decisive number, at a threshold fixed before scoring.

Stage 2 exists because stage 4 is meaningless without it, and it is a gate, not
a formality: ``score`` refuses to run on a candidate set whose adjudication is
incomplete rather than quietly reporting a number for a different population.

Everything here is read-only. Point ``--ledger-dir`` at a copy of
``state/ledger`` and ``--results-index`` at a dump of the surviving result
artifacts; nothing is written to the host.
"""
from __future__ import annotations

import argparse
import glob
import gzip
import json
import math
import os
import random
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

# ─── the pre-declared threshold ────────────────────────────────────────────
#
# The threshold is a RULE fixed before any scoring, not a number imported from
# somewhere else. #1451 rules out importing 0.95 from the ShinkaEvolve
# ablation, and rightly: that value was measured on a different corpus with a
# different embedding model, and a cosine threshold is meaningless without the
# scale the model actually produces. Proposal titles from one repository share
# vocabulary, paths and phrasing, so their pairwise similarity floor is high
# and unknown in advance.
#
# So the threshold is defined against this corpus's own null distribution:
#
#     block iff cosine(candidate, prior) >= the NULL_PERCENTILE-th percentile
#     of cosine similarity over UNRELATED pairs drawn from the same texts,
#     where "unrelated" means different demand_id AND different target_path.
#
# Two reasons for this shape rather than an absolute constant:
#
# * **Asymmetric cost.** #1328's standing rule is that a falsely suppressed
#   live task costs more than a paid duplicate. A percentile states the false
#   block rate against unrelated work directly: at 99, at most 1% of unrelated
#   pairs are as similar as the pairs this key would block.
# * **Model independence.** The gateway's embedding model can change. A
#   percentile of the model's own null distribution survives that; a hardcoded
#   0.95 silently becomes a different rule.
#
# The absolute cosine value this resolves to is reported alongside the result,
# so the number is auditable — but it is an output of the run, never an input.
NULL_PERCENTILE = 99.0
NULL_PAIR_SAMPLE = 4000
NULL_SEED = 1451

#: The bridge's own real-failure criterion is keyed on the terminal ``outcome``
#: value; ``skipped-*`` outcomes are the dedup stack working, not failures.
#: Mirrors ``nanobot.runtime.demand._ledger_defects``.
REAL_FAILURE_OUTCOMES = frozenset({"failed", "timeout", "error", "harness_failed"})

#: #1328's replay window: a prior failure only cools a later proposal if it
#: happened within this many hours.
DEFAULT_WINDOW_HOURS = 24

#: Gateway batch size for embedding requests. Batching reduces request count,
#: never text count — #1451 says so explicitly, and the cost report keeps both.
EMBED_BATCH = 32


# ─── ledger extraction ─────────────────────────────────────────────────────

@dataclass
class Cycle:
    """One proposed cycle joined to its terminal outcome."""

    cycle_id: str
    ts: datetime
    demand_id: str
    target_path: str
    task_title: str
    expected_outcome_claim: str
    outcome: str
    reason: str

    def text(self) -> str:
        """The proposal text embedded for this cycle.

        ``task_title`` plus ``expected_outcome_claim``: the two proposal fields
        the ledger actually records. ``rationale`` and ``serves`` live in the
        improvement artifact, not in the ledger row, and are added by the
        caller when that artifact survives for the cycle.
        """
        parts = [self.task_title.strip(), self.expected_outcome_claim.strip()]
        return "\n".join(part for part in parts if part)


def _parse_ts(raw: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def load_ledger_rows(ledger_dir: Path) -> list[dict[str, Any]]:
    """Every row from the live ledger and its gzipped daily archives.

    A malformed line is skipped rather than fatal: the ledger is append-only
    and a torn last line is an ordinary crash artifact, not a reason to refuse
    the whole corpus.
    """
    rows: list[dict[str, Any]] = []
    paths = sorted(glob.glob(str(ledger_dir / "*.jsonl.gz")))
    live = ledger_dir / "cycles.jsonl"
    if live.is_file():
        paths.append(str(live))
    for path in paths:
        opener = gzip.open if path.endswith(".gz") else open
        try:
            with opener(path, "rt", encoding="utf-8", errors="replace") as handle:  # type: ignore[operator]
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(row, dict):
                        rows.append(row)
        except OSError:
            continue
    return rows


def build_cycles(rows: Iterable[dict[str, Any]], *, until: datetime | None = None) -> list[Cycle]:
    """Join ``proposed`` rows to their ``outcome`` rows, oldest first.

    ``until`` reproduces a historical measurement window; a cycle proposed
    before the cutoff keeps its outcome even when the outcome landed after it,
    because the question is what the proposal did, not when it finished.
    """
    proposed: dict[str, dict[str, Any]] = {}
    outcomes: dict[str, dict[str, Any]] = {}
    for row in rows:
        cycle_id = row.get("cycle_id")
        if not cycle_id:
            continue
        phase = row.get("phase")
        if phase == "proposed":
            ts = _parse_ts(row.get("ts"))
            if ts is None or (until is not None and ts >= until):
                continue
            proposed.setdefault(str(cycle_id), row)
        elif phase == "outcome":
            outcomes[str(cycle_id)] = row
    cycles: list[Cycle] = []
    for cycle_id, row in proposed.items():
        ts = _parse_ts(row.get("ts"))
        if ts is None:
            continue
        outcome_row = outcomes.get(cycle_id) or {}
        cycles.append(
            Cycle(
                cycle_id=cycle_id,
                ts=ts,
                demand_id=str(row.get("demand_id") or "").strip(),
                target_path=str(row.get("target_path") or "").strip(),
                task_title=str(row.get("task_title") or "").strip(),
                expected_outcome_claim=str(row.get("expected_outcome_claim") or "").strip(),
                outcome=str(outcome_row.get("outcome") or "").strip().lower(),
                reason=str(outcome_row.get("reason") or "").strip(),
            )
        )
    cycles.sort(key=lambda cycle: cycle.ts)
    return cycles


@dataclass
class Candidate:
    """A proposal the name key would have cooled, and the prior it matched."""

    blocked: Cycle
    prior: Cycle

    @property
    def later_success(self) -> bool:
        return self.blocked.outcome == "success"

    @property
    def later_partial(self) -> bool:
        return self.blocked.outcome == "partial"


def name_key_candidates(
    cycles: Sequence[Cycle],
    *,
    window_hours: int = DEFAULT_WINDOW_HOURS,
    is_failure: Callable[[Cycle], bool] | None = None,
) -> list[Candidate]:
    """#1328's replay: proposals a ``(demand_id, target_path)`` key would cool.

    For each proposal, the nearest prior cycle inside the window that shares
    both the demand id and the target path and whose outcome was a failure.
    ``is_failure`` defaults to the plain ``outcome == "failed"`` key, which is
    the row of #1328's table that the ledger alone can reproduce; the bridge's
    stricter criterion additionally needs the result artifact (see
    :func:`is_real_result`).
    """
    failure = is_failure or (lambda cycle: cycle.outcome == "failed")
    candidates: list[Candidate] = []
    for index, cycle in enumerate(cycles):
        if not cycle.demand_id or not cycle.target_path:
            continue
        cutoff = cycle.ts - timedelta(hours=window_hours)
        for prior_index in range(index - 1, -1, -1):
            prior = cycles[prior_index]
            if prior.ts < cutoff:
                break
            if (
                prior.demand_id == cycle.demand_id
                and prior.target_path == cycle.target_path
                and failure(prior)
            ):
                candidates.append(Candidate(blocked=cycle, prior=prior))
                break
    return candidates


# ─── the bridge's real-failure criterion, and whether it can still be applied ─

def is_real_result(record: dict[str, Any]) -> bool:
    """``nanobot.runtime.bridge._is_real_result``, ported verbatim in behaviour.

    True only when a result represents actual LLM execution rather than a
    blocked stub. Ported rather than imported so this script can run against a
    dumped field index without the runtime on the path; the test pins it
    against the live function.
    """
    status = str(record.get("result_status") or record.get("status") or "").lower()
    terminal = str(record.get("terminal_reason") or "").lower()
    materialized_from = str(record.get("materialized_from") or "").lower()
    blocker = record.get("blocker") or {}
    blocker_reason = str(blocker.get("reason") or "") if isinstance(blocker, dict) else ""
    blocker_reason = blocker_reason or str(record.get("blocker_reason") or "")
    if status == "blocked":
        return False
    if terminal == "local_executor_unavailable":
        return False
    if materialized_from == "queued_request_terminalizer":
        return False
    if blocker_reason == "local_executor_unavailable":
        return False
    return True


@dataclass
class CoverageAudit:
    """Whether the bridge-criterion comparison set can still be built.

    The name-key candidate set comes from the ledger, which is append-only and
    survives. Narrowing it to the bridge's own real-failure criterion needs
    each prior's result artifact, which the archiver prunes. When priors are
    missing, the exact set cannot be rebuilt — and neither can a count of how
    many it would have contained, because an unaudited prior might have been a
    blocked stub.
    """

    total: int
    adjudicable: int
    missing: int
    blocked_priors: int
    missing_ts_range: tuple[str, str] | None
    missing_cycle_ids: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return self.missing == 0

    def lower_bound(self) -> int:
        """Smallest bridge-criterion set consistent with what survives: every
        unaudited prior assumed to be a blocked stub."""
        return self.adjudicable - self.blocked_priors

    def upper_bound(self) -> int:
        """Largest such set: every unaudited prior assumed real."""
        return self.total - self.blocked_priors


def audit_coverage(
    candidates: Sequence[Candidate], result_index: dict[str, dict[str, Any]]
) -> CoverageAudit:
    missing_cycles = [c.prior for c in candidates if c.prior.cycle_id not in result_index]
    blocked = sum(
        1
        for candidate in candidates
        if (record := result_index.get(candidate.prior.cycle_id)) is not None
        and not is_real_result(record)
    )
    ts_range: tuple[str, str] | None = None
    if missing_cycles:
        stamps = sorted(cycle.ts for cycle in missing_cycles)
        ts_range = (stamps[0].isoformat(), stamps[-1].isoformat())
    return CoverageAudit(
        total=len(candidates),
        adjudicable=len(candidates) - len(missing_cycles),
        missing=len(missing_cycles),
        blocked_priors=blocked,
        missing_ts_range=ts_range,
        missing_cycle_ids=[cycle.cycle_id for cycle in missing_cycles],
    )


def load_result_index(path: Path) -> dict[str, dict[str, Any]]:
    """A dump of surviving result artifacts, keyed by cycle id.

    Accepts either a JSON list of records or a mapping already keyed by cycle
    id. Only the fields :func:`is_real_result` reads are required.
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        return {str(key): value for key, value in raw.items() if isinstance(value, dict)}
    index: dict[str, dict[str, Any]] = {}
    for record in raw:
        if isinstance(record, dict) and record.get("cycle_id"):
            index[str(record["cycle_id"])] = record
    return index


# ─── embeddings (gateway calls, counted) ───────────────────────────────────

@dataclass
class EmbedCost:
    texts: int = 0
    requests: int = 0
    prompt_tokens: int = 0

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


def embed_texts(
    texts: Sequence[str],
    *,
    model: str | None = None,
    batch: int = EMBED_BATCH,
    client: Callable[[Sequence[str], str], tuple[list[list[float]], int]] | None = None,
) -> tuple[list[list[float]], EmbedCost]:
    """Embed ``texts`` through the LiteLLM gateway in batches.

    The host is i386 with 2 GB of RAM, so this is a network call per batch and
    never local computation. Both numbers are reported: batching reduces
    request count, not text count.
    """
    model = model or os.environ.get("SELFEVO_EMBEDDING_MODEL", "").strip()
    if not model:
        raise SystemExit(
            "no embedding model configured: set SELFEVO_EMBEDDING_MODEL or pass --model"
        )
    call = client or _gateway_embed
    vectors: list[list[float]] = []
    cost = EmbedCost()
    for start in range(0, len(texts), batch):
        chunk = list(texts[start : start + batch])
        batch_vectors, tokens = call(chunk, model)
        if len(batch_vectors) != len(chunk):
            raise SystemExit(
                f"gateway returned {len(batch_vectors)} vectors for {len(chunk)} texts"
            )
        vectors.extend(batch_vectors)
        cost.texts += len(chunk)
        cost.requests += 1
        cost.prompt_tokens += tokens
    return vectors, cost


def _gateway_embed(texts: Sequence[str], model: str) -> tuple[list[list[float]], int]:
    """One embeddings request through the gateway the rest of the loop uses."""
    from openai import OpenAI  # imported lazily: extraction and audit need no client

    base_url = os.environ.get("LITELLM_BASE_URL", "").strip()
    api_key = os.environ.get("LITELLM_API_KEY", "").strip()
    if not base_url or not api_key:
        raise SystemExit("litellm credentials not configured; check the unit EnvironmentFile chain")
    response = OpenAI(base_url=base_url, api_key=api_key, timeout=120).embeddings.create(
        model=model, input=list(texts)
    )
    usage = getattr(response, "usage", None)
    tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
    return [list(item.embedding) for item in response.data], tokens


def cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    norm_left = math.sqrt(sum(a * a for a in left))
    norm_right = math.sqrt(sum(b * b for b in right))
    if norm_left == 0.0 or norm_right == 0.0:
        return 0.0
    return dot / (norm_left * norm_right)


def percentile(values: Sequence[float], pct: float) -> float:
    """Linear-interpolated percentile; ``values`` need not be sorted."""
    if not values:
        raise ValueError("percentile of an empty sample")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (pct / 100.0) * (len(ordered) - 1)
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return ordered[int(position)]
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def null_threshold(
    keys: Sequence[tuple[str, str]],
    vectors: Sequence[Sequence[float]],
    *,
    pct: float = NULL_PERCENTILE,
    sample: int = NULL_PAIR_SAMPLE,
    seed: int = NULL_SEED,
) -> tuple[float, int]:
    """The threshold the pre-declared rule resolves to on this corpus.

    ``keys`` is ``(demand_id, target_path)`` per vector; a pair is part of the
    null only when both differ, so genuinely related work never inflates the
    baseline. Returns ``(threshold, pairs_sampled)``.
    """
    rng = random.Random(seed)
    count = len(vectors)
    if count < 2:
        raise ValueError("null distribution needs at least two vectors")
    sims: list[float] = []
    attempts = 0
    budget = sample * 20
    while len(sims) < sample and attempts < budget:
        attempts += 1
        i = rng.randrange(count)
        j = rng.randrange(count)
        if i == j:
            continue
        if keys[i][0] == keys[j][0] or keys[i][1] == keys[j][1]:
            continue
        sims.append(cosine(vectors[i], vectors[j]))
    if not sims:
        raise ValueError("no unrelated pairs available for the null distribution")
    return percentile(sims, pct), len(sims)


# ─── scoring ───────────────────────────────────────────────────────────────

def score(
    candidates: Sequence[Candidate],
    vectors_by_cycle: dict[str, Sequence[float]],
    *,
    threshold: float,
) -> dict[str, Any]:
    """Classify every candidate under the semantic key at a fixed threshold."""
    rows: list[dict[str, Any]] = []
    for candidate in candidates:
        left = vectors_by_cycle.get(candidate.blocked.cycle_id)
        right = vectors_by_cycle.get(candidate.prior.cycle_id)
        if left is None or right is None:
            continue
        similarity = cosine(left, right)
        blocks = similarity >= threshold
        rows.append(
            {
                "cycle_id": candidate.blocked.cycle_id,
                "prior_cycle_id": candidate.prior.cycle_id,
                "demand_id": candidate.blocked.demand_id,
                "target_path": candidate.blocked.target_path,
                "outcome": candidate.blocked.outcome,
                "similarity": round(similarity, 6),
                "semantic_blocks": blocks,
            }
        )
    successes = [row for row in rows if row["outcome"] == "success"]
    return {
        "threshold": threshold,
        "scored": len(rows),
        "name_key_blocks": len(rows),
        "name_key_kills_successes": len(successes),
        "semantic_blocks": sum(1 for row in rows if row["semantic_blocks"]),
        "semantic_kills_successes": sum(1 for row in successes if row["semantic_blocks"]),
        "successes_rescued": sum(1 for row in successes if not row["semantic_blocks"]),
        "duplicates_released": sum(
            1 for row in rows if row["outcome"] != "success" and not row["semantic_blocks"]
        ),
        "rows": rows,
    }


# ─── CLI ───────────────────────────────────────────────────────────────────

def _extract(args: argparse.Namespace) -> int:
    rows = load_ledger_rows(Path(args.ledger_dir))
    until = _parse_ts(args.until) if args.until else None
    cycles = build_cycles(rows, until=until)
    candidates = name_key_candidates(cycles, window_hours=args.window_hours)
    report = {
        "ledger_rows": len(rows),
        "cycles": len(cycles),
        "window_hours": args.window_hours,
        "until": args.until,
        "name_key_blocks": len(candidates),
        "later_success": sum(1 for c in candidates if c.later_success),
        "later_partial": sum(1 for c in candidates if c.later_partial),
    }
    if args.out:
        payload = [
            {
                "blocked": {**asdict(c.blocked), "ts": c.blocked.ts.isoformat()},
                "prior": {**asdict(c.prior), "ts": c.prior.ts.isoformat()},
            }
            for c in candidates
        ]
        Path(args.out).write_text(json.dumps(payload, indent=1), encoding="utf-8")
        report["out"] = args.out
    json.dump(report, sys.stdout, indent=1)
    print()
    return 0


def _audit(args: argparse.Namespace) -> int:
    rows = load_ledger_rows(Path(args.ledger_dir))
    until = _parse_ts(args.until) if args.until else None
    cycles = build_cycles(rows, until=until)
    candidates = name_key_candidates(cycles, window_hours=args.window_hours)
    index = load_result_index(Path(args.results_index)) if args.results_index else {}
    audit = audit_coverage(candidates, index)
    report = {
        "name_key_blocks": audit.total,
        "adjudicable": audit.adjudicable,
        "missing_result_artifact": audit.missing,
        "blocked_priors_among_adjudicable": audit.blocked_priors,
        "bridge_criterion_set_lower_bound": audit.lower_bound(),
        "bridge_criterion_set_upper_bound": audit.upper_bound(),
        "missing_ts_range": audit.missing_ts_range,
        "complete": audit.complete,
    }
    json.dump(report, sys.stdout, indent=1)
    print()
    return 0 if audit.complete else 2


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    extract = sub.add_parser("extract", help="rebuild the name-key candidate set from the ledger")
    extract.add_argument("--ledger-dir", required=True)
    extract.add_argument("--window-hours", type=int, default=DEFAULT_WINDOW_HOURS)
    extract.add_argument("--until", help="ISO timestamp; reproduce a historical window")
    extract.add_argument("--out", help="write the candidate rows here")
    extract.set_defaults(func=_extract)

    audit = sub.add_parser("audit", help="can the bridge-criterion comparison set still be built?")
    audit.add_argument("--ledger-dir", required=True)
    audit.add_argument("--results-index", help="JSON dump of surviving result artifacts")
    audit.add_argument("--window-hours", type=int, default=DEFAULT_WINDOW_HOURS)
    audit.add_argument("--until")
    audit.set_defaults(func=_audit)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
