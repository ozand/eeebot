"""Reproducible disposable #2181 comparison: current files vs SQLite WAL."""
from __future__ import annotations

import argparse
import json
import math
import os
import platform
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from nanobot.runtime import cycle_ledger, open_increment  # noqa: E402

CYCLE = "eval-cycle"
RUNNING = {
    "cycle_id": CYCLE, "branch": "eval-only", "attempt": 1,
    "started_at": "2026-01-01T00:00:00Z", "resumed_by": None,
    "task_id": None, "plan_text": "synthetic benchmark fixture",
}
EVENT = {
    "phase": "outcome", "cycle_id": CYCLE, "outcome": "failed",
    "reason": "evaluation_fixture", "files_changed": [], "branch": "eval-only",
}


def compact_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def seed_file(state_dir: Path, cycle_id: str) -> None:
    state = open_increment.OpenIncrementState(running={**RUNNING, "cycle_id": cycle_id})
    if not open_increment._save_state(state_dir, state):
        raise RuntimeError("failed to persist file baseline state")


def create_sqlite(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE attempts(singleton INTEGER PRIMARY KEY CHECK(singleton=1), cycle_id TEXT, running_json TEXT)"
    )
    connection.execute("CREATE TABLE outcomes(seq INTEGER PRIMARY KEY, cycle_id TEXT, payload TEXT)")
    connection.execute("CREATE INDEX outcomes_cycle_id ON outcomes(cycle_id)")
    connection.commit()


def set_sqlite_running(connection: sqlite3.Connection, cycle_id: str) -> None:
    connection.execute(
        "INSERT INTO attempts(singleton,cycle_id,running_json) VALUES(1,?,?) "
        "ON CONFLICT(singleton) DO UPDATE SET cycle_id=excluded.cycle_id,running_json=excluded.running_json",
        (cycle_id, compact_json({**RUNNING, "cycle_id": cycle_id})),
    )
    connection.commit()


def transition_file(state_dir: Path, cycle_id: str) -> None:
    cycle_ledger.record_cycle_outcome(
        state_dir, cycle_id, "failed", "evaluation_fixture", [], "eval-only",
    )


def transition_sqlite(connection: sqlite3.Connection, cycle_id: str) -> None:
    connection.execute("BEGIN IMMEDIATE")
    connection.execute(
        "INSERT INTO outcomes(cycle_id,payload) VALUES(?,?)",
        (cycle_id, compact_json({**EVENT, "cycle_id": cycle_id})),
    )
    connection.execute(
        "UPDATE attempts SET running_json=NULL WHERE singleton=1 AND cycle_id=?", (cycle_id,),
    )
    connection.commit()


def child(mode: str) -> int:
    root = Path(os.environ["EVAL_DIR"])
    state_dir = root / "state"
    if mode.startswith("file-"):
        if mode == "file-before":
            seed_file(state_dir, "crash-probe")
            os._exit(71)
        if mode == "file-between":
            cycle_ledger.append_event(state_dir, {**EVENT, "cycle_id": "crash-probe"})
            os._exit(73)
        transition_file(state_dir, "crash-probe")
        os._exit(72)
    if mode.startswith("ledger-"):
        ledger_state_dir = root / "state"
        if mode == "ledger-before":
            os._exit(91)
        if mode == "ledger-mid":
            ledger_path = ledger_state_dir / "ledger" / "cycles.jsonl"
            ledger_path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps({**EVENT, "cycle_id": "crash-probe"}).encode("utf-8")
            with ledger_path.open("ab") as handle:
                handle.write(payload[: len(payload) // 2])
                handle.flush()
                os._exit(93)
        cycle_ledger.append_event(ledger_state_dir, {**EVENT, "cycle_id": "crash-probe"})
        os._exit(92)

    connection = sqlite3.connect(root / "state.db")
    if mode == "sqlite-before":
        os._exit(81)
    if mode == "sqlite-mid":
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO outcomes(cycle_id,payload) VALUES(?,?)",
            ("crash-probe", compact_json({**EVENT, "cycle_id": "crash-probe"})),
        )
        connection.execute("UPDATE attempts SET running_json=NULL WHERE singleton=1")
        os._exit(82)
    transition_sqlite(connection, "crash-probe")
    os._exit(83)


EXPECTED_CHILD_EXIT = {
    "file-before": 71,
    "file-between": 73,
    "file-after": 72,
    "ledger-before": 91,
    "ledger-mid": 93,
    "ledger-after": 92,
    "sqlite-before": 81,
    "sqlite-mid": 82,
    "sqlite-after": 83,
}


def verify_child_exit(mode: str, actual_code: int, stderr: str = "") -> None:
    expected_code = EXPECTED_CHILD_EXIT.get(mode)
    diagnostic = stderr[-800:].strip() or "<empty>"
    if expected_code is None or actual_code != expected_code:
        raise RuntimeError(
            f"failure injection {mode!r}: expected exit {expected_code}, "
            f"got {actual_code}; stderr (last 800 chars): {diagnostic}",
        )


def run_child(mode: str, root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), mode], cwd=ROOT,
        env={**os.environ, "EVAL_DIR": str(root)},
        capture_output=True, text=True, timeout=10, check=False,
    )


