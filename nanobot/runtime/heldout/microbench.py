"""Harness-run per-cycle causal micro-benchmark (#822).

#819's ``benchmark_evidence.verify_benchmark`` corroborates an optimization
claim against the harness's OWN ``scorecard/history.jsonl`` — but those are
7-day WINDOWED AGGREGATES, not a single-cycle before/after measurement (see
that module's HONEST RESIDUAL). In practice this means a real, single-cycle
performance win almost never moves a 7-day aggregate enough to corroborate,
so ``SELFEVO_BENCHMARK_TRUST`` stays off even for genuine wins — the #819
guarantee (a forged artifact cannot self-corroborate) is real, but the gate
it protects rarely fires FOR THE RIGHT REASON either.

This module closes that gap with an actual causal measurement: the harness
itself times a SELF-CONTAINED benchmark script, once against the pre-cycle
base commit and once against the cycle's own candidate commit, in isolated
git worktrees under a stripped subprocess env — never trusting a number the
instance wrote. Concretely:

- :data:`MICROBENCHES` is a small, product-code-owned registry mapping a
  repo-relative module path to the SOURCE of a benchmark script for that
  module's hot path. There is no environment variable or on-disk file that
  can add or override an entry — the instance cannot invent its own
  benchmark and have it trusted, symmetric with #819's
  ``_HARNESS_METRICS`` allowlist.
- :func:`run_measurement` executes one registered spec against one git ref
  of a repo: a temporary ``git worktree add --detach`` checkout, the spec
  script run via ``sys.executable`` with a stripped env (mirrors
  ``heldout/checkers.py``'s ``_sandbox_env`` — no state dir, no secrets, no
  network assumptions), parsing the LAST stdout line as a float
  (milliseconds, lower=better). The worktree is always removed, even on
  failure/timeout. Fails open to ``None`` on any error.
- :func:`measure_cycle` is the bridge's entry point: given a cycle's
  changed files, it finds the first one with a registered spec, measures
  baseline (``base_sha``) then candidate (``head_ref``), and — only if BOTH
  measurements succeed — appends a bounded (200-entry) record to
  ``<state_dir>/heldout/microbench.json``. Fails open to ``None`` (writing
  nothing) on any error, including a single failed measurement.
- :func:`load_microbench_entry` is the read side ``benchmark_evidence``
  uses: returns a well-formed entry for a cycle_id, or ``None`` if the
  entry is absent OR malformed (a malformed entry is treated exactly like
  an absent one — never trusted, never a hard failure either).

Security invariants (do not weaken):

- The instance never supplies the measured number. Both measurements are
  taken by the harness itself, in subprocesses it spawns, against code it
  checked out via git — the instance's own scripts/state cannot influence
  which number gets written beyond choosing which files to change (which is
  its normal job).
- ``state/heldout/microbench.json`` is a #789 fitness sidecar (see
  ``scorecard.FITNESS_SIDECARS``) — hashed at the same spawn-boundary
  checkpoint as every other fitness input, so an instance-authored script
  writing into it mid-cycle is detected exactly like the #789 incident that
  motivated that check for the other sidecars.
- Nothing here reads ``state/benchmarks/`` (the instance-forgeable
  optimization-claim artifact #813/#819 already treat as untrusted prose) —
  this module and ``benchmark_evidence`` are deliberately independent
  measurement paths that happen to share a consumer.

RESIDUAL ACCEPTED RISK (opus-review RED-3 — read before trusting a green
result blindly): each registered spec now asserts CORRECTNESS on its own
synthetic corpus (not just wall time — see the seed spec below), so a
candidate that goes fast by returning empty/wrong results (e.g.
``find_similar`` silently returning ``[]``) fails the spec and produces no
entry, not an inflated ``improvement_pct``. That corpus is still
world-readable product source, shipped with every release: a sufficiently
adversarial instance change could in principle special-case exactly this
synthetic corpus's shape while staying behaviorally correct ONLY on it (and
wrong everywhere else). This module does not claim to close that gap by
itself; it is one layer of a stack: (1) the correctness assertions here
catch the crude "just return nothing/wrong" failure mode; (2)
``heldout/checkers.py``'s INVISIBLE behavioral checks (a fully separate,
never-instance-facing fixture set) are the harness's actual defense against
narrow overfitting; (3) every green runtime-slice result — microbench
entry included — lands only as a promotion candidate under
``state/promotions/`` for OPERATOR REVIEW before ever reaching the live
release (#812) — nothing here auto-integrates on its own.
"""
from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

