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
times a SELF-CONTAINED benchmark script against two trees that differ in
EXACTLY ONE FILE — never trusting a number the instance wrote, and (rework,
live-host finding below) never trusting instance-authored git HISTORY for
the baseline either.

LIVE-HOST FINDING that forced this rework: the original design measured
baseline via ``git worktree add <tmp> <base_sha>`` against the INSTANCE
repo. On the actual eeepc host, the instance repo
(``eeebot-self-evolving``) never contains ``nanobot/`` at all — runtime-tier
cycles never integrate to instance ``main`` (#812: promotion-candidate
only), and non-slice ``nanobot/`` writes are blocked outright — so
``import nanobot`` inside that worktree ALWAYS failed and
:func:`run_measurement` returned ``None`` forever; no entry was EVER written
in production. Worse, even a hypothetical fix that made ``base_sha`` resolve
would still measure ``git`` history the INSTANCE authored — a sandbagging
vector (slow a module down in one cycle, "optimize" it back in a later one,
both baseline and candidate are equally instance-controlled history).

The fix: baseline is now the harness's own INSTALLED, root-owned product
tree — the code that is ACTUALLY RUNNING this process right now — never
anything read out of the instance's git history:

- :data:`MICROBENCHES` is a small, product-code-owned registry mapping a
  repo-relative module path to the SOURCE of a benchmark script for that
  module's hot path. There is no environment variable or on-disk file that
  can add or override an entry — the instance cannot invent its own
  benchmark and have it trusted, symmetric with #819's
  ``_HARNESS_METRICS`` allowlist.
- :data:`_product_root` is resolved once, from ``nanobot.__file__`` itself —
  the harness's own installed location, wherever that is deployed. Its
  ``nanobot/`` subtree is copied into a scratch tempdir for EVERY
  measurement (see :func:`run_measurement`); the instance never authors or
  influences this tree, and it does not depend on ANY git ref, worktree, or
  state the instance can write.
- :func:`run_measurement` measures one registered spec against that
  tree-copy: baseline (``candidate_bytes=None``) runs the pure, unmodified
  copy; a candidate run overwrites ONLY the one registered module file
  (with bytes read via ``git show <head_ref>:<module_path>`` — the cycle's
  own reviewed candidate content) before running. Everything else in the
  tree — every other module, every dependency — is held EXACTLY constant
  across baseline and candidate: this is deliberate causal isolation, not
  an approximation. Runs via ``sys.executable`` with a stripped env (mirrors
  ``heldout/checkers.py``'s ``_sandbox_env`` — no state dir, no secrets, no
  network assumptions), parsing the LAST stdout line as a float
  (milliseconds, lower=better). The scratch tree is always removed, even on
  failure/timeout. Fails open to ``None`` on any error.
- :func:`measure_cycle` is the bridge's entry point (signature UNCHANGED by
  this rework — the bridge call site needs no update): given a cycle's
  changed files, it finds the first one with a registered spec, reads that
  file's candidate bytes at ``head_ref`` via ``git show``, measures baseline
  / candidate / baseline-again (drift guard, below), and — only if
  everything succeeds — appends a bounded (200-entry) record to
  ``<state_dir>/heldout/microbench.json``. ``base_sha``/``head_ref`` are
  still recorded on the entry for provenance (and ``head_ref`` is still
  where candidate bytes come from), but ``base_sha`` no longer determines
  WHAT the baseline measurement runs — that's always the installed tree.
  MULTI-FILE CYCLES: only the one registered module's delta is measured;
  other files the cycle touched are not part of this signal at all (a
  cycle that changes 3 runtime files gets one module's isolated
  before/after, not a whole-cycle number).
- :func:`load_microbench_entry` is the read side ``benchmark_evidence``
  uses: returns a well-formed entry for a cycle_id, or ``None`` if the
  entry is absent OR malformed (a malformed entry is treated exactly like
  an absent one — never trusted, never a hard failure either).

Security invariants (do not weaken):

- The instance never supplies the measured number. Both measurements run
  in harness-spawned subprocesses over a tree the harness itself owns
  (installed product code, root-owned in production) plus, for the
  candidate run, ONE file's bytes read from the cycle's own reviewed git
  history — the instance's own scripts/state cannot influence which
  number gets written beyond choosing which files to change (its normal
  job) and what those changes contain (subject to the smoke gate that
  already ran before this measurement).
- The baseline is NEVER read from instance-authored git history (the
  live-host finding above) — it is always the currently-installed
  ``nanobot/`` tree, which only a harness/operator deploy can change. This
  closes the sandbag vector: an instance cannot slow a module down in one
  cycle and "optimize" it back in a later one, because there is no
  instance-controlled "before" to game — the installed tree simply IS the
  before, whatever it happens to be at deploy time.
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

