"""Static loop-explorer visualization — the loop's life as one HTML page (#781).

Reference UX: weco.ai's flappy-bird search-tree demo — nodes are iterations
with score and genealogy, a score timeline underneath. All the data already
exists in harness-owned state: the cycle ledger (#720, demand_id chains via
#760/#773), the scorecard history (#765), and the completed/confirmed
sidecars (#761). This module renders it, deterministically and LLM-free:

- :func:`build_model` — parse the last :data:`_MAX_EVENTS` cycle-ish events
  from the ledger (rotation-aware: the current ``cycles.jsonl`` PLUS the
  newest ``cycles-*.jsonl.gz`` archives — the #771/#772/#773 lesson: a
  midnight rotation blinds every single-file ledger reader). Events are
  idle heartbeats, proposer skips/rejects, and CYCLES (``proposed`` /
  ``dedup`` / ``gate`` / ``outcome`` rows grouped by ``cycle_id``), each
  carrying title, ``demand_id``, dedup decision, outcome, files, and a
  ``confirmed`` flag joined from ``demand/completed.json`` (#761). Plus
  ``chains`` (events grouped by ``demand_id`` — the genealogy) and
  ``scorecard_series`` (bounded read of ``scorecard/history.jsonl`` for the
  timeline: integrations, tokens_per_integration, heldout_gap,
  repeat_failure_rate).
- :func:`render_html` — ONE self-contained dark-theme HTML file: inline CSS,
  minimal vanilla JS, inline SVG charts, NO external resource of any kind
  (must render offline, opened as a file on the host). Top: the cycle strip
  (one colored block per event, click → detail panel). Middle: demand
  chains. Bottom: the scorecard timeline.
- :func:`render_ansi` — terminal fallback: the strip as one colored
  character per event (matching the HTML legend), a legend, the last 10
  events, and a one-line scorecard summary. Degrades to plain ASCII when
  the ``NO_COLOR`` environment variable is set.
- :func:`update_explorer` — watermark-gated regeneration (ledger byte-size/
  mtime change OR 30 min elapsed; sidecar ``<state_dir>/explorer/
  watermark.json``) writing ``<state_dir>/explorer/index.html``. Invoked
  from the scorecard recompute path (#765, itself 30-min watermarked), so
  idle cycles pay one small stat/read.

Everything here is deterministic and fail-open: a missing/corrupt file, an
unreadable directory, or any unexpected exception degrades to an empty
model / a friendly empty page — never raises into the caller.
"""
from __future__ import annotations

import gzip
import html as _html
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

EXPLORER_SCHEMA = "loop-explorer-v1"

_MAX_EVENTS = 200
_MAX_HISTORY_ENTRIES = 100
_MAX_GZ_FILES = 7  # bounded archive read — same discipline as scorecard
_REGEN_MINUTES = 30
_WATERMARK_SCHEMA = "loop-explorer-watermark-v1"

# Event class → (legend label, HTML CSS color, ANSI SGR code, ASCII char).
# One shared table so the HTML legend, the ANSI strip, and the tests all
# speak the same language.
CLASSES: dict[str, dict[str, str]] = {
    "confirmed": {"label": "success+confirmed", "color": "#2dd4bf", "ansi": "38;5;44", "char": "C"},
    "success": {"label": "success", "color": "#4ade80", "ansi": "32", "char": "S"},
    "skip": {"label": "skipped (dedup)", "color": "#facc15", "ansi": "33", "char": "k"},
    "failed": {"label": "failed/timeout", "color": "#f87171", "ansi": "31", "char": "X"},
    "reject": {"label": "proposer_reject", "color": "#fb923c", "ansi": "38;5;208", "char": "R"},
    "noop": {"label": "proposer_skip (honest no-op)", "color": "#60a5fa", "ansi": "34", "char": "n"},
    "idle": {"label": "idle heartbeat", "color": "#6b7280", "ansi": "90", "char": "."},
}