_MICROBENCH_DIR = "heldout"
_MICROBENCH_FILENAME = "microbench.json"
_MAX_ENTRIES = 200
_SCHEMA = "heldout-microbench-v1"
_ENTRY_SCHEMA = "heldout-microbench-entry-v1"

# #822 MED-2: max relative disagreement allowed between the two baseline
# runs (bracketing the candidate run) before the whole measurement is
# discarded as too noisy to trust — see measure_cycle's drift guard.
_BASELINE_DRIFT_MAX_REL = 0.05


# ─── seed spec: nanobot/runtime/existence_index.py ─────────────────────────

_EXISTENCE_INDEX_SPEC = '''"""Harness microbench spec: nanobot/runtime/existence_index.py (#822).

Self-contained — no imports beyond stdlib + nanobot.runtime.existence_index.
Builds a small synthetic scripts/ corpus fresh in a tempdir, exercises the
module's real hot path (reindex + a handful of find_similar /
find_duplicate_script queries), and prints the best-of-5 wall time in
milliseconds as the LAST stdout line. Never touches any real state dir or
network — every path used is inside a tempdir created by this process.

CORRECTNESS, not just speed (opus-review RED-3): a candidate that goes fast
by silently returning empty/wrong results must not be credited as a win.
After each timed run, this spec asserts the module still gets the RIGHT
answer on two known cases planted in the corpus: (1) a query naming an
indexed fixture by number must return a hit containing that fixture's
filename; (2) a title matching the module's own documented worked example
("monitor RAM and memory usage" vs. a planted scripts/track_memory.py,
sharing exactly the 2 words the matching rule is tuned on) must be caught
by find_duplicate_script. Either check failing calls sys.exit(1) — a
nonzero exit makes run_measurement() return None, so a fast-but-wrong
candidate produces NO entry rather than an inflated improvement_pct.
"""
import shutil
import sys
import tempfile
import time
from pathlib import Path

from nanobot.runtime import existence_index as ei

_CORPUS_SIZE = 30
_QUERY_COUNT = 5
_RUNS = 5


def _build_corpus(scripts_dir):
    scripts_dir.mkdir(parents=True, exist_ok=True)
    for i in range(_CORPUS_SIZE):
        path = scripts_dir / ("fixture_script_%02d.py" % i)
        text = (
            '"""Fixture script number %d exercising benchmark corpus text '
            'for the existence index measurement."""\\n'
            "def run_%d():\\n"
            "    return %d\\n"
        ) % (i, i, i)
        path.write_text(text, encoding="utf-8")
    # Planted near-duplicate pair — the module docstring's own worked
    # calibration example ("monitor RAM and memory usage" vs. an existing
    # track_memory.py sharing 2 words: memory, usage). A find_duplicate_script
    # call with this exact title (no target_path, so the #798 concrete-target
    # carve-out does not suppress different-path flagging) must return this
    # file's path.
    dup_path = scripts_dir / "track_memory.py"
    dup_path.write_text(
        \'"""Track memory usage over time."""\\ndef run():\\n    pass\\n\',
        encoding="utf-8",
    )


def _fail(message):
    sys.stderr.write("microbench correctness check failed: %s\\n" % message)
    sys.exit(1)


def _one_run():
    tmp = Path(tempfile.mkdtemp(prefix="microbench-ei-"))
    try:
        repo = tmp / "repo"
        state = tmp / "state"
        _build_corpus(repo / "scripts")

        start = time.perf_counter()
        ei.reindex(state, repo)
        for i in range(_QUERY_COUNT):
            ei.find_similar(state, "fixture script number %d corpus text" % i, limit=5)
        ei.find_duplicate_script(state, repo, "fixture script number 3 corpus text")
        elapsed_ms = (time.perf_counter() - start) * 1000.0

        # Correctness checks — deliberately NOT part of the timed window, so
        # they cannot themselves be gamed for speed; a failure here aborts
        # the whole measurement (see module docstring above).
        hits = ei.find_similar(state, "fixture script number 3 corpus text", limit=5)
        hit_paths = [h.get("path") or "" for h in hits]
        if not hits or not any("fixture_script_03.py" in p for p in hit_paths):
            _fail(
                "known-similar query for fixture_script_03 returned no hit "
                "containing the expected stem (got %r)" % (hit_paths,)
            )

        dup = ei.find_duplicate_script(state, repo, "monitor RAM and memory usage")
        if dup != "scripts/track_memory.py":
            _fail(
                "find_duplicate_script did not find the planted near-duplicate "
                "(expected scripts/track_memory.py, got %r)" % (dup,)
            )

        return elapsed_ms
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    best = min(_one_run() for _ in range(_RUNS))
    print(best)


if __name__ == "__main__":
    main()
'''


