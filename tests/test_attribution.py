from __future__ import annotations

import ast
from pathlib import Path

from scripts.cycle_cost_probe import CycleCostSampler, read_process_snapshot


def test_own_process_attribution_uses_proc_self_never_hostwide_aggregates(tmp_path: Path) -> None:
    """Acceptance criterion 2: the test proves that cycle cost measures only

    its own process (and children), never host-wide aggregates from /proc/stat,
    /proc/meminfo, or /proc/loadavg.
    """
    # 1. Source / AST check of cycle_cost_probe.py to ensure proc_root defaults to /proc/self
    probe_py = Path(__file__).resolve().parent.parent / "scripts" / "cycle_cost_probe.py"
    source = probe_py.read_text(encoding="utf-8")
    tree = ast.parse(source)

    forbidden_hostwide_paths = {"/proc/stat", "/proc/meminfo", "/proc/loadavg"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            for forbidden in forbidden_hostwide_paths:
                assert forbidden not in node.value, f"Forbidden host-wide path {forbidden} found in probe source"

    # 2. Functional check: read_process_snapshot reads strictly from provided proc_root (defaults to /proc/self)
    proc_mock = tmp_path / "proc_self"
    proc_mock.mkdir()
    (proc_mock / "stat").write_text("100 (self) S 1 2 3 4 5 6 7 8 9 10 25 35 14\n", encoding="utf-8")
    (proc_mock / "status").write_text("Name:\tself\nVmHWM:\t51200 kB\n", encoding="utf-8")

    snap = read_process_snapshot(proc_root=proc_mock, clock_ticks=100)
    # ticks = 25 + 35 = 60 ticks
    assert snap["cpu_ticks"] == 60
    assert snap["clock_ticks"] == 100
    assert snap["peak_rss_bytes"] == 51200 * 1024

    # 3. Verify CycleCostSampler defaults to own process /proc/self
    sampler = CycleCostSampler()
    assert sampler.proc_root == Path("/proc/self")


def test_fixture_hostwide_load_differs_from_own_process_load(tmp_path: Path) -> None:
    """Issue #1605 Box 2: a test drives a fixture where host-wide load and

    own-process load differ and asserts the two are not conflated.

    Write a fixture host-wide /proc/stat with large cpu numbers next to
    the /proc/self stat with small ticks, run the sampler against
    proc_root=<fixture self>, assert the reported ticks equal the self value
    and not the host value.
    """
    proc_dir = tmp_path / "proc"
    proc_dir.mkdir()

    # 1. Host-wide /proc/stat with very large aggregate CPU numbers (millions of ticks)
    host_stat = proc_dir / "stat"
    host_stat.write_text(
        "cpu  99999999 88888888 77777777 66666666 55555 44444 33333 22222 0 0\n"
        "cpu0 50000000 44444444 38888888 33333333 27777 22222 16666 11111 0 0\n"
        "cpu1 49999999 44444444 38888889 33333333 27778 22222 16667 11111 0 0\n",
        encoding="utf-8",
    )

    # 2. Own-process /proc/self (small ticks: 10 utime + 15 stime = 25 ticks)
    proc_self = proc_dir / "self"
    proc_self.mkdir()
    (proc_self / "stat").write_text("42 (worker) R 1 42 42 0 0 0 0 0 0 0 10 15 0 0\n", encoding="utf-8")
    (proc_self / "status").write_text("Name:\tworker\nVmHWM:\t32768 kB\n", encoding="utf-8")

    # Run CycleCostSampler using proc_root=proc_self
    fake_thermal = {"zone": "/sys/class/thermal/thermal_zone0", "type": "acpitz", "temperature_millicelsius": 55000}
    fake_throttle = {"count": 0, "total_time_ms": 0, "max_time_ms": 0, "paths": []}

    sampler = CycleCostSampler(
        proc_root=proc_self,
        thermal_reader=lambda _path: fake_thermal,
        throttle_reader=lambda: fake_throttle,
    )
    # Start sampler with initial snapshot (25 ticks)
    sampler.start(cycle_id="cycle-attribution-test")

    # During cycle, process accumulates 5 ticks (12 utime + 18 stime = 30 ticks)
    (proc_self / "stat").write_text("42 (worker) R 1 42 42 0 0 0 0 0 0 0 12 18 0 0\n", encoding="utf-8")

    # Finish sampler (sees 30 ticks, delta = 5 ticks = 0.05 seconds at 100 Hz)
    summary = sampler.finish()

    assert summary["cpu_seconds"]["state"] == "present"
    # Exact delta of own process: (30 - 25) / 100 = 0.05s
    # NOT millions of ticks from host-wide /proc/stat
    assert summary["cpu_seconds"]["value"] == 0.05
    assert summary["peak_rss_bytes"]["value"] == 32768 * 1024