# ─── small shared helpers (same shapes as scorecard.py) ─────────────────────


def _read_json(path: Path, default: Any) -> Any:
    try:
        if not path.is_file():
            return default
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, data: Any) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _parse_ts(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except Exception:
        return None


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _explorer_dir(state_dir: Path) -> Path:
    return Path(state_dir) / "explorer"


# ─── ledger reading (rotation-aware, bounded) ───────────────────────────────


def _ledger_rows(state_dir: Path) -> list[dict[str, Any]]:
    """All parseable rows from ``ledger/cycles.jsonl`` PLUS up to
    :data:`_MAX_GZ_FILES` newest rotated ``cycles-*.jsonl.gz`` archives
    (scorecard's rotation-aware approach; no time cutoff here — the event
    bound is applied after grouping). Fail-open per file."""
    rows: list[dict[str, Any]] = []

    def _consume(lines: list[str]) -> None:
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if isinstance(row, dict) and _parse_ts(row.get("ts")) is not None:
                rows.append(row)

    try:
        ledger_dir = Path(state_dir) / "ledger"
        if not ledger_dir.is_dir():
            return rows
        try:
            archives = sorted(ledger_dir.glob("cycles-*.jsonl.gz"), reverse=True)
        except Exception:
            archives = []
        # Oldest of the selected archives first, active file last — the
        # natural chronological order; a final sort fixes any stragglers.
        for gz_path in reversed(archives[:_MAX_GZ_FILES]):
            try:
                with gzip.open(gz_path, "rt", encoding="utf-8") as fh:
                    _consume(fh.read().splitlines())
            except Exception:
                continue
        active = ledger_dir / "cycles.jsonl"
        if active.is_file():
            try:
                _consume(active.read_text(encoding="utf-8").splitlines())
            except Exception:
                pass
        return rows
    except Exception:
        return rows


# ─── model ──────────────────────────────────────────────────────────────────


def _completed_entries(state_dir: Path) -> dict[str, dict[str, Any]]:
    data = _read_json(Path(state_dir) / "demand" / "completed.json", None)
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, dict):
        return {}
    return {k: v for k, v in entries.items() if isinstance(v, dict)}


def _event_class(event: dict[str, Any]) -> str:
    etype = event.get("type")
    if etype == "idle":
        return "idle"
    if etype == "proposer_skip":
        return "noop"
    if etype == "proposer_reject":
        return "reject"
    outcome = str(event.get("outcome") or "").strip().lower()
    if outcome == "success":
        return "confirmed" if event.get("confirmed") else "success"
    if outcome.startswith("skipped"):
        return "skip"
    return "failed"