# The registry: repo-relative module path -> self-contained benchmark script
# SOURCE. Product-code-owned; there is no env var or on-disk override — an
# entry only exists here if it was added by a product change.
MICROBENCHES: dict[str, str] = {
    "nanobot/runtime/existence_index.py": _EXISTENCE_INDEX_SPEC,
}


# ─── sandboxed measurement ──────────────────────────────────────────────────


def _git_cmd(repo_root: Path) -> list[str]:
    return ["git", "-c", f"safe.directory={repo_root}", "-C", str(repo_root)]


def _sandbox_env(worktree: Path) -> dict[str, str]:
    """Minimal subprocess env for running a spec script: PYTHONPATH pinned
    to the worktree (so ``import nanobot...`` resolves against the
    CHECKED-OUT ref, not this process's own installed package) and
    HOME/TMPDIR pinned to the worktree too — no state dir, no secrets
    pass-through, no network configuration. Mirrors
    ``heldout/checkers.py``'s ``_sandbox_env``.

    PATH (and, on Windows, SYSTEMROOT) are widened relative to that
    module's hardcoded ``/usr/bin:/bin`` because Windows' CreateProcess
    needs a real PATH/SYSTEMROOT to resolve system DLLs when launching the
    interpreter — the actual production host (eeepc, POSIX) keeps the
    tight, checkers.py-style PATH.
    """
    env = {
        "PYTHONPATH": str(worktree),
        "HOME": str(worktree),
        "TMPDIR": str(worktree),
        "LANG": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    if os.name == "nt":
        env["PATH"] = os.environ.get("PATH", "")
        env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT") or os.environ.get("windir", "C:\\Windows")
        env["USERPROFILE"] = str(worktree)
        env["TMP"] = str(worktree)
        env["TEMP"] = str(worktree)
    else:
        env["PATH"] = "/usr/bin:/bin"
    return env


def run_measurement(
    repo_root: "Path", module_path: str, ref: str, *, timeout: int = 120,
) -> "float | None":
    """Measure ``MICROBENCHES[module_path]`` at git ``ref`` of ``repo_root``.

    Creates a temporary detached ``git worktree`` at ``ref``, writes the
    registered spec to a temp ``.py`` file, and runs it with
    ``sys.executable`` under the stripped sandbox env (cwd=worktree,
    PYTHONPATH=worktree) — no state dir, no network. The worktree is ALWAYS
    removed in ``finally`` (best-effort ``git worktree remove --force`` +
    a ``prune`` fallback, then the temp parent directory is deleted).

    Returns the parsed LAST stdout line as a float (milliseconds), or
    ``None`` on ANY failure: unregistered module, git worktree failure,
    non-zero exit, empty/unparseable output, or a non-finite/negative
    value. Never raises — fail-open.
    """
    spec = MICROBENCHES.get(module_path)
    if not spec:
        return None
    repo_root = Path(repo_root)
    parent: "Path | None" = None
    worktree_dir: "Path | None" = None
    spec_path: "Path | None" = None
    try:
        parent = Path(tempfile.mkdtemp(prefix="microbench-wt-"))
        worktree_dir = parent / "wt"
        add = subprocess.run(
            _git_cmd(repo_root) + ["worktree", "add", "--detach", str(worktree_dir), str(ref)],
            capture_output=True, text=True, timeout=timeout,
        )
        if add.returncode != 0 or not worktree_dir.is_dir():
            return None

        fd, spec_path_str = tempfile.mkstemp(
            suffix=".py", prefix="microbench-spec-", dir=str(parent),
        )
        os.close(fd)
        spec_path = Path(spec_path_str)
        spec_path.write_text(spec, encoding="utf-8")

        proc = subprocess.run(
            [sys.executable, str(spec_path)],
            cwd=str(worktree_dir),
            env=_sandbox_env(worktree_dir),
            capture_output=True, text=True, timeout=timeout,
        )
        if proc.returncode != 0:
            return None
        lines = [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]
        if not lines:
            return None
        try:
            value = float(lines[-1])
        except Exception:
            return None
        if not math.isfinite(value) or value < 0:
            return None
        return value
    except Exception:
        return None
    finally:
        if worktree_dir is not None:
            try:
                subprocess.run(
                    _git_cmd(repo_root) + ["worktree", "remove", "--force", str(worktree_dir)],
                    capture_output=True, text=True, timeout=timeout,
                )
            except Exception:
                pass
            try:
                subprocess.run(
                    _git_cmd(repo_root) + ["worktree", "prune"],
                    capture_output=True, text=True, timeout=timeout,
                )
            except Exception:
                pass
        if parent is not None:
            try:
                shutil.rmtree(parent, ignore_errors=True)
            except Exception:
                pass


# ─── cycle-level measurement + persistence ─────────────────────────────────


def _is_safe_cycle_id(cycle_id: Any) -> str:
    """Same rejection rule as ``benchmark_evidence._is_safe_cycle_id``: a
    ``cycle_id`` containing a path separator or ``..`` is never safe to
    join into ``<state_dir>/heldout/microbench.json``'s entries key (which
    is only ever a dict key, not a path component, here — but kept
    consistent so this module never becomes the softer link)."""
    try:
        c = str(cycle_id or "").strip()
        if not c or "/" in c or "\\" in c or ".." in c:
            return ""
        return c
    except Exception:
        return ""


def _microbench_path(state_dir: "Path") -> Path:
    return Path(state_dir) / _MICROBENCH_DIR / _MICROBENCH_FILENAME


def _load_microbench_file(state_dir: "Path") -> dict:
    path = _microbench_path(state_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("entries"), dict):
            return data
    except Exception:
        pass
    return {"schema_version": _SCHEMA, "entries": {}}


def _resolve_ref(repo_root: "Path", ref: str) -> "str | None":
    try:
        proc = subprocess.run(
            _git_cmd(repo_root) + ["rev-parse", str(ref)],
            capture_output=True, text=True, timeout=15,
        )
        if proc.returncode == 0:
            out = proc.stdout.strip()
            return out or None
        return None
    except Exception:
        return None


def measure_cycle(
    state_dir: "Path",
    repo_root: "Path",
    cycle_id: str,
    base_sha: "str | None",
    head_ref: str,
    changed_files: "list[str]",
) -> "dict | None":
    """Measure and persist a harness microbench entry for one cycle, if a
    changed file has a registered spec. #822.

    Finds the FIRST file in ``changed_files`` present in
    :data:`MICROBENCHES`, then measures THREE times via
    :func:`run_measurement`: baseline (``base_sha``), candidate
    (``head_ref``), baseline again (``base_sha``) — a drift guard
    (opus-review MED-2) against systematic host-load drift on the 2GB host
    faking an improvement between two sequential measurements. If either
    baseline run or the candidate run fails (``None``/non-positive), or the
    two baseline runs disagree by more than 5% relative
    (:data:`_BASELINE_DRIFT_MAX_REL`), the environment is judged too noisy
    to trust and this returns ``None`` — writing nothing. Otherwise
    ``baseline_ms`` is the MINIMUM of the two baseline runs (both recorded
    under ``baseline_ms_runs`` for auditability), and an entry is appended
    to ``<state_dir>/heldout/microbench.json`` keyed by ``cycle_id``
    (newest 200 entries kept, by ``measured_at_utc``) and returned.

    Also returns ``None`` — writing nothing — on: no registered changed
    file, or an unsafe ``cycle_id``/missing ``base_sha``/``head_ref``.
    Never raises.
    """
    try:
        safe_id = _is_safe_cycle_id(cycle_id)
        if not safe_id:
            return None
        if not base_sha or not head_ref:
            return None

        module_path = None
        for f in changed_files or []:
            normalized = str(f).replace("\\", "/")
            if normalized in MICROBENCHES:
                module_path = normalized
                break
        if module_path is None:
            return None

        repo_root = Path(repo_root)

        # #822 MED-2 drift guard: baseline measured TWICE, bracketing the
        # candidate measurement, so systematic host-load drift across the
        # whole window shows up as baseline disagreement rather than being
        # silently read as candidate improvement.
        baseline_ms_1 = run_measurement(repo_root, module_path, base_sha)
        if baseline_ms_1 is None or baseline_ms_1 <= 0:
            return None
        candidate_ms = run_measurement(repo_root, module_path, head_ref)
        if candidate_ms is None or candidate_ms <= 0:
            return None
        baseline_ms_2 = run_measurement(repo_root, module_path, base_sha)
        if baseline_ms_2 is None or baseline_ms_2 <= 0:
            return None

        drift = abs(baseline_ms_1 - baseline_ms_2) / min(baseline_ms_1, baseline_ms_2)
        if drift > _BASELINE_DRIFT_MAX_REL:
            return None  # environment too noisy this window — fail open

        baseline_ms = min(baseline_ms_1, baseline_ms_2)
        head_sha = _resolve_ref(repo_root, head_ref) or str(head_ref)
        improvement_pct = (baseline_ms - candidate_ms) / baseline_ms * 100.0

        entry = {
            "module": module_path,
            "metric": "wall_ms_best_of_5",
            "baseline_ms": baseline_ms,
            "baseline_ms_runs": [baseline_ms_1, baseline_ms_2],
            "candidate_ms": candidate_ms,
            "improvement_pct": improvement_pct,
            "direction": "lower",
            "base_sha": base_sha,
            "head_sha": head_sha,
            "measured_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "schema": _ENTRY_SCHEMA,
        }

        data = _load_microbench_file(state_dir)
        entries = data.get("entries")
        if not isinstance(entries, dict):
            entries = {}
        entries[safe_id] = entry
        if len(entries) > _MAX_ENTRIES:
            def _measured_at(item: tuple) -> str:
                _, e = item
                return str(e.get("measured_at_utc") or "") if isinstance(e, dict) else ""

            ordered = sorted(entries.items(), key=_measured_at)
            entries = dict(ordered[-_MAX_ENTRIES:])
        data["entries"] = entries
        data["schema_version"] = _SCHEMA

        out_path = _microbench_path(state_dir)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return entry
    except Exception:
        return None


def _entry_well_formed(entry: Any) -> bool:
    """True iff ``entry`` has the numeric shape :func:`load_microbench_entry`
    requires before treating it as authoritative: ``baseline_ms``,
    ``candidate_ms`` and ``improvement_pct`` all finite numbers, and both
    ms values strictly positive. Anything else (missing dict, non-numeric,
    NaN/inf, zero/negative ms) is malformed — never raises."""
    try:
        if not isinstance(entry, dict):
            return False
        baseline = entry.get("baseline_ms")
        candidate = entry.get("candidate_ms")
        improvement = entry.get("improvement_pct")
        for value in (baseline, candidate, improvement):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return False
            if not math.isfinite(value):
                return False
        return baseline > 0 and candidate > 0
    except Exception:
        return False


def load_microbench_entry(state_dir: "Path", cycle_id: str) -> "dict | None":
    """Return the well-formed microbench entry for ``cycle_id``, or ``None``
    if absent, unsafe, or malformed.

    This is the read side ``benchmark_evidence.verify_benchmark`` uses
    (#822): a malformed entry is deliberately treated exactly like an
    absent one — the caller falls back to its legacy 7-day-aggregate
    corroboration rather than trusting (or hard-rejecting on) a corrupt
    sidecar. Never raises.
    """
    try:
        safe_id = _is_safe_cycle_id(cycle_id)
        if not safe_id:
            return None
        data = _load_microbench_file(state_dir)
        entries = data.get("entries") or {}
        entry = entries.get(safe_id)
        if not _entry_well_formed(entry):
            return None
        return entry
    except Exception:
        return None
