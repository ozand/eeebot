"""Frozen scorer — pure, isolated reward computation for the eeebot coordinator.

Inspired by Darwin Mode ADR-072 (ruvnet/agent-harness-generator):
  'A variant may propose weights, but the verdict that decides promotion is
   computed here [frozen scorer], so a variant can never re-grade itself.'

This module is intentionally dependency-free from the rest of nanobot.
coordinator.py calls score_cycle() and uses the result; this file itself
must never be modified by the self-evolving loop.

SCORER_VERSION is logged in every cycle's history JSON for auditability.
"""
from __future__ import annotations

from dataclasses import dataclass

# ── Module-level constants — NOT configurable at runtime ──────────────────────
SCORER_VERSION = "1.0"

# Reward weights (sum to 1.0)
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
    "record_reward_after_synthesized_materialization": 0.8,
    "record_reward_after_synth":             0.8,
    "retire_stale_subagent_lane":            0.7,
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


def score_cycle(
    fd: dict,
    budget: dict,
    commits_pushed: int,
    result_status: str,
) -> ScoringResult:
    """Compute the reward score for one coordinator cycle.

    Pure function — same inputs always produce the same ScoringResult.
    No file I/O, no side effects, no imports from coordinator.py.

    Args:
        fd: feedback_decision dict from the cycle (must have 'mode' key).
        budget: budget_used dict (currently unused, reserved for future cost weighting).
        commits_pushed: number of git commits the subagent pushed (0 = no concrete change).
        result_status: result_status string from bridge result JSON.

    Returns:
        ScoringResult with value, outcome, revert_required, rationale.
    """
    mode = str(fd.get("mode") or "")
    status = str(result_status or "")

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
        WEIGHT_COMMITS * commit_score
        + WEIGHT_MODE   * mode_score
        + WEIGHT_STATUS * status_score,
        6,
    )

    # ── Bonus: already_done is a valid, positive outcome ─────────────────────
    if status == "already_done":
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
    )


def beats_parent(child: ScoringResult, parent_value: float) -> bool:
    """True if child clears the promotion delta above parent (ADR-072 anti-noise margin)."""
    return child.value >= parent_value + PROMOTION_DELTA