def build_model(state_dir: Path) -> dict[str, Any]:
    """Deterministic explorer model over harness-owned state. Fail-open:
    any error degrades to an empty model, never raises."""
    try:
        state_dir = Path(state_dir)
        rows = _ledger_rows(state_dir)
        completed = _completed_entries(state_dir)

        # Group cycle-phase rows by cycle_id; keep loose rows as events.
        cycles: dict[str, dict[str, Any]] = {}
        loose: list[dict[str, Any]] = []
        for row in rows:
            phase = row.get("phase")
            if phase == "idle":
                loose.append(
                    {"type": "idle", "ts": row.get("ts"), "reason": row.get("reason") or ""}
                )
            elif phase == "proposer_skip":
                loose.append(
                    {
                        "type": "proposer_skip",
                        "ts": row.get("ts"),
                        "reason": str(row.get("reason") or "")[:200],
                    }
                )
            elif phase == "proposer_reject":
                loose.append(
                    {
                        "type": "proposer_reject",
                        "ts": row.get("ts"),
                        "reason": str(row.get("reason") or "")[:200],
                        "title": str(row.get("task_title") or "")[:200],
                        "demand_id": str(row.get("demand_id") or ""),
                        "matched_against": str(row.get("matched_against") or "")[:200],
                    }
                )
            elif phase in ("proposed", "dedup", "gate", "outcome", "started"):
                cycle_id = str(row.get("cycle_id") or "").strip()
                if not cycle_id:
                    continue
                bucket = cycles.setdefault(cycle_id, {"rows": []})
                bucket["rows"].append(row)

        events: list[dict[str, Any]] = list(loose)
        for cycle_id, bucket in cycles.items():
            crows: list[dict[str, Any]] = bucket["rows"]
            proposed = next((r for r in crows if r.get("phase") == "proposed"), None)
            dedup = next((r for r in reversed(crows) if r.get("phase") == "dedup"), None)
            outcome = next((r for r in reversed(crows) if r.get("phase") == "outcome"), None)
            timestamps = [t for t in (_parse_ts(r.get("ts")) for r in crows) if t is not None]
            if not timestamps:
                continue
            demand_id = str((proposed or {}).get("demand_id") or "").strip()
            files = (outcome or {}).get("files_changed")
            entry = completed.get(demand_id) if demand_id else None
            events.append(
                {
                    "type": "cycle",
                    "ts": _iso(min(timestamps)),
                    "cycle_id": cycle_id,
                    "title": str((proposed or {}).get("task_title") or "")[:200],
                    "demand_id": demand_id,
                    "dedup_decision": str((dedup or {}).get("decision") or ""),
                    "matched_against": str((dedup or {}).get("matched_against") or "")[:200],
                    "outcome": str((outcome or {}).get("outcome") or "incomplete"),
                    "reason": str((outcome or {}).get("reason") or "")[:200],
                    "files_changed": files if isinstance(files, list) else [],
                    "confirmed": bool(entry.get("confirmed") is True) if entry else False,
                }
            )

        events = [e for e in events if _parse_ts(e.get("ts")) is not None]
        events.sort(key=lambda e: str(e.get("ts")))
        events = events[-_MAX_EVENTS:]
        for event in events:
            event["class"] = _event_class(event)

        chains: list[dict[str, Any]] = []
        by_demand: dict[str, list[dict[str, Any]]] = {}
        for event in events:
            demand_id = str(event.get("demand_id") or "").strip()
            if demand_id:
                by_demand.setdefault(demand_id, []).append(event)
        for demand_id, chain_events in by_demand.items():
            entry = completed.get(demand_id)
            chains.append(
                {
                    "demand_id": demand_id,
                    "completed": entry is not None,
                    "confirmed": bool(entry.get("confirmed") is True) if entry else False,
                    "events": chain_events,
                }
            )
        chains.sort(key=lambda c: str(c["events"][-1].get("ts")), reverse=True)

        series: list[dict[str, Any]] = []
        try:
            history_path = Path(state_dir) / "scorecard" / "history.jsonl"
            if history_path.is_file():
                lines = history_path.read_text(encoding="utf-8").splitlines()
                for line in lines[-_MAX_HISTORY_ENTRIES:]:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        snap = json.loads(line)
                    except Exception:
                        continue
                    if not isinstance(snap, dict):
                        continue
                    series.append(
                        {
                            "ts": snap.get("computed_at_utc"),
                            "integrations": (snap.get("loop") or {}).get("integrations"),
                            "tokens_per_integration": (snap.get("cost") or {}).get(
                                "tokens_per_integration"
                            ),
                            "heldout_gap": (snap.get("heldout") or {}).get("heldout_gap"),
                            "repeat_failure_rate": (snap.get("loop") or {}).get(
                                "repeat_failure_rate"
                            ),
                        }
                    )
        except Exception:
            pass

        counts: dict[str, int] = {}
        for event in events:
            counts[event["class"]] = counts.get(event["class"], 0) + 1

        return {
            "schema_version": EXPLORER_SCHEMA,
            "generated_at_utc": _iso(datetime.now(timezone.utc)),
            "window": {
                "start": events[0]["ts"] if events else None,
                "end": events[-1]["ts"] if events else None,
                "n_events": len(events),
            },
            "counts": counts,
            "events": events,
            "chains": chains,
            "scorecard_series": series,
        }
    except Exception:
        return {
            "schema_version": EXPLORER_SCHEMA,
            "generated_at_utc": _iso(datetime.now(timezone.utc)),
            "window": {"start": None, "end": None, "n_events": 0},
            "counts": {},
            "events": [],
            "chains": [],
            "scorecard_series": [],
        }


