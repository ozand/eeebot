#!/usr/bin/env python3
"""
loop_explorer_cli.py — CLI for the #781 loop-explorer visualization.

Thin argparse wrapper over ``nanobot.runtime.loop_explorer``: build the
deterministic explorer model from a state dir and render it as the terminal
ANSI strip (``--ansi``, default) or the single self-contained HTML page
(``--html PATH``). ``--test`` runs a self-check against a synthetic fixture
state dir (the ``loop_metrics_report.py`` pattern) and exits.

Read-only over the state dir except for ``--html`` (writes exactly the one
requested file). The state dir resolves like the other runtime report
tools: ``--state-dir`` flag, else the ``STATE_DIR`` env var, else the eeepc
default ``/var/lib/eeepc-agent/self-evolving-agent/state``.

Usage:
    python3 scripts/loop_explorer_cli.py [--state-dir PATH] [--ansi]
    python3 scripts/loop_explorer_cli.py [--state-dir PATH] --html out.html
    python3 scripts/loop_explorer_cli.py --test
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Allow running straight from a repo checkout (scripts/ next to nanobot/).
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from nanobot.runtime import loop_explorer  # noqa: E402

# Same default as loop_metrics_report.py / nanobot.runtime.bridge.STATE_DIR.
_DEFAULT_STATE_DIR = "/var/lib/eeepc-agent/self-evolving-agent/state"


def _default_state_dir() -> Path:
    env_dir = os.environ.get("STATE_DIR", "").strip()
    if env_dir:
        return Path(env_dir)
    return Path(_DEFAULT_STATE_DIR)


def _self_test() -> None:
    """Build a temp fixture state dir and assert model/HTML/ANSI basics."""
    import shutil
    import tempfile

    tmp = tempfile.mkdtemp()
    try:
        state_dir = Path(tmp)
        ledger_dir = state_dir / "ledger"
        ledger_dir.mkdir(parents=True)
        now = datetime.now(timezone.utc)

        def ts(minutes_ago: int) -> str:
            return (now - timedelta(minutes=minutes_ago)).isoformat().replace("+00:00", "Z")

        rows = [
            {"phase": "idle", "reason": "no_demand", "ts": ts(60)},
            {"phase": "proposer_skip", "reason": "nothing valuable", "ts": ts(50)},
            {"phase": "proposer_reject", "reason": "self_dedup", "task_title": "dup",
             "demand_id": "priority-aaa", "matched_against": "done: dup", "ts": ts(40)},
            {"phase": "proposed", "cycle_id": "c1", "task_title": "fix the thing",
             "demand_id": "priority-aaa", "ts": ts(30)},
            {"phase": "dedup", "cycle_id": "c1", "decision": "proceeded",
             "matched_against": None, "ts": ts(29)},
            {"phase": "outcome", "cycle_id": "c1", "outcome": "success", "reason": None,
             "files_changed": ["scripts/a.py"], "ts": ts(28)},
        ]
        (ledger_dir / "cycles.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
        )
        demand_dir = state_dir / "demand"
        demand_dir.mkdir(parents=True)
        (demand_dir / "completed.json").write_text(
            json.dumps(
                {
                    "schema_version": "demand-completed-v1",
                    "entries": {
                        "priority-aaa": {
                            "cycle_id": "c1",
                            "ts": ts(28),
                            "files_changed": ["scripts/a.py"],
                            "confirmed": True,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        scorecard_dir = state_dir / "scorecard"
        scorecard_dir.mkdir(parents=True)
        with open(scorecard_dir / "history.jsonl", "w", encoding="utf-8") as fh:
            for minutes, integrations in ((120, 1), (60, 2), (5, 3)):
                fh.write(
                    json.dumps(
                        {
                            "computed_at_utc": ts(minutes),
                            "loop": {"integrations": integrations, "repeat_failure_rate": 0.1},
                            "cost": {"tokens_per_integration": 1000 * integrations},
                            "heldout": {"heldout_gap": 0.0},
                        }
                    )
                    + "\n"
                )

        model = loop_explorer.build_model(state_dir)
        assert model["window"]["n_events"] == 4, model["window"]
        classes = [e["class"] for e in model["events"]]
        assert classes == ["idle", "noop", "reject", "confirmed"], classes
        assert model["chains"] and model["chains"][0]["demand_id"] == "priority-aaa"
        assert model["chains"][0]["confirmed"] is True
        assert len(model["scorecard_series"]) == 3

        page = loop_explorer.render_html(model)
        assert "fix the thing" in page
        assert "svg" in page
        assert "http" not in page  # fully self-contained, offline-renderable

        ansi = loop_explorer.render_ansi(model)
        assert "legend:" in ansi
        assert "fix the thing" in ansi

        out = loop_explorer.update_explorer(state_dir)
        assert out is not None and out.is_file(), out
        assert loop_explorer.update_explorer(state_dir) is None  # watermark no-op

        empty = loop_explorer.build_model(Path(tmp) / "does-not-exist")
        assert empty["events"] == []
        assert "no loop events" in loop_explorer.render_ansi(empty)

        print("PASS: loop_explorer_cli self-tests passed.")
    finally:
        shutil.rmtree(tmp)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--state-dir", type=Path, default=None,
        help="state dir containing ledger/ (default: STATE_DIR env or eeepc default)",
    )
    parser.add_argument("--ansi", action="store_true", help="print the ANSI strip to stdout (default)")
    parser.add_argument("--html", type=Path, default=None, metavar="PATH", help="write the self-contained HTML page to PATH")
    parser.add_argument("--test", action="store_true", help="run self-tests against a temp fixture state dir and exit")
    args = parser.parse_args(argv)

    if args.test:
        _self_test()
        return 0

    state_dir = args.state_dir or _default_state_dir()
    model = loop_explorer.build_model(state_dir)

    if args.html is not None:
        args.html.parent.mkdir(parents=True, exist_ok=True)
        args.html.write_text(loop_explorer.render_html(model), encoding="utf-8")
        print(f"wrote {args.html}")
        if not args.ansi:
            return 0

    print(loop_explorer.render_ansi(model))
    return 0


if __name__ == "__main__":
    sys.exit(main())
