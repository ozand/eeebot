"""Held-out behavioral checkers — the private half of the evaluation split (#780).

Each checker targets ONE instance artifact by its PUBLIC contract (the
goal_text priority that created it), exercised on fixtures the checker
generates itself inside an isolated tmpdir at runtime. Nothing here ships
into, reads from, or writes to the instance workspace — the instance cannot
see these checks, so it cannot optimize against them (SpecBench's
visible-vs-held-out gap: up to 100pp on complex tasks; a solution tuned to
its own visible tests is not evidence of correctness).

Checker discipline:

- **Lenient on interface details** (exact flag names, output formatting may
  drift) but **strict on the behavioral core**: the dashboard must not crash
  on empty state; system-map regeneration must produce a file naming fixture
  scripts. Loosely-contracted scripts get a lenient smoke check only.
- Checkers receive a :class:`CheckContext` and return ``(status, evidence)``
  with ``status`` ∈ ``pass|fail|skip``. The evidence string is the ONLY
  thing that ever reaches the loop (as defect-demand evidence) — it states
  WHAT is broken, never how the check works internally.
- Scripts run via ``sys.executable`` with ``cwd=tmp_dir`` and an env
  stripped to a minimal PATH + a tmpdir-only PYTHONPATH/HOME/TMPDIR — no
  state_dir, no secrets, no network assumptions. A hung script hits the
  subprocess timeout, which the runner converts to ``skip``.

Registry: :data:`CHECKERS` maps instance-repo-relative artifact path →
checker callable. Adding coverage = adding one entry here.
"""
from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

PASS = "pass"
FAIL = "fail"
SKIP = "skip"

_STDERR_TAIL = 160  # bounded evidence excerpt


@dataclass
class CheckContext:
    """Everything a checker gets: an isolated fixture root (also the
    subprocess cwd), the tmpdir copy of the script under test, and the
    subprocess timeout in seconds."""

    tmp_dir: Path
    script: Path
    timeout: float = 30.0