def file_state(root: Path) -> dict[str, object]:
    state_dir = root / "state"
    outcomes = cycle_ledger.read_events_across_rotation(
        state_dir, phases=frozenset({"outcome"}), max_archives=8,
    )
    ledger_path = state_dir / "ledger" / "cycles.jsonl"
    raw_lines = ledger_path.read_bytes().splitlines() if ledger_path.exists() else []
    prior = open_increment.load_state(state_dir).running
    signal = "recorded_terminal_outcome" if any(
        row.get("cycle_id") == "crash-probe" for row in outcomes
    ) else "no_terminal_outcome"
    stale = open_increment.check_running_for_kill(state_dir, "next-cycle")
    after = open_increment.load_state(state_dir).running
    return {
        "outcome_count": sum(row.get("cycle_id") == "crash-probe" for row in outcomes),
        "valid_json_lines": sum(_is_json_object(line) for line in raw_lines),
        "raw_line_count": len(raw_lines),
        "running_before_recovery": prior,
        "recovery": "terminal_outcome_reconciled" if stale is None and after is None else "unresolved_registration_held",
        "recovery_signal": signal,
        "running_after_recovery": after,
    }


def ledger_state(root: Path) -> dict[str, object]:
    ledger_path = root / "state" / "ledger" / "cycles.jsonl"
    raw_lines = ledger_path.read_bytes().splitlines() if ledger_path.exists() else []
    return {
        "outcome_count": sum(
            isinstance(row, dict) and row.get("cycle_id") == "crash-probe"
            for row in cycle_ledger.read_events(root / "state")
        ),
        "valid_json_lines": sum(_is_json_object(line) for line in raw_lines),
        "raw_line_count": len(raw_lines),
        "registration_recovery": "not_applicable_no_registration_in_ledger_only_probe",
    }


def _is_json_object(line: bytes) -> bool:
    try:
        return isinstance(json.loads(line), dict)
    except Exception:
        return False


def sqlite_state(root: Path) -> dict[str, object]:
    connection = sqlite3.connect(root / "state.db")
    count = connection.execute(
        "SELECT count(*) FROM outcomes WHERE cycle_id='crash-probe'",
    ).fetchone()[0]
    running = connection.execute(
        "SELECT running_json FROM attempts WHERE singleton=1 AND cycle_id='crash-probe'",
    ).fetchone()
    before = json.loads(running[0]) if running and running[0] else None
    if count:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE attempts SET running_json=NULL WHERE singleton=1 AND cycle_id='crash-probe'",
        )
        connection.commit()
        recovery = "terminal_outcome_reconciled"
    else:
        recovery = "unresolved_registration_held"
    after = connection.execute(
        "SELECT running_json FROM attempts WHERE singleton=1 AND cycle_id='crash-probe'",
    ).fetchone()
    connection.close()
    return {
        "outcome_count": count, "running_before_recovery": before,
        "recovery": recovery,
        "running_after_recovery": json.loads(after[0]) if after and after[0] else None,
    }


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(fraction * len(ordered)) - 1)]


def measure_file(root: Path, operations: int) -> dict[str, object]:
    state_dir = root / "state"
    latencies: list[float] = []
    ledger_dir = state_dir / "ledger"
    ledger_dir.mkdir(parents=True)
    with (ledger_dir / "cycles.jsonl").open("w", encoding="utf-8") as history:
        history.writelines(
            json.dumps({**EVENT, "cycle_id": f"history-{index}"}) + "\n"
            for index in range(1000)
        )
    for index in range(operations):
        cycle_id = f"{CYCLE}-{index}"
        seed_file(state_dir, cycle_id)
        start = time.perf_counter_ns()
        transition_file(state_dir, cycle_id)
        latencies.append((time.perf_counter_ns() - start) / 1e6)
    return {
        "p50_ms": round(percentile(latencies, .50), 4),
        "p95_ms": round(percentile(latencies, .95), 4),
        "bytes": sum(p.stat().st_size for p in root.rglob("*") if p.is_file()),
    }


def measure_sqlite(root: Path, operations: int) -> dict[str, object]:
    database = root / "state.db"
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    create_sqlite(connection)
    connection.executemany(
        "INSERT INTO outcomes(cycle_id,payload) VALUES(?,?)",
        [(f"history-{index}", compact_json({**EVENT, "cycle_id": f"history-{index}"}))
         for index in range(1000)],
    )
    connection.commit()
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    latencies: list[float] = []
    for index in range(operations):
        cycle_id = f"{CYCLE}-{index}"
        set_sqlite_running(connection, cycle_id)
        start = time.perf_counter_ns()
        transition_sqlite(connection, cycle_id)
        latencies.append((time.perf_counter_ns() - start) / 1e6)
    connection.close()
    return {
        "p50_ms": round(percentile(latencies, .50), 4),
        "p95_ms": round(percentile(latencies, .95), 4),
        "bytes": sum(p.stat().st_size for p in root.rglob("*") if p.is_file()),
    }


