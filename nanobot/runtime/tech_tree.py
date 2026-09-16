"""Tech-tree of improvement DIRECTIONS — RSI stage 5 (#879).

Two DIFFERENT trees exist in this codebase; do not conflate them:

- ``evolution_tree.py`` (#877) is the STATE-space tree — where the code has
  BEEN (git shas, branches, generations). Already built.
- THIS module is the DIRECTION-space tree — which capability DOMAIN the
  loop invests its next cycles in. A Civ-style tech tree: invest in one
  direction while it keeps yielding measured gains; when that direction
  PLATEAUS (no net improvement over a trailing window), shift investment
  to the best other direction. The instance may also MINT a new direction
  from a supported hypothesis (#878) whose domain isn't already mapped.

STRICT product simplicity (the issue's own words): 2GB-simple, NO heavy
bandit machinery, NO MAP-Elites, NO new scheduler. This module is a
RANKING INPUT to the EXISTING demand/goal-review pipeline — the same shape
as the #815 V1-over-V2 soft vector bias in ``demand.py`` — never a
scheduler, never a gate. A forged/absent/corrupt sidecar can only ever
change which direction the loop *prefers*; every cycle still passes the
full unweakened gate, promotions are still root-verified, priorities still
pass ``goal_review.validate_priority``.

Mechanics, all crude-and-cheap by design:

- **Seed nodes** (:data:`SEED_NODES`): five product-authored domains, each
  naming one existing scorecard metric (a dotted ``section.metric`` path,
  e.g. ``"loop.repeat_failure_rate"``) as its lever, and which DIRECTION of
  that metric counts as improvement (``"lower"``/``"higher"``).
- **Marginal gain** (:func:`record_gains`): PACED BY INTEGRATION PROGRESS
  (#893), not by how often the caller (the scorecard recompute path) happens
  to invoke this function. The lever metrics are 7-day rolling aggregates
  that barely move between back-to-back recomputes — a timer-driven
  recompute tick can fire many times per hour while the underlying
  integration count crawls forward by only a handful over the same window;
  recording an observation on every tick regardless produced a wall of
  near-zero "gains" that plateaued every node within hours despite almost
  no real work having landed. So each call reads the harness's own
  cycle-progress counter, ``loop.integrations``, from the scorecard result;
  if it has not advanced past the portfolio's own last-observed watermark,
  the call records NOTHING (no gain observation, no ``last_lever_value``
  update, for any node) and returns. Only once ``loop.integrations`` has
  advanced does each node's lever value get read from the scorecard result
  (harness-owned, never an instance claim) and compared to the node's own
  previous value, oriented by its direction, to produce one signed "gain"
  observation appended to a bounded trailing window
  (:data:`GAIN_HISTORY_MAX`). The FIRST-EVER observation (no stored
  integration watermark yet) records no gain for any node (there is
  nothing yet to compare against) — only sets the baseline. A missing/
  non-numeric ``loop.integrations`` fails open to "record nothing" — it
  never falls back to the old per-tick recording.
- **Plateau** (:func:`is_plateaued`): once a node has a FULL window of
  observations — each now backed by real integration progress, not a bare
  recompute tick — a mean gain at or below :data:`PLATEAU_FLOOR` (0.0 — "no
  net improvement over the window", see the constant's own docstring for
  why ``<=`` was chosen over strict ``<``) marks it plateaued.
- **Selection** (:func:`select_current_direction`): epsilon-greedy
  (:data:`EPSILON`) over non-plateaued, non-cooldown nodes — explore a
  random eligible node with probability epsilon, otherwise exploit the
  highest mean-gain node (ties broken toward the least-tried node, so a
  freshly-seeded/reactivated node with zero attempts is not permanently
  starved by an established leader). When the PREVIOUSLY current node
  just plateaued this pass, it is retired into a cooldown window
  (:data:`COOLDOWN_HOURS`) and the forced switch is recorded (both in the
  sidecar's own bounded ``switches`` list and as a
  ``{"phase": "tech_tree", "reason": "plateau_switch"}`` ledger event).
  A plateaued node re-enters the eligible pool once its cooldown elapses,
  OR immediately if a newly-mintable hypothesis maps back onto it (see
  :func:`maybe_mint_node`).
- **Minting** (:func:`maybe_mint_node`): given
  ``hypothesis_backlog.supported_hypotheses`` output (harness-verdicted,
  never an instance claim), a simple normalized-token overlap between the
  hypothesis text and each existing node's name/lever-metric tokens
  decides whether its domain is already mapped. Mapped -> no mint (and a
  matched-but-plateaued node is reactivated instead, so a hypothesis can
  pull a domain back into rotation early). Unmapped -> mint ONE new node,
  rate-limited to at most one mint per :data:`MINT_MIN_INTERVAL_HOURS`
  (a wall-clock approximation of "at most 1 mint per M cycles" — this
  module has no direct cycle counter of its own, see the constant's
  docstring) and name-deduped against existing nodes. The new node's lever
  defaults to ``loop.confirmed_integration_ratio`` (higher-better) unless
  the hypothesis's own verdict-evidence names a metric this module already
  recognizes (:data:`_KNOWN_LEVER_DIRECTIONS`) — a deliberately crude v1
  mapping, documented as such.

Trust boundary (read before wiring this in anywhere else): the sidecar,
``<state_dir>/tech_tree/portfolio.json``, is instance-writable state
exactly like every other bridge sidecar (``demand/completed.json``,
``scorecard/latest.json``, ``evolution/tree.json``, ...). Selecting a
direction is a STEERING decision, not a verification one (the same
argument ``evolution_tree.py`` makes for its own sidecar): a forged
portfolio can, at worst, re-order which domain the loop *prefers* to work
on next — never bypass the gate, never skip root-verified promotion,
never starve another direction of a turn. This module's OWN write path
(:func:`record_gains`) never fabricates anything: it only ever appends a
``gain_history`` entry computed from (a) the node's own prior
``last_lever_value`` and (b) the CURRENT scorecard result handed to it by
the harness's own ``compute_scorecard`` recompute. But a direct edit to
the sidecar file itself — bypassing this module's API entirely — CAN
write an arbitrary ``gain_history``/``last_lever_value`` value, and that
forged value persists in the eligible-node ranking for up to
:data:`GAIN_HISTORY_MAX` observations, until the next :func:`record_gains`
call overwrites ``last_lever_value`` with the real scorecard reading (a
stale forged entry inside ``gain_history`` itself ages out of the window
the same way any other entry does). This is exactly why the sidecar is
listed in ``scorecard.FITNESS_SIDECARS`` (#789): such tampering is
DETECTED (spawn-boundary hash mismatch -> an ``integrity`` ledger row) —
never silently trusted, and never able to reach further than "which
direction gets preferred next," since a gate/promotion/priority-validation
bypass is never possible through this module. See
``nanobot/runtime/tech_tree.py``'s entry in
``runtime_deny._RUNTIME_DENY_ALWAYS_FILES`` for the matching
mutation-surface hardening (fitness-adjacent steering, same tier as
``evolution_tree.py``/``hypothesis_verdict.py``).

Everything here is stdlib-only and FAIL-OPEN: a missing/corrupt sidecar,
a bad argument, or any unexpected exception degrades to a no-op / empty
snapshot / ``None`` — never raises into the caller. A bug in this module
must NEVER block a cycle or the scorecard recompute it rides.
"""
from __future__ import annotations

