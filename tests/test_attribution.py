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
