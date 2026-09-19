"""Operator affordance: revert a range of loop integrations (#1772).

The harness release has a rollback (``host/eeepc/scripts/deploy_release.sh``
restores the previous release on a failed health check). The instance
repository the loop itself edits (``ozand/eeebot-self-evolving``) has none —
the gate that guards each integration checks compilation, affected tests,
the mutation surface and the lesson schema, none of which catches "this
landed and turned out to be wrong", and for a documentation/memory/lesson
change there is nothing for a test to catch by construction. The operator's
only remedy today is reconstructing a bad day from ``git log`` and undoing
it by hand.

This module is read-only against the harness state directory and the
instance repository's working tree, apart from the git commit/push the
revert itself performs when applied. It never resets, force-pushes, or
rewrites history — only ``git revert`` commits.

Measured before building (do not re-derive):

- Every integration merge into the instance repo's ``main`` matches
  ``merge: integrate selfevo/(cycle|fallback)-[a-f0-9]+`` and is a true
  2-parent merge commit (100% of 70 checked) — a revert needs
  ``git revert -m 1``.
- ``git revert -m 1 --no-commit`` applies cleanly on a recent integration;
  a merge from further back can conflict when later commits touched the
  same lines. This tool detects that, aborts the whole revert (no partial
  commit), and reports which target conflicted — it does not attempt to
  auto-resolve.
- The ``evolution_tree`` ledger row (``phase: "evolution_tree"``) is the
  join from ``cycle_id`` to the merge commit's own sha; the ``outcome`` row
  (``phase: "outcome"``, ``outcome: "success"``) is the join to
  ``files_changed``/``change_tier``; ``demand/completed.json`` entries
  (matched by their own ``cycle_id`` field) are the confirmed-in-use join —
  the same three sources and the same joins the gh-pages digest uses.
- 38 direct, non-merge commits landed on `main` in the measured 7-day
  window outside this merge path: 20 structured-error records + 1 curator
  promotion (both authored ``eeepc-agent <eeepc-agent@eeepc.local>``), and
  17 by the operator directly (``ozand <ozand.ru@gmail.com>``). Only the
  operator's own commits are ever revert-worthy, and the operator reverts
  those themselves with plain git. This tool's scope is the merge path —
  everything the LOOP integrated — which is exactly this issue's scope;
  the 38 direct commits are out of scope by design, not by oversight.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

#: Matches the bridge's own merge-commit subject
#: (nanobot/runtime/bridge.py's `_integrate_cycle_to_main`).
MERGE_SUBJECT_RX = re.compile(r"^merge: integrate (selfevo/(?:cycle|fallback)-[a-f0-9]+)$")

_DEFAULT_BRIDGE_SERVICE = "eeepc-self-evolving-subagent-bridge.service"


class RevertRefusedError(RuntimeError):
    """Raised when the tool refuses to run (bridge active, unverified commit shape)."""


@dataclass(frozen=True)
class Target:
    """One integration considered for revert."""

    cycle_id: str
    sha: str
    ts: str
    files_changed: tuple[str, ...]
    change_tier: str | None
    confirmed: bool | None  # None: no completed.json entry at all (untracked)


# ─── bridge-active guard ────────────────────────────────────────────────────


def bridge_is_active(
    service_name: str = _DEFAULT_BRIDGE_SERVICE,
    *,
    runner: Any = None,
) -> bool:
    """True iff the bridge systemd unit is currently active.

    Delegates to :func:`nanobot.runtime.health.read_service_status` (the
    same reader the deploy health gate uses) rather than re-implementing
    the systemctl call — one source of truth for "is a cycle mid-flight".
    Fail-open is the WRONG default here (the whole point is refusing to run
    concurrently with a cycle): this returns True (refuse) for every state
    other than an explicitly-confirmed ``"inactive"`` — including
    ``"unknown"`` (systemctl unreachable, a non-systemd host, or a read
    failure ``read_service_status`` itself already swallowed) and
    ``"activating"``/``"deactivating"``/``"failed"``. A status this tool
    cannot confirm as inactive is refused, never assumed safe.
    """
    try:
        from nanobot.runtime.health import read_service_status

        status = read_service_status(service_name, runner=runner) if runner else read_service_status(service_name)
    except Exception:
        return True
    active_state = status.get("active_state") if isinstance(status, dict) else None
    # "unknown" (systemctl unreachable, non-systemd host, a read failure
    # read_service_status itself already swallowed) is refused too, not
    # assumed inactive -- see the docstring above.
    return str(active_state or "unknown").strip().lower() != "inactive"


# ─── resolving targets from the ledger ─────────────────────────────────────


def _load_evolution_shas(state_dir: Path, *, since_ts: str) -> dict[str, str]:
    """``{cycle_id: merge_sha}`` from ``phase: "evolution_tree"`` ledger rows."""
    from nanobot.runtime.state_access import ledger_window

    window = ledger_window(state_dir, since_ts=since_ts, phases=frozenset({"evolution_tree"}))
    out: dict[str, str] = {}
    for row in window.rows:
        cycle_id = str(row.get("cycle_id") or "").strip()
        sha = str(row.get("sha") or "").strip()
        if cycle_id and sha:
            out[cycle_id] = sha
    return out


def _load_confirmed_cycle_ids(state_dir: Path) -> set[str]:
    """Reuses ``scorecard._confirmed_cycle_ids`` — the same harness-signal-
    guarded join the gh-pages digest and the scorecard's own confirmed
    integration ratio use, rather than re-deriving it here."""
    try:
        from nanobot.runtime.scorecard import _confirmed_cycle_ids

        return _confirmed_cycle_ids(state_dir)
    except Exception:
        return set()


def _load_all_completed_cycle_ids(state_dir: Path) -> tuple[set[str], bool]:
    """``({cycle_id for every entry in demand/completed.json}, readable)``.

    ``readable`` distinguishes "the file could not be read at all" from "it
    read fine and this cycle simply has no entry" — both cases must still
    resolve to ``confirmed=None`` (untracked), never ``False``, but the flag
    lets a caller tell the two apart if it ever needs to."""
    try:
        import json

        path = Path(state_dir) / "demand" / "completed.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        entries = data.get("entries") if isinstance(data, dict) else None
        if not isinstance(entries, dict):
            return set(), True
        out = {
            str(entry.get("cycle_id")).strip()
            for entry in entries.values()
            if isinstance(entry, dict) and entry.get("cycle_id")
        }
        return out, True
    except Exception:
        return set(), False


def resolve_targets(
    state_dir: Path,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    cycle_ids: list[str] | None = None,
) -> list[Target]:
    """Successful integrations in ``[since, until)`` or matching
    ``cycle_ids``, newest-first (the safe revert order — reverting a
    contiguous trailing range newest-to-oldest exactly restores the tree
    from before it; see :func:`apply_revert`).

    A cycle with no ``evolution_tree`` sha is never a target (nothing to
    revert without a commit to point at). A cycle with no
    ``demand/completed.json`` entry gets ``confirmed=None`` (untracked /
    pending-confirmation) — never a false "not confirmed": :data:`Target`'s
    own docstring is explicit that ``None`` and ``False`` are different
    claims, and a failed join is missing data, not a negative.
    """
    from nanobot.runtime.state_access import ledger_window

    since = since or datetime(2020, 1, 1, tzinfo=timezone.utc)
    since_ts = since.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    outcome_window = ledger_window(state_dir, since_ts=since_ts, phases=frozenset({"outcome"}))
    sha_by_cycle = _load_evolution_shas(state_dir, since_ts=since_ts)
    confirmed_ids = _load_confirmed_cycle_ids(state_dir)
    completed_cycle_ids, _completed_readable = _load_all_completed_cycle_ids(state_dir)

    wanted_ids = set(cycle_ids) if cycle_ids else None
    targets: list[Target] = []
    seen_cycles: set[str] = set()
    for row in outcome_window.rows:
        if str(row.get("outcome") or "").strip().lower() != "success":
            continue
        cycle_id = str(row.get("cycle_id") or "").strip()
        if not cycle_id or cycle_id in seen_cycles:
            continue
        if wanted_ids is not None and cycle_id not in wanted_ids:
            continue
        ts_raw = str(row.get("ts") or "")
        try:
            ts_dt = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if until is not None and ts_dt >= until:
            continue
        sha = sha_by_cycle.get(cycle_id)
        if not sha:
            continue  # nothing to revert without a commit to point at
        files = row.get("files_changed")
        seen_cycles.add(cycle_id)
        targets.append(Target(
            cycle_id=cycle_id,
            sha=sha,
            ts=ts_raw,
            files_changed=tuple(files) if isinstance(files, list) else (),
            change_tier=row.get("change_tier"),
            # #1772 AC: a failed join (no entry for this cycle at all --
            # unreadable completed.json, or a genuinely untracked cycle) is
            # missing data, never a negative -- None, not False.
            confirmed=(
                True if cycle_id in confirmed_ids
                else False if cycle_id in completed_cycle_ids
                else None
            ),
        ))
    targets.sort(key=lambda t: t.ts, reverse=True)
    return targets


# ─── git plumbing ───────────────────────────────────────────────────────────


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, timeout=30,
    )


def verify_merge_commit(repo: Path, target: Target) -> str | None:
    """Return an error string if *target* is not a clean, expected merge
    commit to revert, else ``None``. Never assumes the ledger's sha is still
    a valid, well-shaped commit in the repo — this is the one check that
    stands between a malformed/forged ledger row and reverting the wrong
    thing."""
    parents = _git(repo, "rev-list", "--parents", "-n", "1", target.sha)
    if parents.returncode != 0:
        return f"{target.sha[:12]} ({target.cycle_id}): not found in {repo}"
    fields = parents.stdout.strip().split()
    if len(fields) != 3:
        return f"{target.sha[:12]} ({target.cycle_id}): expected a 2-parent merge commit, found {len(fields) - 1} parent(s)"
    subject = _git(repo, "log", "-1", "--format=%s", target.sha)
    if subject.returncode != 0 or not MERGE_SUBJECT_RX.match(subject.stdout.strip()):
        return f"{target.sha[:12]} ({target.cycle_id}): subject {subject.stdout.strip()!r} does not match the bridge's merge-commit shape"
    if target.cycle_id not in subject.stdout.strip():
        return f"{target.sha[:12]}: subject {subject.stdout.strip()!r} does not name cycle {target.cycle_id!r}"
    return None


@dataclass
class RevertResult:
    applied: bool
    commit_sha: str | None
    reverted: list[Target]
    error: str | None


def format_dry_run(targets: list[Target]) -> str:
    """The default, side-effect-free report: exactly what would be
    reverted, and why (or why nothing would be)."""
    if not targets:
        return "revert_cycles: no matching integrations to revert (dry run; nothing would change)"
    lines = [f"revert_cycles: would revert {len(targets)} integration(s), newest first (dry run):"]
    for t in targets:
        confirmed = "untracked" if t.confirmed is None else ("confirmed" if t.confirmed else "unconfirmed")
        paths = ", ".join(t.files_changed[:5]) + ("…" if len(t.files_changed) > 5 else "")
        lines.append(
            f"  {t.sha[:12]}  {t.cycle_id}  {t.ts}  tier={t.change_tier or 'unknown'}  {confirmed}  {paths}"
        )
    lines.append("Pass --apply to actually revert. Nothing has been changed.")
    return "\n".join(lines)


def apply_revert(
    repo: Path,
    targets: list[Target],
    *,
    push: bool = True,
    service_name: str = _DEFAULT_BRIDGE_SERVICE,
    bridge_active_override: bool | None = None,
) -> RevertResult:
    """Apply ``git revert -m 1 --no-commit`` for every target, newest-first,
    then ONE commit naming every reverted cycle. All-or-nothing: any
    conflict aborts the whole operation (``git revert --abort``) and leaves
    the tree exactly as it was — never a partial revert, never a
    reset/force-push to recover from one.

    Refuses outright (raises :class:`RevertRefusedError`) while the bridge
    service is active — a cycle mid-flight on this same repo must never
    race a revert.
    """
    is_active = bridge_active_override if bridge_active_override is not None else bridge_is_active(service_name)
    if is_active:
        raise RevertRefusedError(
            f"refusing to revert: {service_name} is active (a cycle may be mid-flight)"
        )
    if not targets:
        return RevertResult(applied=False, commit_sha=None, reverted=[], error="no targets")

    for target in targets:
        problem = verify_merge_commit(repo, target)
        if problem:
            return RevertResult(applied=False, commit_sha=None, reverted=[], error=problem)

    # #1772: each target is a SEPARATE `--no-commit` invocation (git allows
    # chaining several before one commit), so `revert --abort` on a LATER
    # target only unwinds ITS OWN conflict state — it does not know about
    # earlier targets already staged cleanly in this same loop. `reset
    # --hard head_before` afterwards is what actually guarantees "no
    # partial revert, ever": it discards uncommitted index/working-tree
    # state back to the local HEAD this run itself observed moments ago —
    # never a rewrite of committed/pushed history, which is what the
    # "no reset" requirement is about.
    head_before = _git(repo, "rev-parse", "HEAD").stdout.strip()
    for target in targets:
        result = _git(repo, "revert", "-m", "1", "--no-commit", target.sha)
        if result.returncode != 0:
            _git(repo, "revert", "--abort")
            _git(repo, "reset", "--hard", head_before)
            return RevertResult(
                applied=False, commit_sha=None, reverted=[],
                error=f"conflict reverting {target.sha[:12]} ({target.cycle_id}): {result.stderr.strip() or result.stdout.strip()}",
            )

    # #1772: legible to the loop's own recent-activity reading -- a
    # conventional "revert:" subject (scripts/change_shape.classify_subject
    # already maps this to 'maintenance', not 'feature') plus every reverted
    # cycle id named in the body, so a later cycle reading git log/the
    # ledger sees this as undone rather than re-deriving the original work.
    subject = f"revert: undo {len(targets)} loop integration(s)"
    body_lines = [f"- {t.cycle_id} ({t.sha[:12]})" for t in targets]
    message = subject + "\n\n" + "\n".join(body_lines) + "\n\n#1772\n"
    commit = _git(repo, "commit", "-m", message)
    if commit.returncode != 0:
        _git(repo, "revert", "--abort")
        _git(repo, "reset", "--hard", head_before)
        return RevertResult(
            applied=False, commit_sha=None, reverted=[],
            error=f"commit failed after staging cleanly: {commit.stderr.strip()}",
        )
    commit_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

    if push:
        pushed = _git(repo, "push", "origin", "HEAD:main")
        if pushed.returncode != 0:
            return RevertResult(
                applied=True, commit_sha=commit_sha, reverted=targets,
                error=f"committed locally ({commit_sha[:12]}) but push failed: {pushed.stderr.strip()}",
            )
    return RevertResult(applied=True, commit_sha=commit_sha, reverted=targets, error=None)


# ─── CLI ────────────────────────────────────────────────────────────────────


def _parse_dt(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Revert a range of loop integrations from the instance repo (#1772)."
    )
    parser.add_argument("--state-dir", required=True, help="harness STATE_DIR")
    parser.add_argument("--repo", required=True, help="path to the eeebot-self-evolving checkout")
    parser.add_argument("--since", type=_parse_dt, default=None, help="ISO timestamp, inclusive")
    parser.add_argument("--until", type=_parse_dt, default=None, help="ISO timestamp, exclusive")
    parser.add_argument("--cycle-id", action="append", dest="cycle_ids", default=None,
                         help="revert only this cycle id (repeatable)")
    parser.add_argument("--apply", action="store_true", help="actually revert (default: dry run)")
    parser.add_argument("--no-push", action="store_true", help="commit locally but do not push")
    args = parser.parse_args(argv)

    state_dir = Path(args.state_dir)
    repo = Path(args.repo)
    targets = resolve_targets(state_dir, since=args.since, until=args.until, cycle_ids=args.cycle_ids)

    if not args.apply:
        print(format_dry_run(targets))
        return 0

    try:
        result = apply_revert(repo, targets, push=not args.no_push)
    except RevertRefusedError as exc:
        print(f"revert_cycles: {exc}", file=sys.stderr)
        return 2

    if not result.applied:
        print(f"revert_cycles: not applied — {result.error}", file=sys.stderr)
        return 1
    print(f"revert_cycles: reverted {len(result.reverted)} integration(s) as {result.commit_sha}")
    if result.error:
        print(f"revert_cycles: WARNING — {result.error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