import json
import random
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "tech-tree-v1"
_PORTFOLIO_SUBPATH = "tech_tree/portfolio.json"

# ─── seed nodes (product-authored, #879) ────────────────────────────────────
#
# Each names one existing scorecard metric (dotted "section.metric" path,
# see nanobot.runtime.scorecard's snapshot shape) as its lever, and which
# direction of that metric counts as an improvement.
SEED_NODES: tuple[dict[str, str], ...] = (
    {"name": "proposer-quality", "lever_metric": "loop.repeat_failure_rate", "direction": "lower"},
    {"name": "cycle-cost", "lever_metric": "cost.tokens_per_integration", "direction": "lower"},
    {"name": "tool-reuse", "lever_metric": "loop.confirmed_integration_ratio", "direction": "higher"},
    {"name": "heldout-robustness", "lever_metric": "heldout.heldout_gap", "direction": "lower"},
    {"name": "compile-health", "lever_metric": "quality.compile_clean_ratio", "direction": "higher"},
)

# Metrics + directions this module can recognize when a minted-from-
# hypothesis node's verdict evidence names one (crude v1 mapping, #879 —
# anything else falls back to DEFAULT_MINT_LEVER below). Kept in sync with
# SEED_NODES' own lever/direction pairs; a future seed addition should
# extend this too if it wants hypothesis-minted nodes to ever reuse it.
_KNOWN_LEVER_DIRECTIONS: dict[str, str] = {spec["lever_metric"]: spec["direction"] for spec in SEED_NODES}

# Crude v1 default lever for a hypothesis-minted node whose evidence names
# no metric this module recognizes (documented in the issue itself as a
# deliberate simplification — a mint should never be blocked for lack of a
# perfect metric mapping).
DEFAULT_MINT_LEVER = "loop.confirmed_integration_ratio"

# Trailing marginal-gain window per node (K). Chosen to match the
# hypothesis-lifecycle's own small-N conventions elsewhere in this
# codebase (e.g. hypothesis_backlog.SUPPORTED_TOP_N) — small enough that a
# node reaches a plateau verdict within a handful of scorecard recomputes,
# large enough that one noisy observation cannot single-handedly plateau
# or un-plateau it.
GAIN_HISTORY_MAX = 8

# Epsilon-greedy explore probability (#879: "NO heavy bandit machinery" —
# this is the entire exploration mechanism, no UCB/Thompson sampling).
EPSILON = 0.15

# PLATEAU_FLOOR = 0.0: a node's trailing mean marginal gain at or BELOW
# zero over a full GAIN_HISTORY_MAX window counts as plateaued — "no net
# improvement". Deliberately "<=" rather than strict "<": a node
# oscillating exactly around zero net change (a metric that has fully
# saturated) should read as plateaued, not perpetually "not yet plateaued"
# by a hair's-width technicality. Documented per the issue's own request
# to record this choice explicitly.
PLATEAU_FLOOR = 0.0