def peak_working_set() -> int:
    import psutil
    return int(psutil.Process().memory_info().peak_wset)


def benchmark_worker(backend: str, root: Path, operations: int) -> dict[str, object]:
    measured = measure_file(root, operations) if backend == "file" else measure_sqlite(root, operations)
    return {**measured, "peak_working_set_bytes": peak_working_set()}


def run_benchmark_worker(backend: str, root: Path, operations: int) -> dict[str, object]:
    completed = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--worker", backend,
         str(root), str(operations)],
        cwd=ROOT, capture_output=True, text=True, check=True,
    )
    return json.loads(completed.stdout)


def crash_matrix(root: Path) -> dict[str, object]:
    results: dict[str, object] = {}
    for backend, modes, reader in (
        ("file", ("file-before", "file-between", "file-after"), file_state),
        ("ledger", ("ledger-before", "ledger-mid", "ledger-after"), ledger_state),
        ("sqlite", ("sqlite-before", "sqlite-mid", "sqlite-after"), sqlite_state),
    ):
        for mode in modes:
            case = root / mode
            case.mkdir()
            if backend == "file":
                seed_file(case / "state", "crash-probe")
            elif backend == "ledger":
                (case / "state").mkdir()
                if mode != "ledger-before":
                    (case / "state" / "ledger").mkdir()
            else:
                connection = sqlite3.connect(case / "state.db")
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=FULL")
                create_sqlite(connection)
                set_sqlite_running(connection, "crash-probe")
                connection.close()
            child_result = run_child(mode, case)
            verify_child_exit(mode, child_result.returncode, child_result.stderr)
            results[mode] = {
                "exit_code": child_result.returncode,
                "reopened": reader(case),
            }
    return results


def evaluate(runs: int, operations: int) -> dict[str, object]:
    file_runs: list[dict[str, object]] = []
    sqlite_runs: list[dict[str, object]] = []
    peaks = {"file": 0, "sqlite": 0}
    with tempfile.TemporaryDirectory(prefix="2181-crash-") as temp:
        failures = crash_matrix(Path(temp))
    schedule = [("file", file_runs), ("sqlite", sqlite_runs)]
    for run in range(runs):
        ordered = schedule if run % 2 == 0 else list(reversed(schedule))
        for backend, run_results in ordered:
            with tempfile.TemporaryDirectory(prefix=f"2181-{backend}-") as temp:
                result = run_benchmark_worker(backend, Path(temp), operations)
                run_results.append(result)
                peaks[backend] = max(peaks[backend], int(result.pop("peak_working_set_bytes")))
    for backend, run_results in schedule:
        values = run_results
        medians = {
            metric: round(percentile([float(item[metric]) for item in values], .50), 4)
            for metric in ("p50_ms", "p95_ms")
        }
        medians["max_p95_ms"] = max(float(item["p95_ms"]) for item in values)
        if backend == "file":
            file_summary = medians
        else:
            sqlite_summary = medians
    return {
        "environment": {
            "os": platform.platform(), "machine": platform.machine(),
            "baseline_revision": "2848397fb388d690d27b7639ae634de059b6bc37",
            "python": sys.version.split()[0], "pointer_bits": __import__("struct").calcsize("P") * 8,
            "sqlite": sqlite3.sqlite_version, "temp_volume": "local NTFS fixed disk; path withheld",
            "runs": runs, "operations_per_run": operations,
            "benchmark_statistic": "median and maximum across runs of per-operation p50/p95; file and SQLite execution order alternated by run",
            "sqlite_journal_mode": "WAL", "sqlite_synchronous": "FULL",
            "memory_probe": "child-process Windows peak working set via psutil peak_wset; DLL mappings included",
            "workload": "terminalization of one currently registered attempt; 1,000 historical outcome rows preloaded in both backends; transitions timed only, recovery lookups functional-only",
        },
        "crash_reopen": failures,
        "file_runs": file_runs,
        "sqlite_runs": sqlite_runs,
        "peak_working_set_bytes": peaks,
        "peak_working_set_delta_bytes": peaks["sqlite"] - peaks["file"],
        "run_summary": {"file": file_summary, "sqlite": sqlite_summary},
    }


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1].startswith(("file-", "ledger-", "sqlite-")):
        return child(sys.argv[1])
    if len(sys.argv) > 1 and sys.argv[1] == "--worker":
        backend, directory, count = sys.argv[2], Path(sys.argv[3]), int(sys.argv[4])
        result = benchmark_worker(backend, directory, count)
        print(json.dumps(result))
        return 0
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--operations", type=int, default=1000)
    args = parser.parse_args()
    output = args.output.resolve()
    try:
        output.relative_to(ROOT.resolve())
    except ValueError:
        pass
    else:
        parser.error("--output must be outside the repository")
    if output.exists():
        parser.error("--output must name a new path; refusing to overwrite")
    result = evaluate(args.runs, args.operations)
    serialized = json.dumps(result, indent=2) + "\n"
    print(serialized, end="")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(serialized, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