# ─── HTML rendering ─────────────────────────────────────────────────────────

_CSS = """
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body { background:#0f1115; color:#d1d5db; font:13px/1.5 monospace; margin:0; padding:16px 20px; }
h1 { font-size:15px; color:#e5e7eb; margin:0 0 4px; }
h2 { font-size:13px; color:#9ca3af; margin:20px 0 6px; border-bottom:1px solid #1f2430; padding-bottom:3px; }
.meta { color:#6b7280; font-size:11px; }
#strip { display:flex; flex-wrap:wrap; gap:2px; margin:10px 0 6px; }
#strip .b { width:12px; height:18px; border-radius:2px; cursor:pointer; opacity:.9; }
#strip .b:hover { outline:1px solid #e5e7eb; opacity:1; }
#strip .b.sel { outline:2px solid #e5e7eb; }
.legend { display:flex; flex-wrap:wrap; gap:12px; font-size:11px; color:#9ca3af; margin:4px 0 10px; }
.legend .sw { display:inline-block; width:10px; height:10px; border-radius:2px; margin-right:4px; vertical-align:-1px; }
#detail { background:#161a22; border:1px solid #1f2430; border-radius:4px; padding:10px 12px; min-height:64px; font-size:12px; }
#detail .k { color:#6b7280; display:inline-block; min-width:130px; }
.chain { background:#161a22; border:1px solid #1f2430; border-radius:4px; padding:8px 12px; margin:6px 0; font-size:12px; }
.chain .did { color:#93c5fd; }
.badge { display:inline-block; font-size:10px; border-radius:3px; padding:0 5px; margin-left:6px; }
.badge.ok { background:#134e4a; color:#2dd4bf; }
.badge.done { background:#1e3a5f; color:#93c5fd; }
.chain ul { margin:4px 0 0; padding-left:18px; }
.chart { margin:8px 0 14px; }
.chart .t { font-size:11px; color:#9ca3af; margin-bottom:2px; }
svg { background:#161a22; border:1px solid #1f2430; border-radius:4px; }
svg text { fill:#6b7280; font:9px monospace; }
svg polyline { fill:none; stroke:#60a5fa; stroke-width:1.5; }
svg circle { fill:#60a5fa; }
.empty { color:#6b7280; }
"""

_JS = """
var M = JSON.parse(document.getElementById('model').textContent);
var detail = document.getElementById('detail');
function esc(s){var d=document.createElement('span');d.textContent=s==null?'':String(s);return d.innerHTML;}
function row(k,v){return v?'<div><span class="k">'+esc(k)+'</span>'+esc(v)+'</div>':'';}
function show(i, el){
  var e = M.events[i]; if(!e) return;
  var sel = document.querySelector('#strip .b.sel'); if(sel) sel.classList.remove('sel');
  if(el) el.classList.add('sel');
  detail.innerHTML =
    row('ts', e.ts) + row('kind', e.type + ' [' + e['class'] + ']') +
    row('title', e.title) + row('cycle_id', e.cycle_id) +
    row('demand_id', e.demand_id) + row('dedup', e.dedup_decision) +
    row('matched_against', e.matched_against) + row('outcome', e.outcome) +
    row('reason', e.reason) +
    row('confirmed', e.type==='cycle' ? (e.confirmed?'yes':'no') : '') +
    row('files_changed', (e.files_changed||[]).join(', '));
}
var blocks = document.querySelectorAll('#strip .b');
blocks.forEach(function(b){
  var i = parseInt(b.getAttribute('data-i'), 10);
  b.addEventListener('click', function(){ show(i, b); });
  b.addEventListener('mouseenter', function(){ show(i, b); });
});
if (blocks.length) show(M.events.length - 1, blocks[blocks.length - 1]);
"""