# How long a just-plateaued node sits out of the eligible pool before it
# can be reselected. 72h (3 days) mirrors hypothesis_backlog's own
# IN_FLIGHT_TIMEOUT_DAYS=3 "short but not thrashy" timescale — long enough
# that the loop does not immediately re-pick the direction it just left,
# short enough that a metric which later moves (e.g. after unrelated work
# shifts it) is not locked out for a goal_review-scale 14-day decay window.
COOLDOWN_HOURS = 72

# Rate limit for maybe_mint_node's NEW-node path. The issue asks for "at
# most 1 mint per M cycles"; this module has no direct cycle counter of
# its own (it is invoked from the scorecard recompute path, itself
# time-watermarked, not cycle-counted) so a wall-clock interval is used as
# the simplest faithful approximation — one calendar day, matching
# goal_review's own once-per-day review cadence this shares a call site
# with.
MINT_MIN_INTERVAL_HOURS = 24

# Bounded audit trail of forced plateau switches kept inside the sidecar
# itself (mirrors evolution_tree.MAX_SWITCHES) — the durable record is the
# cycle-ledger "phase": "tech_tree" rows; this is just a small in-sidecar
# summary for portfolio_snapshot/debugging.
MAX_SWITCHES = 20

# Token-matching hygiene (maybe_mint_node domain-mapping / goal_review's
# soft direction bias): ignore very short tokens and a tiny stopword set
# so generic connective words never register as a "match".
_MIN_TOKEN_LEN = 3
_STOPWORDS = frozenset({
    "the", "a", "an", "of", "to", "in", "on", "for", "and", "or", "is",
    "are", "this", "that", "it", "its", "use", "using", "via", "one",
})

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Reused four-state capability vocabulary from scripts/eeebot_dashboard.py.
# Keep this literal in sync; dashboard tests pin the same published states.
PROBE_STATES = frozenset({"present", "absent", "present_uninitialized", "probe_unavailable"})


# ─── small shared helpers (same shapes as demand.py / scorecard.py) ─────────


def _portfolio_path(state_dir: Any) -> Path:
    return Path(state_dir) / _PORTFOLIO_SUBPATH


def _iso(dt: 'datetime | None' = None) -> str:
    dt = dt or datetime.now(timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_ts(value: Any) -> 'datetime | None':
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except Exception:
        return None


def _empty_portfolio() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "current": None,
        "nodes": {},
        "switches": [],
        "last_mint_ts": None,
        # #893: global (not per-node) watermark of the harness's own
        # cycle-progress counter (``loop.integrations``) last observed by
        # record_gains — the pacing gate that keeps gain observations tied
        # to real integration progress rather than scorecard recompute
        # ticks. ``None`` means no observation has ever been recorded.
        "last_integrations": None,
    }


def read_portfolio(state_dir: Any) -> dict[str, Any]:
    """Parsed ``tech_tree/portfolio.json``, normalized to the v1 schema
    shape (same defensive-read pattern as ``evolution_tree.read_tree``).
    Any trouble at all (missing file, corrupt json, wrong top-level type,
    malformed sub-fields) degrades to a fresh empty portfolio — never
    raises. Read-only: never writes."""
    try:
        path = _portfolio_path(state_dir)
        if not path.is_file():
            return _empty_portfolio()
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return _empty_portfolio()
        portfolio = _empty_portfolio()
        current = raw.get("current")
        portfolio["current"] = current if isinstance(current, str) and current else None
        nodes = raw.get("nodes")
        if isinstance(nodes, dict):
            portfolio["nodes"] = {str(k): v for k, v in nodes.items() if isinstance(v, dict)}
        switches = raw.get("switches")
        if isinstance(switches, list):
            portfolio["switches"] = [s for s in switches if isinstance(s, dict)]
        last_mint = raw.get("last_mint_ts")
        portfolio["last_mint_ts"] = last_mint if isinstance(last_mint, str) and last_mint else None
        last_integrations = raw.get("last_integrations")
        portfolio["last_integrations"] = (
            last_integrations
            if isinstance(last_integrations, (int, float)) and not isinstance(last_integrations, bool)
            else None
        )
        return portfolio
    except Exception:
        return _empty_portfolio()


_PORTFOLIO_MAX_BYTES = 16 * 1024 * 1024  # same reasoning as evolution_tree._TREE_MAX_BYTES (#1178)


def _write_portfolio(state_dir: Any, portfolio: dict[str, Any]) -> None:
    path = _portfolio_path(state_dir)
    # #1178 Class B: the read that produced ``portfolio`` returns a blank default
    # on a corrupt/oversize/unreadable file; writing that back would erase the
    # history. Skip and say so; an absent file is created normally.
    from nanobot.runtime.state_access import WRITABLE_STATUSES, rewrite_status

    status = rewrite_status(path, max_bytes=_PORTFOLIO_MAX_BYTES)
    if status not in WRITABLE_STATUSES:
        import logging

        logging.getLogger(__name__).warning("tech_tree: write skipped, existing file is %s: %s", status, path)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(portfolio, indent=2, ensure_ascii=False), encoding="utf-8")


