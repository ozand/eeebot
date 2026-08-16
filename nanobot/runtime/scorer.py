"""Frozen scorer — pure, isolated reward computation for the eeebot coordinator.

Inspired by Darwin Mode ADR-072 (ruvnet/agent-harness-generator):
  'A variant may propose weights, but the verdict that decides promotion is
   computed here [frozen scorer], so a variant can never re-grade itself.'

This module is intentionally dependency-free from the rest of nanobot.
coordinator.py calls score_cycle() and uses the result; this file itself
must never be modified by the self-evolving loop.

SCORER_VERSION is logged in every cycle's history JSON for auditability.

Weight loading (Priority 18):
  score_cycle() accepts an optional weights_path: Path argument. When provided
  and valid, weights are loaded from that file (surfaces/score_weights.json).
  Module-level constants are NEVER mutated — frozen invariant preserved.
  SELFEVO_SURFACES_DIR env var controls whether coordinator passes the path.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# ── Module-level constants — NOT configurable at runtime ──────────────────────
SCORER_VERSION = "1.0"

# Reward weights (sum to 1.0) — hardcoded defaults, never mutated
WEIGHT_COMMITS = 0.40   # concrete code change is the strongest signal
WEIGHT_MODE    = 0.30   # feedback_decision.mode quality
WEIGHT_STATUS  = 0.30   # result_status from bridge

# Reward value thresholds
THRESHOLD_KEEP    = 1.0   # value ≥ this → keep (outcome=keep)
THRESHOLD_DISCARD = 0.6   # value < this → discard

# Promotion delta: child must beat parent by at least this margin
PROMOTION_DELTA = 0.05

# fd.mode → quality score (0..1)
_MODE_SCORES: dict[str, float] = {
    "continue_active_lane":                  1.0,
    "synthesize_next_candidate":             1.0,
    "handoff_to_subagent_verification":      1.0,
    "start_next_improvement_generation":     1.0,
    "record_reward_after_synthesized_materialization": 0.8,
    "record_reward_after_synth":             0.8,
    "retire_stale_subagent_lane":            0.7,
    "retire_completed_subagent_lane":        0.8,
    "record_reward":                         0.6,
    "discard":                               0.3,
}

# result_status → quality score
_STATUS_SCORES: dict[str, float] = {
    "completed":    1.0,
    "already_done": 1.0,   # valid termination — task was done, no re-work needed
    "blocked":      0.5,
    "timed_out":    0.4,
    "error":        0.2,
    "failed":       0.2,
}


# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ScoringResult:
    """Immutable result of score_cycle(). Pure — no side effects."""
    value: float            # 0.0 .. 1.5+ (1.0 baseline, >1.0 for commits)
    outcome: str            # "keep" | "discard" | "retry"
    revert_required: bool   # True when outcome=discard AND commits_pushed > 0
    rationale: str          # human-readable explanation
    scorer_version: str     # always SCORER_VERSION
    weights_source: str = field(default='hardcoded')  # 'hardcoded' | 'surfaces'


def _load_weights(
    weights_path: Optional[Path],
) -> tuple[float, float, float, str]:
    """Load scorer weights from file, falling back to hardcoded defaults.

    Does NOT mutate module-level constants — frozen invariant preserved.

    Returns:
        (w_commits, w_mode, w_status, source) where source is 'surfaces' or 'hardcoded'.

    Validation rules (any violation → hardcoded defaults):
      - All three keys present: WEIGHT_COMMITS, WEIGHT_MODE, WEIGHT_STATUS
      - Each value is a float in [0.01, 0.99]
      - Sum is within ±0.05 of 1.0
    """
    if weights_path is None or not weights_path.exists():
        return WEIGHT_COMMITS, WEIGHT_MODE, WEIGHT_STATUS, 'hardcoded'
    try:
        import json as _json
        data = _json.loads(weights_path.read_text(encoding='utf-8'))
        wc = float(data['WEIGHT_COMMITS'])
        wm = float(data['WEIGHT_MODE'])
        ws = float(data['WEIGHT_STATUS'])
        if not all(0.01 <= w <= 0.99 for w in (wc, wm, ws)):
            return WEIGHT_COMMITS, WEIGHT_MODE, WEIGHT_STATUS, 'hardcoded'
        if abs(wc + wm + ws - 1.0) > 0.05:
            return WEIGHT_COMMITS, WEIGHT_MODE, WEIGHT_STATUS, 'hardcoded'
        return wc, wm, ws, 'surfaces'
    except Exception:
        return WEIGHT_COMMITS, WEIGHT_MODE, WEIGHT_STATUS, 'hardcoded'


def score_cycle(
    fd: Optional[dict],
    budget: dict,
    commits_pushed: int,
    result_status: str,
    weights_path: Optional[Path] = None,
) -> ScoringResult:
    """Compute the reward score for one coordinator cycle.

    Pure function — same inputs always produce the same ScoringResult.
    No file I/O beyond optional weights_path read. No imports from coordinator.py.

    Args:
        fd: feedback_decision dict from the cycle (must have 'mode' key).
        budget: budget_used dict (currently unused, reserved for future cost weighting).
        commits_pushed: number of git commits the subagent pushed (0 = no concrete change).
        result_status: result_status string from bridge result JSON.
        weights_path: optional path to score_weights.json (surfaces/). When provided
            and valid, overrides hardcoded WEIGHT_* constants. Operator opt-in only.

    Returns:
        ScoringResult with value, outcome, revert_required, rationale, weights_source.
    """
    # VALIDITY-BEFORE-SCORE (#843): a null/malformed feedback dict is not a
    # valid submission — coerce to empty so it scores as "no signal" (unknown
    # mode/status → defaults), never as a pass. Prevents a crash masquerading
    # as a scoring path.
    if not isinstance(fd, dict):
        fd = {}

    mode = str(fd.get("mode") or "")
    status = str(result_status or "")

    # Load weights (operator opt-in via weights_path; hardcoded defaults otherwise)
    w_commits, w_mode, w_status, weights_source = _load_weights(weights_path)

    # ── Component scores ──────────────────────────────────────────────────────
    # Commits: 0 → 0.0, 1 → 1.0, 2+ → 1.2 (bonus for multiple commits)
    if commits_pushed <= 0:
        commit_score = 0.0
    elif commits_pushed == 1:
        commit_score = 1.0
    else:
        commit_score = min(1.2, 1.0 + 0.1 * (commits_pushed - 1))

    mode_score   = _MODE_SCORES.get(mode, 0.5)
    status_score = _STATUS_SCORES.get(status, 0.5)

    # ── Weighted composite ────────────────────────────────────────────────────
    value = round(
        w_commits * commit_score
        + w_mode   * mode_score
        + w_status * status_score,
        6,
    )

    # ── Bonus: already_done is a valid, positive outcome ─────────────────────
    # VALIDITY-BEFORE-SCORE (#843): credit already_done only when a real
    # feedback decision exists (non-empty mode). An empty/no-op submission
    # cannot claim a passing verdict by merely asserting status="already_done".
    if status == "already_done" and mode:
        value = max(value, 1.0)

    # ── Outcome gate ─────────────────────────────────────────────────────────
    if value >= THRESHOLD_KEEP:
        outcome = "keep"
    elif value < THRESHOLD_DISCARD:
        outcome = "discard"
    else:
        outcome = "retry"

    revert_required = (outcome == "discard" and commits_pushed > 0)

    # ── Rationale ────────────────────────────────────────────────────────────
    parts = [
        f"commits={commits_pushed}→{commit_score:.2f}",
        f"mode={mode!r}→{mode_score:.2f}",
        f"status={status!r}→{status_score:.2f}",
        f"value={value:.4f}",
        f"outcome={outcome}",
        f"weights={weights_source}",
    ]
    if revert_required:
        parts.append("revert_required=True")
    rationale = "; ".join(parts)

    return ScoringResult(
        value=value,
        outcome=outcome,
        revert_required=revert_required,
        rationale=rationale,
        scorer_version=SCORER_VERSION,
        weights_source=weights_source,
    )