_SERIES_SPECS = (
    ("integrations", "integrations (per scorecard snapshot, 7d window)"),
    ("tokens_per_integration", "tokens per integration"),
    ("heldout_gap", "heldout gap (failed / passed+failed)"),
    ("repeat_failure_rate", "repeat failure rate"),
)


def _svg_chart(series: list[dict[str, Any]], key: str) -> str:
    """Inline SVG step/line chart for one scorecard series. Pure Python —
    no JS charting. ``None`` values are skipped; <2 points renders a
    friendly placeholder."""
    width, height, pad = 640, 90, 26
    points = [
        (i, float(entry[key]))
        for i, entry in enumerate(series)
        if isinstance(entry.get(key), (int, float))
    ]
    if len(points) < 2:
        return '<div class="empty">(not enough scorecard history yet)</div>'
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    y_min, y_max = min(ys), max(ys)
    y_span = (y_max - y_min) or 1.0
    x_min, x_max = min(xs), max(xs)
    x_span = (x_max - x_min) or 1

    def sx(x: float) -> float:
        return pad + (x - x_min) / x_span * (width - 2 * pad)

    def sy(y: float) -> float:
        return (height - pad / 2) - (y - y_min) / y_span * (height - 1.5 * pad)

    coords = " ".join(f"{sx(x):.1f},{sy(y):.1f}" for x, y in points)
    # Point markers only for sparse series — a dense 100-point series is
    # readable as a line alone and the circles would triple the SVG size.
    dots = (
        "".join(f'<circle cx="{sx(x):.1f}" cy="{sy(y):.1f}" r="1.8"/>' for x, y in points)
        if len(points) <= 40
        else ""
    )
    first_ts = str(series[points[0][0]].get("ts") or "")[:16]
    last_ts = str(series[points[-1][0]].get("ts") or "")[:16]
    return (
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}">'
        f'<text x="2" y="{pad / 2 + 3:.0f}" text-anchor="start">{y_max:g}</text>'
        f'<text x="2" y="{height - pad / 2:.0f}" text-anchor="start">{y_min:g}</text>'
        f'<text x="{pad}" y="{height - 4}" text-anchor="start">{_html.escape(first_ts)}</text>'
        f'<text x="{width - pad}" y="{height - 4}" text-anchor="end">{_html.escape(last_ts)}</text>'
        f'<polyline points="{coords}"/>{dots}</svg>'
    )