def _dotted_get(data: Any, dotted: str) -> 'float | None':
    """Numeric value at a dotted ``section.metric`` path inside a nested
    dict (the scorecard result's own shape), or ``None`` if absent/not a
    plain number. Booleans are excluded (``bool`` is an ``int`` subclass in
    Python but is never a metric value here)."""
    try:
        cur = data
        for part in dotted.split("."):
            if not isinstance(cur, dict):
                return None
            cur = cur.get(part)
        if isinstance(cur, bool) or not isinstance(cur, (int, float)):
            return None
        return float(cur)
    except Exception:
        return None


def _tokens(text: Any) -> set[str]:
    try:
        raw = _TOKEN_RE.findall(str(text or "").lower())
        return {t for t in raw if len(t) >= _MIN_TOKEN_LEN and t not in _STOPWORDS}
    except Exception:
        return set()


def _node_tokens(name: str, node: dict[str, Any]) -> set[str]:
    """Matchable tokens for one node: its own name plus ONLY the tail
    (metric, not section) component of its lever_metric — the section
    prefix (``loop``/``cost``/``quality``/...) is far too generic a word
    (it means "the whole self-evolving loop" in ordinary prose) to ever
    count as a meaningful domain match, so it is deliberately excluded."""
    lever = str(node.get("lever_metric") or "")
    tail = lever.rsplit(".", 1)[-1] if lever else ""
    return _tokens(name.replace("-", " ")) | _tokens(tail.replace("_", " "))


def _match_existing_node(text: str, nodes: dict[str, Any]) -> 'str | None':
    """Best node whose tokens overlap ``text``'s tokens (simple normalized
    token-overlap match — deliberately crude, #879 v1), or ``None`` when
    there is no overlap with any node. Ties broken by node insertion order
    (dict iteration order) — good enough for a soft, non-verification
    steering decision."""
    try:
        text_toks = _tokens(text)
        if not text_toks:
            return None
        best_name: 'str | None' = None
        best_score = 0
        for name, node in nodes.items():
            score = len(_node_tokens(name, node) & text_toks)
            if score > best_score:
                best_score = score
                best_name = name
        return best_name if best_score > 0 else None
    except Exception:
        return None


# The bound was a bare ``[:40]`` from this module's very first commit
# (#879, a8407ff7) -- never documented, never revisited, and cut mid-word
# (#1686): the node's dict key, its ``current`` value, and every
# ``direction_for_metric``/``matches_direction`` comparison read a
# fragment like ``automated-structural-assertion-for-doc-o``. Raised to 60
# here -- long enough that two distinct hypothesis titles sharing an
# identical prefix this long is rarer still, short enough to stay a usable
# dict key/display fallback for a node whose full text isn't stored (see
# ``_SLUG_MAX_LEN`` used below). The untruncated text is no longer lost
# regardless of the exact bound: :func:`maybe_mint_node` stores it in the
# node's own ``label`` field, so the key's length is a display convenience
# now, not the only record of the original intent.
_SLUG_MAX_LEN = 60


def _slugify(text: str, max_len: int = _SLUG_MAX_LEN) -> str:
    """Hyphenated lowercase slug, truncated to *max_len* on a WORD
    boundary -- never mid-token (#1686). If the text is already short
    enough, or has no hyphen at all, it is returned whole. If its very
    first token alone exceeds *max_len* (no hyphen within budget), that
    whole token is kept rather than split -- a longer key beats a
    mid-word fragment."""
    try:
        slug = re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-")
        if len(slug) <= max_len:
            return slug
        cut = slug.rfind("-", 0, max_len + 1)
        if cut <= 0:
            next_hyphen = slug.find("-", max_len)
            return slug if next_hyphen < 0 else slug[:next_hyphen]
        return slug[:cut]
    except Exception:
        return ""


def _unique_node_name(base_name: str, nodes: dict[str, Any]) -> str:
    """*base_name* if free, else a numeric-suffixed variant not already in
    *nodes* (#1686). Two distinct source texts whose slugs collide --
    trivially possible when both are truncated to the same length prefix
    -- get distinct keys and distinct gain histories instead of the second
    one silently merging into the first's node."""
    name, suffix = base_name, 2
    while name in nodes:
        name = f"{base_name}-{suffix}"
        suffix += 1
    return name


# ─── seeding ─────────────────────────────────────────────────────────────────


def _seed_node_entry(spec: dict[str, str], now_iso: str) -> dict[str, Any]:
    return {
        "lever_metric": spec["lever_metric"],
        "direction": spec["direction"],
        "gain_history": [],
        "status": "active",
        "cooldown_until_ts": None,
        "minted_by": "product",
        "created_ts": now_iso,
        "last_lever_value": None,
    }


def ensure_seeded(state_dir: Any, *, now: 'datetime | None' = None) -> None:
    """Create ``portfolio.json`` from :data:`SEED_NODES` if absent, or add
    any MISSING seed nodes idempotently to an existing portfolio — never
    deletes or overwrites an existing entry (product or instance-minted
    alike). Calling this repeatedly is a no-op once every seed node
    exists. Fail-open: any error is swallowed silently."""
    try:
        portfolio = read_portfolio(state_dir)
        now_iso = _iso(now)
        changed = False
        for spec in SEED_NODES:
            name = spec["name"]
            if name not in portfolio["nodes"]:
                portfolio["nodes"][name] = _seed_node_entry(spec, now_iso)
                changed = True
        if changed:
            _write_portfolio(state_dir, portfolio)
    except Exception:
        pass