def _sandbox_env(ctx: CheckContext) -> dict[str, str]:
    """Minimal subprocess env: PATH for the interpreter's helpers, and
    HOME/TMPDIR/PYTHONPATH pinned to the fixture tmpdir ONLY — no state
    dir, no secrets pass-through, no network configuration."""
    return {
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": str(ctx.tmp_dir),
        "HOME": str(ctx.tmp_dir),
        "TMPDIR": str(ctx.tmp_dir),
        "LANG": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def _run(ctx: CheckContext, args: tuple[str, ...] = ()) -> subprocess.CompletedProcess:
    """Run the script under test in the sandbox. ``TimeoutExpired``
    propagates on purpose — the runner turns it into a ``skip``."""
    return subprocess.run(
        [sys.executable, str(ctx.script), *args],
        cwd=str(ctx.tmp_dir),
        env=_sandbox_env(ctx),
        capture_output=True,
        text=True,
        timeout=ctx.timeout,
    )


def _stderr_tail(proc: subprocess.CompletedProcess) -> str:
    text = (proc.stderr or proc.stdout or "").strip()
    return text[-_STDERR_TAIL:].replace("\n", " ")


def _write_fixture_ledger(ctx: CheckContext) -> Path:
    ledger_dir = ctx.tmp_dir / "state" / "ledger"
    ledger_dir.mkdir(parents=True, exist_ok=True)
    rows = [
        {"phase": "outcome", "cycle_id": "hx1", "outcome": "success", "ts": "2026-07-16T10:00:00Z"},
        {"phase": "outcome", "cycle_id": "hx2", "outcome": "failed", "reason": "gate", "ts": "2026-07-16T11:00:00Z"},
        {"phase": "idle", "reason": "no_demand", "ts": "2026-07-16T12:00:00Z"},
    ]
    path = ledger_dir / "cycles.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


# ─── scripts/eeebot_dashboard.py ────────────────────────────────────────────


def check_eeebot_dashboard(ctx: CheckContext) -> tuple[str, str]:
    """Contract (Priorities 11/14): renders loop-health/demand sections from
    ``state/ledger/cycles.jsonl``. STRICT core: degrades gracefully (exit 0)
    when the ledger is missing/empty; renders SOMETHING on a fixture ledger."""
    proc = _run(ctx)
    if proc.returncode != 0:
        return FAIL, (
            f"crashed on empty state (no ledger): exit {proc.returncode}: {_stderr_tail(proc)}"
        )
    _write_fixture_ledger(ctx)
    proc = _run(ctx)
    if proc.returncode != 0:
        return FAIL, f"crashed on fixture ledger: exit {proc.returncode}: {_stderr_tail(proc)}"
    if not (proc.stdout or "").strip():
        return FAIL, "produced no output on a populated fixture ledger"
    return PASS, "degrades on empty state; renders output on fixture ledger"


# ─── scripts/generate_system_map.py ─────────────────────────────────────────


def check_generate_system_map(ctx: CheckContext) -> tuple[str, str]:
    """Contract (Priority 13): regenerates ``docs/SYSTEM_MAP.md`` — one line
    per script in ``scripts/``. STRICT core: the output file exists and
    mentions the fixture scripts by name."""
    scripts_dir = ctx.tmp_dir / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    (scripts_dir / "fixture_alpha.py").write_text(
        '"""Fixture alpha — a held-out probe script."""\n', encoding="utf-8"
    )
    (scripts_dir / "fixture_beta.py").write_text(
        '"""Fixture beta — a second held-out probe script."""\n', encoding="utf-8"
    )
    (ctx.tmp_dir / "docs").mkdir(parents=True, exist_ok=True)
    proc = _run(ctx)
    if proc.returncode != 0:
        return FAIL, f"exited {proc.returncode}: {_stderr_tail(proc)}"
    candidates = [ctx.tmp_dir / "docs" / "SYSTEM_MAP.md"] + list(ctx.tmp_dir.rglob("SYSTEM_MAP.md"))
    system_map = next((p for p in candidates if p.is_file()), None)
    if system_map is None:
        return FAIL, "did not produce a SYSTEM_MAP.md file"
    content = system_map.read_text(encoding="utf-8", errors="replace")
    if "fixture_alpha" not in content:
        return FAIL, "SYSTEM_MAP.md does not mention the fixture scripts in scripts/"
    return PASS, "regenerated SYSTEM_MAP.md naming the fixture scripts"


# ─── lenient smoke (loosely-contracted scripts) ─────────────────────────────


def check_smoke(ctx: CheckContext) -> tuple[str, str]:
    """Lenient smoke check for scripts whose public contract is loose
    (``prune_failed_backlog.py``, ``loop_health_report.py``): given a small
    fixture state, the script runs and exits 0 with parseable output. When
    the bare invocation needs unknown args, a healthy ``--help`` still
    passes (lenient on interface, strict only on 'it runs at all')."""
    _write_fixture_ledger(ctx)
    backlog_dir = ctx.tmp_dir / "state" / "hypotheses"
    backlog_dir.mkdir(parents=True, exist_ok=True)
    (backlog_dir / "backlog.json").write_text('{"entries": []}\n', encoding="utf-8")

    proc = _run(ctx)
    if proc.returncode == 0:
        stdout = (proc.stdout or "").strip()
        if stdout.startswith(("{", "[")):
            try:
                json.loads(stdout)
            except Exception:
                return FAIL, "output looks like JSON but does not parse"
        return PASS, "runs to exit 0 on fixture state with parseable output"
    help_proc = _run(ctx, ("--help",))
    if help_proc.returncode == 0:
        return PASS, (
            f"bare run exited {proc.returncode} (interface differs) but --help is healthy"
        )
    return FAIL, f"exited {proc.returncode} on fixture state: {_stderr_tail(proc)}"


# ─── registry ───────────────────────────────────────────────────────────────

CHECKERS: dict[str, Callable[[CheckContext], tuple[str, str]]] = {
    "scripts/eeebot_dashboard.py": check_eeebot_dashboard,
    "scripts/generate_system_map.py": check_generate_system_map,
    "scripts/prune_failed_backlog.py": check_smoke,
    "scripts/loop_health_report.py": check_smoke,
}