def render_html(model: dict[str, Any]) -> str:
    """One self-contained dark-theme HTML page: inline CSS + minimal
    vanilla JS + inline SVG; NO external resource of any kind (renders
    offline, opened as a file)."""
    events: list[dict[str, Any]] = model.get("events") or []
    chains: list[dict[str, Any]] = model.get("chains") or []
    series: list[dict[str, Any]] = model.get("scorecard_series") or []
    window = model.get("window") or {}
    counts = model.get("counts") or {}

    # Per-class background colors live in generated CSS rules (below), not
    # per-block inline styles — keeps 200 blocks small. Hover detail comes
    # from the JS mouseenter handler, so no title attribute either.
    class_css = "".join(
        f"#strip .b.{name}{{background:{spec['color']}}}"
        f".legend .sw.{name}{{background:{spec['color']}}}"
        for name, spec in CLASSES.items()
    )
    strip = "".join(
        f'<span class="b {e.get("class", "idle")}" data-i="{i}"></span>'
        for i, e in enumerate(events)
    ) or '<span class="empty">(no loop events recorded yet)</span>'

    legend = "".join(
        f'<span><span class="sw {name}"></span>'
        f"{_html.escape(spec['label'])} ({counts.get(name, 0)})</span>"
        for name, spec in CLASSES.items()
    )

    chain_blocks: list[str] = []
    for chain in chains:
        badges = ""
        if chain.get("confirmed"):
            badges += '<span class="badge ok">confirmed</span>'
        elif chain.get("completed"):
            badges += '<span class="badge done">completed</span>'
        items = "".join(
            "<li>"
            + _html.escape(str(e.get("ts") or "")[:16])
            + " — "
            + _html.escape(
                str(e.get("title") or e.get("reason") or e.get("type") or "(untitled)")
            )
            + " → "
            + _html.escape(
                str(e.get("outcome") or e.get("type") or "")
                + (f" ({e.get('reason')})" if e.get("type") != "cycle" and e.get("reason") else "")
            )
            + "</li>"
            for e in chain.get("events") or []
        )
        chain_blocks.append(
            f'<div class="chain"><span class="did">{_html.escape(chain["demand_id"])}</span>'
            f"{badges}<ul>{items}</ul></div>"
        )
    chains_html = "".join(chain_blocks) or '<div class="empty">(no demand chains in window)</div>'

    charts = "".join(
        f'<div class="chart"><div class="t">{_html.escape(title)}</div>{_svg_chart(series, key)}</div>'
        for key, title in _SERIES_SPECS
    )

    # Compact embedded model: empty/False fields dropped (the JS `row()`
    # helper already skips missing keys) — keeps a typical 200-event page
    # well under ~60KB.
    compact_events = [
        {k: v for k, v in event.items() if v not in ("", None, False, [])}
        for event in events
    ]
    model_json = json.dumps(
        {"events": compact_events}, ensure_ascii=False, separators=(",", ":")
    ).replace("</", "<\\/")

    return (
        "<!DOCTYPE html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        "<title>eeebot loop explorer</title>"
        f"<style>{_CSS}{class_css}</style></head><body>"
        "<h1>eeebot loop explorer</h1>"
        f'<div class="meta">generated {_html.escape(str(model.get("generated_at_utc") or ""))}'
        f' &middot; window {_html.escape(str(window.get("start") or "n/a"))} .. '
        f'{_html.escape(str(window.get("end") or "n/a"))}'
        f' &middot; {window.get("n_events", 0)} events</div>'
        f'<div id="strip">{strip}</div>'
        f'<div class="legend">{legend}</div>'
        '<div id="detail"><span class="empty">hover or click a block for detail</span></div>'
        f"<h2>demand chains</h2>{chains_html}"
        f"<h2>scorecard timeline</h2>{charts}"
        f'<script type="application/json" id="model">{model_json}</script>'
        f"<script>{_JS}</script>"
        "</body></html>"
    )


# ─── ANSI rendering ─────────────────────────────────────────────────────────


def _color_enabled() -> bool:
    return not os.environ.get("NO_COLOR")


