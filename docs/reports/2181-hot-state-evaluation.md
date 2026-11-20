# #2181 disposable hot-state comparison

## Predeclared protocol

Before the first benchmark, #2181 comment [7048133613](https://github.com/ozand/eeebot/issues/2181#issuecomment-7048133613) declared the coupled transition, baseline, environment, process-failure model, measurement set, and screening thresholds. This run used the current file writers at base `2848397fb388d690d27b7639ae634de059b6bc37`, versus a disposable stdlib `sqlite3` prototype. Nothing was migrated or installed; no host or production state was accessed.

- **Coupled transition:** `cycle_ledger.record_cycle_outcome()` appends one terminal outcome and then its existing cross-module behavior clears the matching `open_increment.running` record. The recovery reader checks for an actual terminal outcome before reconciling the registration. No telemetry or inferred no-execution evidence is used.
- **Same logical input:** registered attempt and terminal event with identical JSON-shaped values; 1,000 historical outcome records preloaded in both backends. Baseline uses the repository's real JSON atomic replacement and JSONL append/recovery code. SQLite uses a disposable singleton active-attempt row and outcome table; transaction atomically inserts the outcome and clears the running registration. Recovery looks up the cycle's terminal outcome and clears only when one exists.
- **Failure model:** child process exits before work, during transaction/file append, or after committed/complete transition; parent reopens the store. For JSONL partial-write injection, only half a JSON payload is written before abrupt exit. These are process-exit tests only, not power-loss, storage-controller/cache, disk-full, cross-process contention, or external-effect tests.
- **Metrics:** 5 independent disposable runs × 1,000 transitions per backend, execution order alternated. Per-run p50/p95 transition wall time (terminalization only; recovery-reader performance not measured); median per-run p50 and p95 plus max p95; child-process peak Windows working set; final state bytes. Report is sanitized: local NTFS fixed-disk volume is stated, exact path withheld.
- **Predeclared screening thresholds:** recovery no harder than baseline; SQLite p95 ≤ 2× baseline and ≤ 10 ms; peak memory increase ≤ 8 MiB; evidenced target 32-bit compatibility. Failure of any is no-adoption recommendation. These are evaluation decision cutoffs, not production SLOs.

## Environment and reproducibility

Windows 11 `10.0.26200`, AMD64, fixed NTFS local volume, Python 3.13.17 64-bit, SQLite 3.50.4. SQLite settings: WAL, `synchronous=FULL`. The process memory metric is child peak working set (`psutil.Process().memory_info().peak_wset`), which includes shared DLL mappings and is noisy; it is not private bytes or target-host RAM.

Run from repository root:

```powershell
py -3.13 -m py_compile scripts/evaluate_hot_state_storage.py
py -3.13 -m ruff check scripts/evaluate_hot_state_storage.py
py -3.13 scripts/evaluate_hot_state_storage.py --output <disposable-output-path> --runs 5 --operations 1000
```

The script creates isolated temporary trees and child processes. It prints the final JSON to stdout, then writes a copy only to a new output path resolved outside the repository; it refuses repository paths and overwrites. Exact final output was captured to `C:/Temp/2181-eval-final-20261120-verified.json`, outside the repository. No third-party packages were installed; `psutil` was already present in the selected Python environment.

## Results

| Metric | Current files | SQLite WAL | Observation |
|---|---:|---:|---|
| Median of per-run p50 | 4.6727 ms | 2.1839 ms | SQLite ~2.14× lower median p50 |
| Median of per-run p95 | 12.1323 ms | 2.4389 ms | SQLite ~4.97× lower median p95 |
| Worst per-run p95 | 12.9574 ms | 2.4550 ms | SQLite stayed below the predeclared 10 ms cap |
| Store bytes after run | 363,009 | 389,120 | SQLite ~7.2% larger for this tiny 1,000-row workload including index |
| Child peak working set | 30,306,304 B | 29,683,712 B | SQLite delta −622,592 B (~−0.59 MiB); noisy host metric, no memory benefit claim |

The SQLite latency thresholds passed on this workstation. The measured memory did not exceed the cap but cannot establish the requested 32-bit host RAM envelope. One backend's byte count does not include equivalent archive compression: this sample uses one active JSONL file, no rotation/compression; SQLite files are database plus WAL/SHM and an explicit `cycle_id` index at the end of the run (after connection close). File and SQLite encodings/storage layouts differ, so bytes are descriptive only. The 1,000 historical rows are populated/indexed for the SQLite prototype, while the real file `read_events_across_rotation()` scans JSONL rows in its capped archive window. These reader lookups are used only during crash/reopen inspection, not timed benchmark loops; reader-path cost is not included in the latency comparison.

### Process interruption and reopen

- **Unresolved/no terminal outcome:** baseline before-transition and SQLite before/before-commit reopen with the original registration intact and classify it as held/unresolved. SQLite process termination mid-transaction rolls back both attempted row and state update; it does not manufacture settlement.
- **Crash after the existing file terminal append but before registration clear:** JSONL has one valid terminal row while `running` remains. Existing `check_running_for_kill()` reads that durable row, clears the matching registration, and leaves no new attempt classification; this is a verifiable recovery case.
- **SQLite mid-transaction:** reopen shows zero terminal rows and the original registration intact. **After commit:** reopen shows one outcome and registration already cleared atomically.
- **JSONL torn append:** half-record crash leaves one invalid, non-JSON line. The current reader skips it (zero terminal outcome), so no authority to clear the running registration is inferred. Normal append before/after transition gives expected complete row behavior.
- The script also exercises a standalone ledger-only append crash to identify the append framing limit, separate from the coupled transition. It reports JSONL row validity/count for that case; the ledger reader skips the partial row. Registration reconciliation is marked not applicable because no attempt registration exists in the separate ledger-only probe.

## 32-bit and failure limitations

No 32-bit Python runtime was registered locally (`py -0p` lists only 64-bit builds); `py -3.13-32 --version` returned “No suitable Python runtime found.” No install was attempted. This is **not** actual i386/32-bit evidence. Python stdlib `sqlite3` exists in principle for supported CPython builds, but that alone is insufficient to verify the deployed target's interpreter, SQLite build/compile options, filesystem locking, WAL support, memory headroom, or performance. Because target 32-bit evidence is a predeclared criterion, the threshold is unmet.

`WAL`/`synchronous=FULL` was selected for this process-crash test. It does not make these observations a power-loss test; real power loss, storage caches, and filesystem/device guarantees remain untested. Even a local transaction cannot include subprocess, model, Git, or remote effects. The SQLite recovery branch used in the experiment is a narrow evaluator duplicate that clears a registration only after finding a terminal row; it is not production #2162 logic. No conclusion about executor settlement, stale-call authority, or #2162 recovery is made by this prototype.

## Recommendation

**No adoption recommendation now; keep current storage.** The coupled transition is materially simpler to make atomic in the SQLite prototype, and the measured Windows workstation latency is better. However, threshold (d)—actual target 32-bit compatibility evidence—is unmet, and this host's benchmark does not settle deployment durability, disk-full, backup/WAL restore, reader compatibility, cross-process locking, or external-effect reconciliation. Any future adoption requires a separate governing issue and the ADR, compatibility, backup/restore, rollback, verification, and independent exact-revision review gates already stated in ADR-059. This report and prototype do not authorize migration or production use.