# ─── marginal gain ───────────────────────────────────────────────────────────


def record_gains(state_dir: Any, scorecard_result: dict[str, Any]) -> None:
    """PACED BY INTEGRATION PROGRESS (#893), not by how often this is
    called. Reads the harness's own cycle-progress counter,
    ``loop.integrations``, from ``scorecard_result`` (dotted lookup via
    :func:`_dotted_get`):

    - Absent/non-numeric -> FAIL OPEN: record nothing this call (never
      falls back to the old per-tick recording).
    - Present but NOT strictly greater than the portfolio's own
      ``last_integrations`` watermark -> record nothing this call: no gain
      observation and no ``last_lever_value`` update for ANY node (the
      lever metrics are 7-day aggregates that barely move between
      back-to-back scorecard recompute ticks, so a tick with no new
      integration progress carries no signal worth recording).
    - Present and ADVANCED (or this is the first-ever observation, i.e.
      no ``last_integrations`` watermark yet) -> for each node, read its
      ``lever_metric``'s CURRENT value from ``scorecard_result`` (fail-open
      skip if absent/non-numeric) and append one signed marginal-gain
      observation, oriented by the node's own ``direction``: lower-better
      -> ``gain = last - current``; higher-better -> ``gain = current -
      last``. The FIRST-EVER observation (no stored ``last_integrations``
      watermark yet) records no gain for any node, only sets the baseline
      (matches each node's own pre-existing first-observation convention).
      Bounded to the trailing :data:`GAIN_HISTORY_MAX` observations, then
      ``last_integrations`` is advanced to this call's value.

    Harness-computed ONLY — this never reads any instance-authored "gain"
    field, only the scorecard result it is handed and the node's own
    previously-recorded baseline. Fail-open: any error is swallowed
    silently."""
    try:
        ensure_seeded(state_dir)
        portfolio = read_portfolio(state_dir)

        integrations = _dotted_get(scorecard_result, "loop.integrations")
        if integrations is None:
            return  # no pacing signal at all -- fail open, record nothing

        last_integrations = portfolio.get("last_integrations")
        has_watermark = isinstance(last_integrations, (int, float)) and not isinstance(last_integrations, bool)
        if has_watermark and integrations <= float(last_integrations):
            return  # no integration progress since the last observation

        nodes = portfolio["nodes"]
        for node in nodes.values():
            lever = node.get("lever_metric")
            if not lever:
                continue
            current = _dotted_get(scorecard_result, str(lever))
            if current is None:
                continue
            last = node.get("last_lever_value")
            if isinstance(last, (int, float)) and not isinstance(last, bool):
                direction = node.get("direction")
                gain = (float(last) - current) if direction == "lower" else (current - float(last))
                history = node.get("gain_history")
                history = list(history) if isinstance(history, list) else []
                history.append(round(gain, 6))
                if len(history) > GAIN_HISTORY_MAX:
                    history = history[-GAIN_HISTORY_MAX:]
                node["gain_history"] = history
            node["last_lever_value"] = current

        portfolio["nodes"] = nodes
        portfolio["last_integrations"] = integrations
        _write_portfolio(state_dir, portfolio)
    except Exception:
        pass


def node_mean_gain(node: dict[str, Any]) -> float:
    """Mean of ``node``'s ``gain_history`` (0.0 when empty). Fail-open."""
    try:
        history = node.get("gain_history") or []
        nums = [float(x) for x in history if isinstance(x, (int, float)) and not isinstance(x, bool)]
        if not nums:
            return 0.0
        return sum(nums) / len(nums)
    except Exception:
        return 0.0


def is_plateaued(node: dict[str, Any], floor: float = PLATEAU_FLOOR) -> bool:
    """True iff ``node`` has a FULL trailing window
    (:data:`GAIN_HISTORY_MAX` observations) whose mean is at or below
    ``floor``. A node with fewer observations is never plateaued — there
    is not yet enough evidence to call it. Fail-open to ``False``."""
    try:
        history = node.get("gain_history") or []
        if len(history) < GAIN_HISTORY_MAX:
            return False
        return node_mean_gain(node) <= floor
    except Exception:
        return False


# ─── selection (epsilon-greedy over non-plateaued, non-cooldown nodes) ──────


def _cooldown_active(node: dict[str, Any], now: datetime) -> bool:
    until = _parse_ts(node.get("cooldown_until_ts"))
    return until is not None and now < until


def _probe_result(node: dict[str, Any]) -> 'dict[str, str] | None':
    """Normalized configured probe result; no probe preserves legacy behavior."""
    probe = node.get("probe")
    if probe is None:
        return None
    if not isinstance(probe, dict):
        return {"state": "probe_unavailable", "details": "invalid probe result"}
    state = str(probe.get("state") or "")
    details = str(probe.get("details") or "")
    if state not in PROBE_STATES:
        return {"state": "probe_unavailable", "details": details or "invalid probe state"}
    return {"state": state, "details": details}


def _prerequisites(node: dict[str, Any]) -> list[str]:
    raw = node.get("prerequisites")
    if not isinstance(raw, list):
        return []
    return list(dict.fromkeys(str(value).strip() for value in raw if str(value).strip()))