def render_ansi(model: dict[str, Any]) -> str:
    """Terminal fallback: the strip as one colored character per event
    (colors matching the HTML legend), a legend, the last 10 events, and a
    one-line scorecard summary. Plain ASCII when ``NO_COLOR`` is set."""
    events: list[dict[str, Any]] = model.get("events") or []
    window = model.get("window") or {}
    counts = model.get("counts") or {}
    color = _color_enabled()

    def paint(cls: str, text: str) -> str:
        if not color:
            return text
        spec = CLASSES.get(cls, CLASSES["idle"])
        return f"\x1b[{spec['ansi']}m{text}\x1b[0m"

    dash = "—" if color else "-"  # plain ASCII when NO_COLOR is set
    lines: list[str] = []
    lines.append(
        f"eeebot loop explorer {dash} {window.get('n_events', 0)} events, "
        f"window {window.get('start') or 'n/a'} .. {window.get('end') or 'n/a'}"
    )
    if not events:
        lines.append("(no loop events recorded yet)")
        return "\n".join(lines)

    strip = "".join(
        paint(e.get("class", "idle"), CLASSES.get(e.get("class", "idle"), CLASSES["idle"])["char"])
        for e in events
    )
    lines.append("")
    lines.append(strip)
    lines.append("")
    lines.append(
        "legend: "
        + "  ".join(
            f"{paint(name, spec['char'])}={spec['label']} ({counts.get(name, 0)})"
            for name, spec in CLASSES.items()
        )
    )
    lines.append("")
    lines.append("last events:")
    for event in events[-10:]:
        cls = event.get("class", "idle")
        marker = paint(cls, CLASSES.get(cls, CLASSES["idle"])["char"])
        label = str(
            event.get("title") or event.get("reason") or event.get("type") or "(untitled)"
        )[:70]
        tail = ""
        if event.get("type") == "cycle":
            tail = f" -> {event.get('outcome')}"
            if event.get("confirmed"):
                tail += " [confirmed]"
            if event.get("reason"):
                tail += f" ({event.get('reason')})"
        lines.append(f"  {marker} {str(event.get('ts') or '')[:16]}  {label}{tail}")

    series: list[dict[str, Any]] = model.get("scorecard_series") or []
    if series:
        last = series[-1]

        def fmt(key: str) -> str:
            value = last.get(key)
            return "n/a" if value is None else str(value)

        lines.append("")
        lines.append(
            "scorecard: "
            f"integrations={fmt('integrations')}  "
            f"tokens/integration={fmt('tokens_per_integration')}  "
            f"heldout_gap={fmt('heldout_gap')}  "
            f"repeat_failure_rate={fmt('repeat_failure_rate')}"
        )
    return "\n".join(lines)


# ─── watermark-gated regeneration ───────────────────────────────────────────


def _ledger_stat(state_dir: Path) -> tuple[int, float]:
    """(size, mtime) of the active ledger file — (0, 0.0) when absent."""
    try:
        stat = (Path(state_dir) / "ledger" / "cycles.jsonl").stat()
        return stat.st_size, stat.st_mtime
    except Exception:
        return 0, 0.0


def update_explorer(state_dir: Path, *, now: datetime | None = None) -> Path | None:
    """Regenerate ``<state_dir>/explorer/index.html`` when the ledger
    changed (byte-size/mtime) OR :data:`_REGEN_MINUTES` elapsed since the
    last generation (sidecar ``explorer/watermark.json`` — the system_map
    no-op-gate pattern). Returns the written path, or ``None`` on a
    watermark no-op or any failure. Fail-open: never raises."""
    try:
        state_dir = Path(state_dir)
        now = now or datetime.now(timezone.utc)
        out_dir = _explorer_dir(state_dir)
        index_path = out_dir / "index.html"
        wm_path = out_dir / "watermark.json"

        size, mtime = _ledger_stat(state_dir)
        watermark = _read_json(wm_path, None)
        if isinstance(watermark, dict) and index_path.is_file():
            generated_at = _parse_ts(watermark.get("generated_at_utc"))
            unchanged = (
                watermark.get("ledger_size") == size
                and watermark.get("ledger_mtime") == mtime
            )
            fresh = (
                generated_at is not None
                and timedelta() <= (now - generated_at) < timedelta(minutes=_REGEN_MINUTES)
            )
            if unchanged and fresh:
                return None  # watermark no-op — idle cycles stay cheap

        model = build_model(state_dir)
        page = render_html(model)
        out_dir.mkdir(parents=True, exist_ok=True)
        index_path.write_text(page, encoding="utf-8")
        _write_json(
            wm_path,
            {
                "schema_version": _WATERMARK_SCHEMA,
                "generated_at_utc": _iso(now),
                "ledger_size": size,
                "ledger_mtime": mtime,
                "n_events": model.get("window", {}).get("n_events", 0),
            },
        )
        return index_path
    except Exception:
        return None
