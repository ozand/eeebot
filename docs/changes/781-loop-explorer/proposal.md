# 781 — loop_explorer: static HTML + ANSI visualization of the loop's life (Tier 1)

story_id: docs/specs/subagent-bridge/spec.md (R43)
Issue: #781

## Problem

The loop's life is fully recorded — ledger cycles with demand_id chains
(#720/#760/#773), scorecard history (#765), completed/confirmed sidecars
(#761) — but there is no operator-facing way to *see* it. Reference UX:
weco.ai's flappy-bird search-tree demo — nodes are iterations colored by
score, genealogy edges, a score timeline underneath. All of our equivalent
data already exists; only the rendering is missing. Fits goal Vector 2:
terminal-first, speed-optimized local surfaces.

## Design (Tier 1 — product-side generator)

New module `nanobot/runtime/loop_explorer.py` — deterministic, NO LLM call,
fail-open throughout (missing/corrupt state degrades to an empty model /
friendly empty page, never raises):

- **`build_model(state_dir)`** — the last 200 cycle-ish events from the
  ledger, rotation-aware (current `cycles.jsonl` + up to 7 newest
  `cycles-*.jsonl.gz` archives — the scorecard's `_ledger_rows` approach;
  the #771/#772/#773 rotation lesson). Events: idle heartbeats,
  proposer_skip/proposer_reject rows, and CYCLES (proposed/dedup/gate/
  outcome rows grouped by `cycle_id`), each carrying title, demand_id,
  dedup decision, matched_against, outcome, reason, files_changed, and a
  `confirmed` flag joined from `demand/completed.json` (#761). Plus
  `chains` (events grouped by demand_id — the genealogy: demand item → its
  proposals/rejects → integration → confirmed) and `scorecard_series`
  (bounded last-100 read of `scorecard/history.jsonl`: integrations,
  tokens_per_integration, heldout_gap, repeat_failure_rate).
- **`render_html(model)`** — ONE self-contained dark-theme HTML page:
  inline CSS, minimal vanilla JS, inline SVG charts, NO external resource
  of any kind (regression-pinned: no `http` substring anywhere — must
  render offline, opened as a file on the host). Top: the horizontal cycle
  strip, one small colored block per event (green=success,
  teal=success+confirmed, yellow=skip, gray=idle, orange=proposer_reject,
  blue=proposer_skip, red=failed), hover/click → detail panel. Middle:
  demand chains with completed/confirmed badges. Bottom: SVG line charts
  for the four scorecard series. Sized small: per-class colors as generated
  CSS rules (not per-block inline styles), embedded event JSON compacted
  (empty fields dropped) — ~60KB even on a dense synthetic 200-event
  window, far less on typical state.
- **`render_ansi(model)`** — terminal fallback: the strip as one colored
  character per event (ANSI colors matching the HTML legend; per-class
  ASCII chars `C S k X R n .`), legend, last-10-event detail lines, and a
  one-line scorecard summary. Degrades to plain ASCII when `NO_COLOR` is
  set.
- **`update_explorer(state_dir)`** — watermark-gated regeneration
  (sidecar `<state_dir>/explorer/watermark.json`; regenerate when the
  ledger's byte-size/mtime changed OR 30 min elapsed — the system_map
  no-op-gate pattern), writing `<state_dir>/explorer/index.html`. Wired
  into the scorecard recompute path (#765, itself 30-min watermarked),
  wrapped fail-open — zero extra cost when idle, and a rendering bug can
  never break the scorecard or demand collection.
- **CLI** `scripts/loop_explorer_cli.py` — `--state-dir` (STATE_DIR env /
  eeepc default, the `loop_metrics_report.py` convention), `--ansi`
  (default), `--html PATH`, `--test` (self-check on a synthetic fixture
  state dir, the loop_metrics_report pattern).

## Out of scope

Tier 2 (the instance-built ASCII cycle-timeline section in
`eeebot_dashboard.py`, P15) is a separate operator seeding action after
Tier 1 ships — the system visualizing its own life through its own demand
pipeline. Not part of this PR.

## Tests

`tests/test_loop_explorer.py`: rotation-aware ordering/grouping (fixture
incl. one .gz archive), demand-chain grouping + confirmed join, last-N
bound, empty/missing/corrupt-state degradation, HTML strip classes +
detail data + strict self-containment (no `http`, no `<link>`, no
`<script src>`), ANSI colored / NO_COLOR plain-ASCII / empty-model
message, watermark no-op + regeneration on append + 30-min expiry +
fail-open, scorecard-recompute wiring, and the CLI `--test` self-check.