def _prerequisite_cycles(nodes: dict[str, Any]) -> dict[str, list[str]]:
    """Map every node in a prerequisite cycle to one canonical cycle path."""
    found: dict[str, list[str]] = {}

    def visit(name: str, path: list[str]) -> None:
        if name in path:
            cycle = path[path.index(name):] + [name]
            canonical = min(tuple(cycle[i:-1] + cycle[:i] + [cycle[i]]) for i in range(len(cycle) - 1))
            normalized = list(canonical)
            for member in normalized[:-1]:
                found[member] = normalized
            return
        node = nodes.get(name)
        if not isinstance(node, dict):
            return
        for prerequisite in _prerequisites(node):
            if prerequisite in nodes:
                visit(prerequisite, path + [name])

    for name in sorted(nodes):
        visit(name, [])
    return found


def _eligibility(nodes: dict[str, Any]) -> tuple[set[str], list[dict[str, Any]]]:
    """Preference eligibility and visible frontier; never a permission decision."""
    cycles = _prerequisite_cycles(nodes)
    eligible: set[str] = set()
    frontier: list[dict[str, Any]] = []
    for name in sorted(nodes):
        node = nodes[name]
        cycle = cycles.get(name)
        if cycle:
            frontier.append({"name": name, "reason": "prerequisite_cycle", "cycle": cycle})
            continue
        probe = _probe_result(node)
        if probe is not None and probe["state"] != "present":
            frontier.append({
                "name": name,
                "reason": probe["state"],
                "probe_state": probe["state"],
                "details": probe["details"],
            })
            continue
        unmet = None
        unmet_state = None
        for prerequisite in _prerequisites(node):
            prerequisite_node = nodes.get(prerequisite)
            if not isinstance(prerequisite_node, dict):
                unmet, unmet_state = prerequisite, "absent"
                break
            prerequisite_probe = _probe_result(prerequisite_node)
            prerequisite_state = prerequisite_probe["state"] if prerequisite_probe is not None else "absent"
            if prerequisite_state != "present":
                unmet, unmet_state = prerequisite, prerequisite_state
                break
        if unmet is not None:
            frontier.append({
                "name": name,
                "reason": "unmet_prerequisite",
                "prerequisite": unmet,
                "prerequisite_state": unmet_state,
            })
            continue
        eligible.add(name)
    return eligible, frontier


def select_current_direction(
    state_dir: Any,
    *,
    epsilon: float = EPSILON,
    now: 'datetime | None' = None,
    rng: Any = None,
) -> 'str | None':
    """Pick the current investment direction, epsilon-greedy over eligible
    (non-plateaued, non-cooldown) nodes: with probability ``epsilon`` pick
    a random eligible node (explore), otherwise the highest
    :func:`node_mean_gain` node (exploit); ties broken toward the node
    with the FEWEST attempts (``len(gain_history)``) so a freshly-seeded
    or just-reactivated node is not starved by an established leader.

    If the PREVIOUSLY current node just crossed into :func:`is_plateaued`
    this pass, it is retired (``status: "plateaued"``, a
    :data:`COOLDOWN_HOURS` cooldown) and — if the new pick differs — the
    forced switch is recorded both in the sidecar's own bounded
    ``switches`` list and as a ``{"phase": "tech_tree", "reason":
    "plateau_switch"}`` ledger event. A plateaued node whose cooldown has
    elapsed is reactivated back into the eligible pool before selection
    runs (the OTHER re-entry path — a hypothesis targeting it — lives in
    :func:`maybe_mint_node`).

    ``rng`` defaults to the stdlib ``random`` module; tests may pass a
    fake object exposing ``.random()``/``.choice()`` for determinism.
    Returns the chosen node name, or ``None`` if there is no eligible node
    at all. Fail-open to ``None`` on any error."""
    try:
        ensure_seeded(state_dir, now=now)
        now = now or datetime.now(timezone.utc)
        rng = rng or random
        portfolio = read_portfolio(state_dir)
        nodes = portfolio["nodes"]
        if not nodes:
            return None

        prev_current = portfolio.get("current")
        switched_off_plateau = False
        if prev_current and prev_current in nodes:
            prev_node = nodes[prev_current]
            if prev_node.get("status") != "plateaued" and is_plateaued(prev_node):
                prev_node["status"] = "plateaued"
                prev_node["cooldown_until_ts"] = _iso(now + timedelta(hours=COOLDOWN_HOURS))
                switched_off_plateau = True

        # Cooldown-expired plateaued nodes re-enter the eligible pool.
        for node in nodes.values():
            if node.get("status") == "plateaued" and not _cooldown_active(node, now):
                node["status"] = "active"
                node["cooldown_until_ts"] = None

        preference_eligible, _frontier = _eligibility(nodes)
        eligible = sorted(
            name for name, node in nodes.items()
            if node.get("status") == "active" and name in preference_eligible
        )
        if not eligible:
            portfolio["current"] = None
            portfolio["nodes"] = nodes
            _write_portfolio(state_dir, portfolio)
            return None

        if rng.random() < epsilon:
            new_current = rng.choice(eligible)
        else:
            def _exploit_key(name: str) -> tuple[float, int]:
                node = nodes[name]
                return (-node_mean_gain(node), len(node.get("gain_history") or []))

            new_current = sorted(eligible, key=_exploit_key)[0]

        if switched_off_plateau and new_current != prev_current:
            switches = portfolio.get("switches")
            switches = list(switches) if isinstance(switches, list) else []
            switches.append({
                "ts": _iso(now),
                "from": prev_current,
                "to": new_current,
                "reason": "plateau_switch",
                "floor": PLATEAU_FLOOR,
            })
            if len(switches) > MAX_SWITCHES:
                switches = switches[-MAX_SWITCHES:]
            portfolio["switches"] = switches
            try:
                from nanobot.runtime.cycle_ledger import append_event

                append_event(state_dir, {
                    "phase": "tech_tree",
                    "reason": "plateau_switch",
                    "from": prev_current,
                    "to": new_current,
                    "floor": PLATEAU_FLOOR,
                })
            except Exception:
                pass

        portfolio["current"] = new_current
        portfolio["nodes"] = nodes
        _write_portfolio(state_dir, portfolio)
        return new_current
    except Exception:
        return None


