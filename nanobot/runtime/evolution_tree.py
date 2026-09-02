"""Git-native evolutionary tree — RSI stage 3 (#877).

Operator framing: memory IS files+git already. Population = branches,
generation = commit, fitness = one ledger entry per node. This module adds
NOTHING new to that machinery — no MAP-Elites grid, no population manager,
no new daemon. It is a thin bookkeeping sidecar over the EXISTING
``bridge.py`` git primitives (``_setup_cycle_branch`` /
``_integrate_cycle_to_main``):

- ``record_node`` writes one entry per integrated cycle to
  ``state/evolution/tree.json`` (capped at :data:`MAX_NODES`).
- ``should_switch`` is the ONE trigger that lets the loop move along the
  tree: when ``archive.CycleArchive.stalled()`` says the current line has
  regressed/plateaued (last 5 cycles all reward < 0.8, #844), the bridge
  may branch its next cycle off a stronger DORMANT line instead of the
  live tip — "rollback to an ancestor, return to a stronger line" via a
  native ``git checkout -B <cycle> <target sha>``, nothing more exotic.
- ``tree_indexed_shas`` lets the bridge's branch-pruning avoid deleting a
  branch the tree still points at.
- ``mark_switch_blocked`` flags a node the bridge discovered was poisoned
  (see the RED-1 fix below) so it is never offered as a switch target
  again.

Sequential evaluation, parallel STRUCTURE: only one line is ever live
(evaluated) at a time — parallelism lives in the dormant branches
(``evo/node-*`` keeper refs bridge.py creates before abandoning a tip),
never in concurrent execution.

Trust boundary (read before wiring this into anything else): ``tree.json``
lives under ``state/`` and is therefore instance-writable, exactly like
every other bridge sidecar (demand/completed.json, scorecard/latest.json,
...). It is listed in ``scorecard.FITNESS_SIDECARS`` (#789) so tampering
with it is DETECTED (spawn-boundary hash mismatch) the same way tampering
with any other fitness input is detected. But selecting a base sha is a
STEERING decision, not a verification one: a forged tree can, at worst,
point the next cycle's branch at a different commit inside the instance's
OWN repo — and the bridge (``_setup_cycle_branch`` / the pre-integration
base-surface gate in the main cycle flow) additionally RE-CLASSIFIES the
full surface of ``real_origin_main..HEAD`` (not just the per-cycle delta)
immediately before ever pushing, so a switch onto a poisoned dormant/
forensic sha (one carrying a deny-set/runtime/mutation-surface violation
that kept it from integrating in the first place) is hard-blocked at
integration time, never force-pushed onto ``origin/main`` — see
``bridge.py``'s cycle-flow docstring near its ``_integrate_cycle_to_main``
call site for the exact mechanism. Every cycle branched from a base that
DOES pass this re-classification still runs through the full, unweakened
gate (smoke tests + deny-set/mutation-surface check + held-out pack), and
runtime-slice promotion still requires the independent root verifier
(#875). ``node_score()`` is therefore deliberately crude (see its
docstring) — it must never be read as a trust/verification signal anywhere
else in the codebase, only as "which dormant line looks least bad to try
next".

Switch dampening (YELLOW-1 fix): while ``CycleArchive.stalled()`` stays
True, ``select_switch_target`` will not re-offer a sha that appears as the
``to_sha`` of any of the last :data:`_SWITCH_COOLDOWN` (3) entries in
``tree.json``'s ``switches`` list — without this, a persistently-stalled
archive would re-select (and force-push) the SAME target every single
cycle with no forward progress. It still happily switches to a
DIFFERENT good candidate if one exists; the cooldown only suppresses
immediate repeats of one specific target.

Stdlib-only, harness-owned. Every public function here is FAIL-OPEN: any
error (missing file, corrupt json, bad argument, disk issue) degrades to
``None``/no-op/empty-collection. A bug in this module must NEVER block,
slow down, or crash a bridge cycle.
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "evolution-tree-v1"
_TREE_SUBPATH = "evolution/tree.json"

# Cap on how many generations (nodes) the tree remembers. Beyond this, the
# lowest-node_score, oldest-among-ties entries are evicted first — never
# the live tip or its recent ancestry (see _KEEP_ANCESTOR_HOPS).
MAX_NODES = 100

# Bounded audit trail of line switches kept inside tree.json itself
# (separate from, and smaller than, the cycle ledger's own
# `phase: evolution_tree` rows, which are the durable record).
MAX_SWITCHES = 20

# YELLOW-1 dampening: a sha that was the `to_sha` of any of the last this-
# many `switches` entries is skipped by select_switch_target — prevents
# thrash/re-force-push of the same target every cycle while stalled().
_SWITCH_COOLDOWN = 3

# Never evict current_sha or its last N ancestors when trimming to MAX_NODES
# — losing the live line's own history would make _ancestor_chain-based
# bookkeeping (and simple human inspection) far less useful for no reason.
_KEEP_ANCESTOR_HOPS = 5

# Hard hop limit for bounded git rev-list traversal when finding nearest ancestor node.
_MAX_GIT_ANCESTRY_HOPS = 50


def _tree_path(state_dir: Any) -> Path:
    return Path(state_dir) / _TREE_SUBPATH


def _empty_tree() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "current_sha": None,
        "nodes": {},
        "switches": [],
    }


def _iso(dt: 'datetime | None' = None) -> str:
    dt = dt or datetime.now(timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


def read_tree(state_dir: Any) -> dict[str, Any]:
    """Return the parsed ``tree.json``, normalized to the v1 schema shape.

    Any trouble at all (missing file, corrupt json, wrong top-level type,
    malformed sub-fields) degrades to a fresh empty tree — never raises.
    """
    try:
        path = _tree_path(state_dir)
        if not path.is_file():
            return _empty_tree()
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return _empty_tree()
        tree = _empty_tree()
        current = raw.get("current_sha")
        tree["current_sha"] = current if isinstance(current, str) and current else None
        nodes = raw.get("nodes")
        if isinstance(nodes, dict):
            tree["nodes"] = {str(k): v for k, v in nodes.items() if isinstance(v, dict)}
        switches = raw.get("switches")
        if isinstance(switches, list):
            tree["switches"] = [s for s in switches if isinstance(s, dict)]
        return tree
    except Exception:
        return _empty_tree()


# A tree is bounded by MAX_NODES / MAX_SWITCHES (~50 KB live); anything past
# this was not written by this module and is not overwritten by it (#1178).
_TREE_MAX_BYTES = 16 * 1024 * 1024


def _write_tree(state_dir: Any, tree: dict[str, Any]) -> None:
    path = _tree_path(state_dir)
    # #1178 Class B: the read that produced ``tree`` returns a blank default
    # on a corrupt/oversize/unreadable file; writing that back would erase the
    # history. Skip and say so; an absent file is created normally.
    from nanobot.runtime.state_access import WRITABLE_STATUSES, rewrite_status

    status = rewrite_status(path, max_bytes=_TREE_MAX_BYTES)
    if status not in WRITABLE_STATUSES:
        import logging

        logging.getLogger(__name__).warning("evolution_tree: write skipped, existing file is %s: %s", status, path)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(tree, indent=2, ensure_ascii=False), encoding="utf-8")


def _ancestor_chain(tree: dict[str, Any], sha: 'str | None', hops: int) -> 'set[str]':
    """Return up to ``hops`` ANCESTORS of ``sha`` (its parent, grandparent,
    ... — never ``sha`` itself; callers union ``{sha}`` in separately) by
    walking ``parent_sha`` links through the tree's OWN nodes dict (never
    touches git). Fail-open to whatever partial chain was found before a
    break/missing link."""
    out: 'set[str]' = set()
    nodes = tree.get("nodes") or {}
    cur = nodes.get(sha, {}).get("parent_sha") if sha in nodes else None
    for _ in range(hops):
        if not cur or cur not in nodes:
            break
        out.add(cur)
        cur = nodes[cur].get("parent_sha")
    return out


def _git_ancestry_chain(
    repo_root: Any,
    start_sha: str,
    max_hops: int = _MAX_GIT_ANCESTRY_HOPS,
) -> list[str]:
    """Return bounded git ancestor commits starting from start_sha.

    Bounded by max_hops, fail-open to [] on any git error or missing repository.
    """
    if not repo_root or not start_sha:
        return []
    try:
        root_path = Path(repo_root)
        if not root_path.exists():
            return []
        res = subprocess.run(
            ["git", "-C", str(root_path), "rev-list", f"--max-count={max_hops}", str(start_sha)],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        if res.returncode != 0 or not res.stdout:
            return []
        return [line.strip() for line in res.stdout.splitlines() if line.strip()]
    except Exception:
        return []


def resolve_ancestor_node(
    state_dir: Any,
    raw_parent_sha: 'str | None',
    *,
    tree: 'dict[str, Any] | None' = None,
    repo_root: Any = None,
    max_hops: int = _MAX_GIT_ANCESTRY_HOPS,
    allow_current_sha_fallback: bool = True,
) -> 'str | None':
    """Resolve the nearest existing tree node sha for a given raw parent_sha.

    1. If raw_parent_sha is already a known node in the tree, return it immediately.
    2. Otherwise, if repo_root is available and valid, walk git rev-list bounded by
       max_hops. The first commit that exists in tree.nodes is returned.
    3. Fallback: tree['current_sha'] if allow_current_sha_fallback is True and it exists in tree.nodes.
    4. Fallback: raw_parent_sha itself.
    """
    if tree is None:
        tree = read_tree(state_dir)
    nodes = tree.get("nodes") or {}

    # Case 1: direct hit (or raw_parent_sha is None)
    if not raw_parent_sha:
        return None
    if raw_parent_sha in nodes:
        return raw_parent_sha

    # Case 2: walk git ancestry if repo_root provided
    if repo_root:
        ancestors = _git_ancestry_chain(repo_root, raw_parent_sha, max_hops=max_hops)
        for anc in ancestors:
            if anc in nodes:
                return anc

    # Case 3: fallback to tree.current_sha (only when recording new node, not during orphan migration)
    if allow_current_sha_fallback:
        cur = tree.get("current_sha")
        if cur and cur in nodes:
            return cur

    # Case 4: raw parent_sha
    return raw_parent_sha


def _protected_eviction_shas(tree: dict[str, Any], sha: str) -> set[str]:
    """Calculate protected node shas that should not be evicted.

    Protected:
    1. Active tip (`sha`) and its recent tree ancestors (_KEEP_ANCESTOR_HOPS).
    2. Fork nodes: nodes that have >= 2 children in the current tree.
       Specifically: any node that is parent_sha to >= 2 OTHER distinct child nodes in tree['nodes'].
    3. Switch nodes: any node whose sha is retained as from_sha or to_sha in tree['switches'].
    """
    nodes = tree.get("nodes") or {}
    protected = {sha} | _ancestor_chain(tree, sha, _KEEP_ANCESTOR_HOPS)

    # Fork protection: count live children per parent_sha among existing nodes
    child_counts: dict[str, int] = {}
    for child_sha, n in nodes.items():
        p = n.get("parent_sha")
        if p and p in nodes and p != child_sha:
            child_counts[p] = child_counts.get(p, 0) + 1
    for p_sha, count in child_counts.items():
        if count >= 2:
            protected.add(p_sha)

    # Switch protection: retained switches
    for sw in tree.get("switches") or []:
        if isinstance(sw, dict):
            f_sha = sw.get("from_sha")
            t_sha = sw.get("to_sha")
            if f_sha and f_sha in nodes:
                protected.add(f_sha)
            if t_sha and t_sha in nodes:
                protected.add(t_sha)

    return protected


def node_score(node: dict[str, Any]) -> float:
    """Deliberately crude v1 fitness proxy for ONE purpose only — ranking
    dormant lines against each other in :func:`select_switch_target`.

    ``reward + 0.1*confirmed_integrations - 0.2*repeat_failure_rate``,
    with every missing/unreadable field treated as 0. This is NOT a trust
    or verification input anywhere else in the codebase (see module
    docstring) — it never gates a cycle, never feeds scorecard targets,
    and a forged value here can only ever influence which git sha the
    NEXT cycle branches from. Fail-open to ``0.0`` on any bad shape.
    """
    try:
        fitness = node.get("fitness") or {}
        reward = fitness.get("reward")
        reward = float(reward) if reward is not None else 0.0
        confirmed = fitness.get("confirmed_integrations")
        confirmed = float(confirmed) if confirmed is not None else 0.0
        repeat_fail = fitness.get("repeat_failure_rate")
        repeat_fail = float(repeat_fail) if repeat_fail is not None else 0.0
        return reward + 0.1 * confirmed - 0.2 * repeat_fail
    except Exception:
        return 0.0


def _fitness_from_scorecard(state_dir: Any, reward: 'float | None') -> dict[str, Any]:
    """Best-effort fitness snapshot for a new node.

    Reads ``<state_dir>/scorecard/latest.json`` DIRECTLY as json (no import
    of ``scorecard.py`` — this module stays a leaf dependency) and folds in
    the caller-supplied ``reward``. Fail-open to a fitness dict of
    ``None``s (plus whatever ``reward`` was passed) on any error.
    """
    out: dict[str, Any] = {
        "reward": reward,
        "integrations": None,
        "confirmed_integrations": None,
        "repeat_failure_rate": None,
    }
    try:
        path = Path(state_dir) / "scorecard" / "latest.json"
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            loop = data.get("loop") if isinstance(data, dict) else None
            if isinstance(loop, dict):
                out["integrations"] = loop.get("integrations")
                out["confirmed_integrations"] = loop.get("confirmed_integrations")
                out["repeat_failure_rate"] = loop.get("repeat_failure_rate")
    except Exception:
        pass
    return out


def record_node(
    state_dir: Any,
    *,
    sha: str,
    parent_sha: 'str | None',
    branch: str,
    cycle_id: str,
    reward: 'float | None' = None,
    repo_root: Any = None,
) -> None:
    """Record one generation (one integrated cycle) in the tree.

    Sets ``current_sha`` to ``sha``. Fitness fields are best-effort, pulled
    from ``scorecard/latest.json`` plus the caller-supplied ``reward``
    (later cycles can backfill a real reward once one is known — kept
    simple for v1). Evicts down to :data:`MAX_NODES` when exceeded,
    preferring the lowest :func:`node_score` and, among ties, the oldest
    entry — never ``sha`` itself nor its last :data:`_KEEP_ANCESTOR_HOPS`
    ancestors, fork nodes (>=2 children), or retained switch endpoints.
    Appends a ``phase: "evolution_tree"`` / ``reason: "node_recorded"``
    cycle-ledger event.

    No-op on a falsy ``sha``. Fail-open: never raises, never blocks the
    calling cycle.
    """
    if not sha:
        return
    try:
        tree = read_tree(state_dir)
        observed_parent = parent_sha
        resolved_parent = resolve_ancestor_node(
            state_dir,
            parent_sha,
            tree=tree,
            repo_root=repo_root,
        )

        tree["nodes"][sha] = {
            "parent_sha": resolved_parent,
            "observed_parent_sha": observed_parent,
            "branch": str(branch or ""),
            "cycle_id": str(cycle_id or ""),
            "ts": _iso(),
            "fitness": _fitness_from_scorecard(state_dir, reward),
        }
        tree["current_sha"] = sha

        if len(tree["nodes"]) > MAX_NODES:
            protected = _protected_eviction_shas(tree, sha)
            evictable = [(s, n) for s, n in tree["nodes"].items() if s not in protected]
            # lowest node_score first; ties broken oldest-first (ts ascending)
            evictable.sort(key=lambda item: (node_score(item[1]), item[1].get("ts") or ""))
            while len(tree["nodes"]) > MAX_NODES and evictable:
                victim_sha, _victim_node = evictable.pop(0)
                tree["nodes"].pop(victim_sha, None)

            # Fallback if all nodes are protected but we are still above MAX_NODES:
            # allow evicting any node other than the active tip (`sha`), lowest score/oldest first
            if len(tree["nodes"]) > MAX_NODES:
                all_except_tip = [(s, n) for s, n in tree["nodes"].items() if s != sha]
                all_except_tip.sort(key=lambda item: (node_score(item[1]), item[1].get("ts") or ""))
                while len(tree["nodes"]) > MAX_NODES and all_except_tip:
                    victim_sha, _victim_node = all_except_tip.pop(0)
                    tree["nodes"].pop(victim_sha, None)

        _write_tree(state_dir, tree)

        from nanobot.runtime.cycle_ledger import append_event

        append_event(state_dir, {
            "phase": "evolution_tree",
            "reason": "node_recorded",
            "sha": sha,
            "parent_sha": resolved_parent,
            "observed_parent_sha": observed_parent,
            "cycle_id": cycle_id,
        })
    except Exception:
        pass


def select_switch_target(state_dir: Any, current_sha: 'str | None') -> 'tuple[str, str] | None':
    """Best dormant line to switch to, or ``None``.

    Candidates are every tree node whose sha differs from ``current_sha``
    (the current line tip is never its own switch target), EXCLUDING:

    - nodes flagged ``blocked`` (RED-1 fix — set by
      :func:`mark_switch_blocked` after the bridge discovers a switch
      target's own surface was poisoned; never re-offered), and
    - any sha that was the ``to_sha`` of one of the last
      :data:`_SWITCH_COOLDOWN` ``switches`` entries (YELLOW-1 fix —
      dampens back-to-back re-switching to the same target while the
      archive stays stalled).

    Ranked by :func:`node_score` descending; ties broken by newest ``ts``.
    Returns ``None`` when the tree has fewer than 2 nodes total (nothing
    to compare against), when every OTHER node is excluded by the above,
    or on any error. Fail-open.
    """
    try:
        tree = read_tree(state_dir)
        nodes = tree.get("nodes") or {}
        if len(nodes) < 2:
            return None
        recent_targets = {
            sw.get("to_sha")
            for sw in (tree.get("switches") or [])[-_SWITCH_COOLDOWN:]
            if isinstance(sw, dict)
        }
        candidates = [
            (s, n) for s, n in nodes.items()
            if s != current_sha and not n.get("blocked") and s not in recent_targets
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda item: (node_score(item[1]), item[1].get("ts") or ""), reverse=True)
        best_sha, best_node = candidates[0]
        return best_sha, str(best_node.get("branch") or "")
    except Exception:
        return None


def should_switch(
    state_dir: Any, archive_stalled: bool, current_sha: 'str | None',
) -> 'tuple[str, str] | None':
    """The ONE trigger for a line switch.

    Deliberately does not add a second heuristic: ``archive.CycleArchive.
    stalled()`` (last 5 cycles all reward < 0.8, #844) already covers
    "regression -> low rewards -> stalled". When ``archive_stalled`` is
    True, delegates to :func:`select_switch_target`; otherwise returns
    ``None`` immediately. Fail-open to ``None``.
    """
    if not archive_stalled:
        return None
    try:
        return select_switch_target(state_dir, current_sha)
    except Exception:
        return None


def tree_indexed_shas(state_dir: Any) -> 'set[str]':
    """All shas currently recorded as tree nodes.

    Used by the bridge's cycle-branch pruning (#830/#877) so it never
    deletes a branch the evolution tree still points at. Fail-open to an
    empty set.
    """
    try:
        tree = read_tree(state_dir)
        return set((tree.get("nodes") or {}).keys())
    except Exception:
        return set()


def current_sha(state_dir: Any) -> 'str | None':
    """The tree's ``current_sha``, or ``None`` on any error/absence. Fail-open."""
    try:
        return read_tree(state_dir).get("current_sha")
    except Exception:
        return None


def record_switch(state_dir: Any, *, from_sha: str, to_sha: str, reason: str) -> None:
    """Append one bounded switch record to ``tree.json``'s ``switches``
    list (capped at :data:`MAX_SWITCHES`, oldest dropped first) — a small
    audit trail alongside the durable cycle-ledger ``line_switch`` event
    bridge.py also appends. Fail-open no-op on any error.
    """
    try:
        tree = read_tree(state_dir)
        tree["switches"].append({
            "ts": _iso(),
            "from_sha": from_sha,
            "to_sha": to_sha,
            "reason": reason,
        })
        if len(tree["switches"]) > MAX_SWITCHES:
            tree["switches"] = tree["switches"][-MAX_SWITCHES:]
        _write_tree(state_dir, tree)
    except Exception:
        pass


def mark_switch_blocked(state_dir: Any, sha: str, reason: str = "") -> None:
    """Flag a tree node as never-to-be-offered-again by
    :func:`select_switch_target` (RED-1 fix).

    Called by the bridge AFTER it discovers, at the pre-integration
    base-surface gate, that a switch target's own surface (relative to
    the real ``origin/main``) carries a deny-set/runtime/mutation-surface
    violation — a forged-tree-node or poisoned-forensic-branch scenario.
    Without this, ``should_switch`` would keep re-selecting the SAME
    poisoned sha every cycle for as long as the archive stays stalled
    (nothing about attempting and blocking the switch changes its
    ``node_score``). No-op if ``sha`` has no node (nothing to flag) or on
    any error. Fail-open — never raises.
    """
    if not sha:
        return
    try:
        tree = read_tree(state_dir)
        node = (tree.get("nodes") or {}).get(sha)
        if node is None:
            return
        node["blocked"] = True
        node["blocked_reason"] = str(reason or "")
        _write_tree(state_dir, tree)
    except Exception:
        pass


def migrate_tree_ancestry(
    state_dir: Any,
    repo_root: Any = None,
    max_hops: int = _MAX_GIT_ANCESTRY_HOPS,
    dry_run: bool = False,
) -> dict[str, int]:
    """Repair missing/orphan parent_sha links in tree.json using git ancestry.

    Idempotent, fail-open. Returns stats:
    {"total_nodes": N, "repaired": R, "already_linked": A, "unresolved": U}
    """
    stats = {"total_nodes": 0, "repaired": 0, "already_linked": 0, "unresolved": 0}
    try:
        tree = read_tree(state_dir)
        nodes = tree.get("nodes") or {}
        stats["total_nodes"] = len(nodes)
        if not nodes:
            return stats

        changed = False
        for sha, node in nodes.items():
            if not isinstance(node, dict):
                continue
            parent = node.get("parent_sha")
            # If parent already exists in tree (and is not None), it's validly linked
            # Root node (parent_sha is None) is also considered already validly linked / root
            if parent is None:
                stats["already_linked"] += 1
                continue
            if parent in nodes:
                stats["already_linked"] += 1
                continue

            # Need repair: start search from observed_parent_sha if set, else parent_sha or sha
            start_sha = node.get("observed_parent_sha") or parent or sha
            resolved = resolve_ancestor_node(
                state_dir,
                start_sha,
                tree=tree,
                repo_root=repo_root,
                max_hops=max_hops,
                allow_current_sha_fallback=False,
            )

            # If resolved ancestor is in tree and not self
            if "observed_parent_sha" not in node and parent:
                node["observed_parent_sha"] = parent
            if resolved and resolved in nodes and resolved != sha:
                node["parent_sha"] = resolved
                stats["repaired"] += 1
                changed = True
            else:
                stats["unresolved"] += 1

        if changed and not dry_run:
            _write_tree(state_dir, tree)

        return stats
    except Exception:
        return stats