import nanobot

_MICROBENCH_DIR = "heldout"
_MICROBENCH_FILENAME = "microbench.json"
_MAX_ENTRIES = 200
_SCHEMA = "heldout-microbench-v1"
_ENTRY_SCHEMA = "heldout-microbench-entry-v1"

# #822 MED-2: max relative disagreement allowed between the two baseline
# runs (bracketing the candidate run) before the whole measurement is
# discarded as too noisy to trust — see measure_cycle's drift guard.
_BASELINE_DRIFT_MAX_REL = 0.05

# #822 rework (live-host finding): the harness's own installed product
# root — the directory CONTAINING the ``nanobot/`` package that is actually
# running this process, wherever it is deployed. Resolved once, from
# ``nanobot.__file__`` itself, never from any git ref the instance could
# have written. Tests monkeypatch this module attribute directly (e.g.
# ``monkeypatch.setattr(microbench, "_product_root", fixture_root)``) to
# point at a synthetic product tree.
_product_root: Path = Path(nanobot.__file__).resolve().parent.parent


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


def _sandbox_env(tree_root: Path) -> dict[str, str]:
    """Minimal subprocess env for running a spec script: PYTHONPATH pinned
    to the scratch tree (so ``import nanobot...`` resolves against the
    COPIED tree, not this process's own already-imported package) and
    HOME/TMPDIR pinned to the tree too — no state dir, no secrets
    pass-through, no network configuration. Mirrors
    ``heldout/checkers.py``'s ``_sandbox_env``.

    PATH (and, on Windows, SYSTEMROOT) are widened relative to that
    module's hardcoded ``/usr/bin:/bin`` because Windows' CreateProcess
    needs a real PATH/SYSTEMROOT to resolve system DLLs when launching the
    interpreter — the actual production host (eeepc, POSIX) keeps the
    tight, checkers.py-style PATH.
    """
    env = {
        "PYTHONPATH": str(tree_root),
        "HOME": str(tree_root),
        "TMPDIR": str(tree_root),
        "LANG": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    if os.name == "nt":
        env["PATH"] = os.environ.get("PATH", "")
        env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT") or os.environ.get("windir", "C:\\Windows")
        env["USERPROFILE"] = str(tree_root)
        env["TMP"] = str(tree_root)
        env["TEMP"] = str(tree_root)
    else:
        env["PATH"] = "/usr/bin:/bin"
    return env


_IGNORE_TREE_COPY = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo")


def _git_show_bytes(repo_root: "Path", ref: str, module_path: str, *, timeout: int = 30) -> "bytes | None":
    """Read ``module_path``'s bytes at git ``ref`` of ``repo_root`` via
    ``git show <ref>:<module_path>`` — this is how a CANDIDATE run gets the
    cycle's own reviewed content (never the baseline, which is always the
    installed tree; see the module docstring). Returns ``None`` on any
    failure (bad ref, missing path, timeout, non-zero exit). Never raises."""
    try:
        proc = subprocess.run(
            _git_cmd(Path(repo_root)) + ["show", f"{ref}:{module_path}"],
            capture_output=True, timeout=timeout,
        )
        if proc.returncode != 0:
            return None
        return proc.stdout
    except Exception:
        return None


def run_measurement(
    module_path: str, candidate_bytes: "bytes | None" = None, *, timeout: int = 120,
) -> "float | None":
    """Measure ``MICROBENCHES[module_path]`` against a scratch copy of the
    harness's own INSTALLED product tree (:data:`_product_root` / ``nanobot``).

    ``candidate_bytes=None`` (the default) measures the BASELINE: the pure,
    unmodified tree-copy, exactly as installed — never anything read out of
    instance-authored git history (#822 rework; see module docstring for
    why the original git-worktree design was dead on arrival in production).
    A non-``None`` ``candidate_bytes`` measures the CANDIDATE: the same
    tree-copy with ONLY ``module_path`` overwritten by those bytes before
    running — every other file is held exactly constant, so the two
    measurements differ in exactly one file.

    The scratch tree is always removed in ``finally``, even on
    failure/timeout. Returns the parsed LAST stdout line as a float
    (milliseconds), or ``None`` on ANY failure: unregistered module, a
    missing/unreadable installed tree, a copy failure, non-zero exit,
    empty/unparseable output, or a non-finite/negative value. Never raises
    — fail-open.
    """
    spec = MICROBENCHES.get(module_path)
    if not spec:
        return None
    nanobot_src = _product_root / "nanobot"
    if not nanobot_src.is_dir():
        return None
    parent: "Path | None" = None
    spec_path: "Path | None" = None
    try:
        parent = Path(tempfile.mkdtemp(prefix="microbench-tree-"))
        tree_root = parent / "tree"
        shutil.copytree(nanobot_src, tree_root / "nanobot", ignore=_IGNORE_TREE_COPY)

        if candidate_bytes is not None:
            target = tree_root / module_path
            if not target.resolve().is_relative_to(tree_root.resolve()):
                return None  # module_path escaped the tree — refuse
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(candidate_bytes)

        fd, spec_path_str = tempfile.mkstemp(
            suffix=".py", prefix="microbench-spec-", dir=str(parent),
        )
        os.close(fd)
        spec_path = Path(spec_path_str)
        spec_path.write_text(spec, encoding="utf-8")

        proc = subprocess.run(
            [sys.executable, str(spec_path)],
            cwd=str(tree_root),
            env=_sandbox_env(tree_root),
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


_MICROBENCH_MAX_BYTES = 16 * 1024 * 1024  # _MAX_ENTRIES bounds the corpus; see #1178


def _save_microbench_file(state_dir: "Path", data: dict) -> None:
    """Write ``microbench.json`` unless the file on disk is one
    ``_load_microbench_file`` could not read (#1178 Class B) — that read
    returns an empty corpus, and writing it back would erase every baseline."""
    path = _microbench_path(state_dir)
    # #1178 Class B: the read that produced ``data`` returns a blank default
    # on a corrupt/oversize/unreadable file; writing that back would erase the
    # history. Skip and say so; an absent file is created normally.
    from nanobot.runtime.state_access import WRITABLE_STATUSES, rewrite_status

    status = rewrite_status(path, max_bytes=_MICROBENCH_MAX_BYTES)
    if status not in WRITABLE_STATUSES:
        import logging

        logging.getLogger(__name__).warning("microbench: write skipped, existing file is %s: %s", status, path)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


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
    changed file has a registered spec. #822 (rework: installed-tree
    baseline, see module docstring for the live-host finding that forced
    this). Signature UNCHANGED from the original design — the bridge call
    site needs no update.

    Finds the FIRST file in ``changed_files`` present in
    :data:`MICROBENCHES`, reads that file's candidate bytes at ``head_ref``
    via ``git show`` (:func:`_git_show_bytes`, against ``repo_root`` — the
    instance repo, which IS a valid source for the reviewed candidate
    content, unlike the baseline), then measures THREE times via
    :func:`run_measurement` against the installed product tree: baseline
    (unmodified copy), candidate (copy with only this module overwritten by
    the candidate bytes), baseline again — a drift guard (opus-review
    MED-2) against systematic host-load drift on the 2GB host faking an
    improvement between two sequential measurements. If candidate bytes
    can't be read, or either baseline run or the candidate run fails
    (``None``/non-positive), or the two baseline runs disagree by more than
    5% relative (:data:`_BASELINE_DRIFT_MAX_REL`), this returns ``None`` —
    writing nothing. Otherwise ``baseline_ms`` is the MINIMUM of the two
    baseline runs (both recorded under ``baseline_ms_runs`` for
    auditability), and an entry is appended to
    ``<state_dir>/heldout/microbench.json`` keyed by ``cycle_id`` (newest
    200 entries kept, by ``measured_at_utc``) and returned.

    ``base_sha``/``head_ref`` remain parameters (unchanged bridge call
    site) and are still recorded on the entry for provenance — but
    ``base_sha`` no longer determines WHAT the baseline measurement runs
    (that's always :data:`_product_root`'s installed tree); only
    ``head_ref`` feeds the measurement, as the source of candidate bytes.

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

        candidate_bytes = _git_show_bytes(repo_root, head_ref, module_path)
        if candidate_bytes is None:
            return None

        # #822 MED-2 drift guard: baseline measured TWICE, bracketing the
        # candidate measurement, so systematic host-load drift across the
        # whole window shows up as baseline disagreement rather than being
        # silently read as candidate improvement. Baseline is ALWAYS the
        # installed product tree (never git history — see module docstring).
        baseline_ms_1 = run_measurement(module_path)
        if baseline_ms_1 is None or baseline_ms_1 <= 0:
            return None
        candidate_ms = run_measurement(module_path, candidate_bytes)
        if candidate_ms is None or candidate_ms <= 0:
            return None
        baseline_ms_2 = run_measurement(module_path)
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
            "baseline_source": "installed",
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

        _save_microbench_file(state_dir, data)
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