def current_direction_status(state_dir: Any) -> tuple['str | None', str]:
    """Read current Direction plus ``complete``/``unavailable`` provenance.

    Existing rank-only consumers use :func:`current_direction` and retain its
    fail-open ``None`` behavior. Provenance consumers need the missing
    distinction: an absent portfolio/current is a known absent Direction,
    while a corrupt or inconsistent persisted portfolio is unavailable.
    """
    try:
        path = _portfolio_path(state_dir)
        if not path.is_file():
            return None, "complete"
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return None, "unavailable"
        current = raw.get("current")
        if current is None:
            return None, "complete"
        nodes = raw.get("nodes")
        if (
            not isinstance(current, str)
            or not current.strip()
            or not isinstance(nodes, dict)
            or current not in nodes
            or not isinstance(nodes[current], dict)
        ):
            return None, "unavailable"
        return current, "complete"
    except Exception:
        return None, "unavailable"


def current_direction(state_dir: Any) -> 'str | None':
    """Read-only view of the portfolio's current investment direction (or
    ``None``). Never seeds/mutates state — a pure read for
    ``goal_review``/``demand`` wiring, same shape as
    ``hypothesis_backlog.supported_hypotheses``. Fail-open to ``None``."""
    current, _status = current_direction_status(state_dir)
    return current


def direction_for_metric(state_dir: Any, metric: str) -> 'str | None':
    """Name of the node whose lever_metric's TAIL component (the bare
    metric name, e.g. ``"repeat_failure_rate"`` for
    ``"loop.repeat_failure_rate"``) equals ``metric``, or ``None``. Used by
    ``demand._goal_gap_items`` to tag a goal-gap item with the domain it
    naturally corresponds to (an exact string match — the one place this
    module has a precise, non-fuzzy correspondence to demand). Read-only.
    Fail-open to ``None``."""
    try:
        for name, node in (read_portfolio(state_dir).get("nodes") or {}).items():
            lever = str(node.get("lever_metric") or "")
            tail = lever.rsplit(".", 1)[-1] if lever else ""
            if tail and tail == metric:
                return name
        return None
    except Exception:
        return None


def matches_direction(text: str, state_dir: Any, direction_name: str) -> bool:
    """True iff ``text``'s normalized tokens overlap ``direction_name``'s
    node tokens (name + lever-metric tail) — the same crude token-overlap
    match :func:`maybe_mint_node` uses for hypothesis domain-mapping,
    reused by ``goal_review``'s soft candidate-ordering bias. Read-only.
    Fail-open to ``False``."""
    try:
        if not direction_name:
            return False
        node = (read_portfolio(state_dir).get("nodes") or {}).get(direction_name)
        if not isinstance(node, dict):
            return False
        return bool(_node_tokens(direction_name, node) & _tokens(text))
    except Exception:
        return False


# ─── minting from a supported hypothesis (#878 organic new-node source) ────


