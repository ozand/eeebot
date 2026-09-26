#!/usr/bin/env python3
"""Read-only host exporter for the 2026-09-21 Rule-C base fixture."""

from __future__ import annotations

import gzip
import json
import pathlib
import subprocess
import sys

from nanobot.runtime.service_paths import is_service_only

ROOT = pathlib.Path("/var/lib/eeepc-agent/self-evolving-agent")
WINDOW_START = "2026-09-24T23:37:48Z"
WINDOW_START_DT = __import__("datetime").datetime.fromisoformat(WINDOW_START.replace("Z", "+00:00"))


def before_window(first_start: str) -> bool:
    from datetime import datetime

    return datetime.fromisoformat(first_start.replace("Z", "+00:00")) < WINDOW_START_DT


def require_pre_window(first_start: str) -> None:
    if not before_window(first_start):
        raise SystemExit("post-window first start found in base cohort")


def main() -> None:
    state, repo = ROOT / "state", ROOT / "eeebot-self-evolving"
    rows = []
    for path in sorted((state / "ledger").glob("cycles-*.jsonl.gz")):
        with gzip.open(path, "rt") as stream:
            for line in stream:
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
    active = state / "ledger/cycles.jsonl"
    if active.exists():
        for line in active.read_text().splitlines():
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    starts, outcomes = {}, {}
    for row in rows:
        cid, ts = row.get("cycle_id"), row.get("ts")
        if not cid or not ts:
            continue
        if row.get("phase") == "started":
            starts[cid] = min(starts.get(cid, ts), ts)
        if row.get("phase") == "outcome" and (
            cid not in outcomes or ts > outcomes[cid].get("ts", "")
        ):
            outcomes[cid] = row
    base = [
        cid for cid, ts in starts.items() if "2026-09-21T00:00:00" <= ts < "2026-09-22T00:00:00"
    ]
    for cid in base:
        require_pre_window(starts[cid])
    calls = {}
    for line in (state / "llm_calls/2026-09-21.jsonl").read_text().splitlines():
        try:
            row = json.loads(line)
        except Exception:
            continue
        if row.get("component") == "executor" and row.get("cycle_id"):
            cid = row["cycle_id"]
            calls[cid] = min(calls.get(cid, row["ts"]), row["ts"])
    first24 = sorted((cid for cid in base if cid in calls), key=lambda cid: calls[cid])[:24]
    if len(base) != 42 or len(first24) != 24:
        raise SystemExit("base selection count mismatch")
    accepted = {
        cid
        for cid in base
        if outcomes.get(cid, {}).get("outcome") == "success"
        and outcomes[cid].get("verdict") == "accept"
    }
    log = subprocess.run(
        ["git", "-C", str(repo), "log", "origin/main", "--first-parent", "--format=%H%x09%s"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    merges = {}
    prefix = "merge: integrate selfevo/cycle-"
    for line in log.splitlines():
        sha, _, subject = line.partition("\t")
        if subject.startswith(prefix):
            cid = subject[len(prefix) :].strip()
            if cid in accepted and cid not in merges:
                merges[cid] = sha
    files = {}
    for cid, sha in merges.items():
        names = subprocess.run(
            ["git", "-C", str(repo), "diff", "--name-only", f"{sha}^1", f"{sha}^2"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        if any(path.startswith("/") or ".." in pathlib.PurePosixPath(path).parts for path in names):
            raise SystemExit("diff path outside instance repository")
        files[cid] = names
    order = first24 + [cid for cid in sorted(base) if cid not in first24]
    export = [
        {
            "cycle_id": cid,
            "outcome": outcomes.get(cid, {}).get("outcome"),
            "verdict": outcomes.get(cid, {}).get("verdict"),
            "branch_files": files.get(cid, []),
        }
        for cid in order
    ]
    selected24 = set(first24)
    for selected in (set(base), selected24):
        successes = [
            cid
            for cid in selected
            if outcomes.get(cid, {}).get("outcome") == "success"
            and outcomes[cid].get("verdict") == "accept"
        ]
        excluded = sum(is_service_only(files.get(cid)) for cid in successes)
        if len(successes) - excluded != (15 if len(selected) == 42 else 14):
            raise SystemExit("rule-C baseline count mismatch")
    print(json.dumps(export, indent=2))


if __name__ == "__main__":
    if "--check-boundary" in sys.argv:
        try:
            require_pre_window(WINDOW_START)
        except SystemExit:
            print("PASS: synthetic first start at cutoff rejected")
            raise SystemExit(0)
        raise SystemExit("FAIL: cutoff accepted")
    main()