def maybe_mint_node(state_dir: Any, supported: 'list[dict[str, Any]] | None') -> 'str | None':
    """Given ``hypothesis_backlog.supported_hypotheses`` output, mint ONE
    new node for the first hypothesis whose domain is UNMAPPED (no token
    overlap with any existing node — :func:`_match_existing_node`),
    subject to a rate limit of one mint per :data:`MINT_MIN_INTERVAL_HOURS`
    and name-deduping against existing nodes.

    A hypothesis whose domain IS mapped to an existing node never mints a
    duplicate; if that matched node happens to be plateaued, it is instead
    reactivated (a hypothesis "targeting" it is itself evidence the
    direction may be worth another look — the other plateau re-entry path
    besides cooldown expiry).

    The minted node's lever defaults to ``loop.confirmed_integration_ratio``
    (higher-better) unless the hypothesis's own ``evidence`` names a
    ``metric`` this module recognizes (:data:`_KNOWN_LEVER_DIRECTIONS`) —
    documented crude v1 behavior, see the module docstring.

    Returns the minted node's name, or ``None`` if nothing was minted
    (nothing supported, everything mapped, or rate-limited). Fail-open."""
    try:
        if not supported:
            return None
        ensure_seeded(state_dir)
        portfolio = read_portfolio(state_dir)
        nodes = portfolio["nodes"]
        now = datetime.now(timezone.utc)
        now_iso = _iso(now)
        changed = False
        minted_name: 'str | None' = None

        last_mint = _parse_ts(portfolio.get("last_mint_ts"))
        rate_limited = last_mint is not None and (now - last_mint) < timedelta(hours=MINT_MIN_INTERVAL_HOURS)

        for hyp in supported:
            if not isinstance(hyp, dict):
                continue
            title = str(hyp.get("title") or "").strip()
            if not title:
                continue
            evidence = hyp.get("evidence") if isinstance(hyp.get("evidence"), dict) else {}
            metric_hint = str(evidence.get("metric") or "").strip() if evidence else ""

            # Domain-mapping matches on the TITLE only, deliberately NOT
            # the metric_hint: every metric this module currently
            # recognizes (_KNOWN_LEVER_DIRECTIONS) already belongs to an
            # existing seed node, so folding metric_hint into the match
            # text would make it self-defeating — a hypothesis naming a
            # known metric would always "match" that metric's OWNING node
            # via the metric name's own tokens, before the mint path could
            # ever use it as a fresh node's lever. metric_hint is used
            # ONLY below, to pick the lever for a genuinely new mint.
            matched = _match_existing_node(title, nodes)
            if matched:
                node = nodes[matched]
                if node.get("status") == "plateaued":
                    node["status"] = "active"
                    node["cooldown_until_ts"] = None
                    changed = True
                continue  # domain already mapped — no mint

            if minted_name is not None or rate_limited:
                continue  # already minted this pass, or rate-limited

            name = _slugify(title)
            if not name:
                continue
            name = _unique_node_name(name, nodes)

            lever_metric = metric_hint if metric_hint in _KNOWN_LEVER_DIRECTIONS else DEFAULT_MINT_LEVER
            direction = _KNOWN_LEVER_DIRECTIONS.get(lever_metric, "higher")
            nodes[name] = {
                "lever_metric": lever_metric,
                "direction": direction,
                "gain_history": [],
                "status": "active",
                "cooldown_until_ts": None,
                "minted_by": "hypothesis",
                "created_ts": now_iso,
                "last_lever_value": None,
                # #1686: the key is truncated/slugified for storage; the
                # full original hypothesis title is kept here so the
                # display label and the identity key can diverge without
                # losing the source text. Falls back to the key itself in
                # :func:`portfolio_snapshot` for any node lacking one (the
                # five hand-named seed nodes, and the two pre-#1686
                # truncated nodes this fix intentionally leaves as-is).
                "label": title,
            }
            portfolio["last_mint_ts"] = now_iso
            minted_name = name
            changed = True
            try:
                from nanobot.runtime.cycle_ledger import append_event

                append_event(state_dir, {
                    "phase": "tech_tree",
                    "reason": "node_minted",
                    "name": name,
                    "lever_metric": lever_metric,
                })
            except Exception:
                pass

        if changed:
            portfolio["nodes"] = nodes
            _write_portfolio(state_dir, portfolio)
        return minted_name
    except Exception:
        return None


# ─── control-plane / demand visibility ───────────────────────────────────────


def portfolio_snapshot(state_dir: Any) -> dict[str, Any]:
    """``{current, current_label, nodes, switches, frontier}`` visibility
    for preference state.

    Legacy portfolios with no probe/prerequisite fields keep the previous
    node payload otherwise unchanged; ``frontier: []`` and, as of #1686,
    ``label``/``current_label`` are additive. ``current``/``nodes`` keys
    remain the raw (possibly truncated, #1686) identity every other reader
    of this store compares against -- ``label`` is ONLY a display string,
    read by the operator dashboard instead of the key. A node minted before
    #1686, or a seed node, carries no stored label; it falls back to its
    own key/name, which is exactly what such a reader already prints today
    -- no worse, and the pre-#1686 truncated identifiers (grandfathered,
    not migrated -- their source text is not recoverable) look the same as
    before. Read by ``scorecard._control_plane_snapshot``-style visibility
    wiring. Fail-open to ``{}``."""
    try:
        portfolio = read_portfolio(state_dir)
        nodes = portfolio.get("nodes") or {}
        _eligible, frontier = _eligibility(nodes)
        nodes_out: dict[str, Any] = {}
        for name, node in nodes.items():
            payload = {
                "status": node.get("status"),
                "mean_gain": round(node_mean_gain(node), 6),
                "attempts": len(node.get("gain_history") or []),
                "lever_metric": node.get("lever_metric"),
                "label": node.get("label") or name,
            }
            probe = _probe_result(node)
            if probe is not None:
                payload["probe"] = probe
            prerequisites = _prerequisites(node)
            if prerequisites:
                payload["prerequisites"] = prerequisites
            nodes_out[name] = payload
        current = portfolio.get("current")
        current_node = nodes.get(current) if isinstance(current, str) else None
        current_label = (current_node or {}).get("label") or current
        return {
            "current": current,
            "current_label": current_label,
            "nodes": nodes_out,
            "switches": len(portfolio.get("switches") or []),
            "frontier": frontier,
        }
    except Exception:
        return {}
