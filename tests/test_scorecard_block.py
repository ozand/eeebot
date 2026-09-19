"""#1766 (ADR-023): the loop's own last-N-days state block.

Two concerns:

- **Rendering** (``ContextBuilder._load_scorecard_block``): fail-open shape
  (missing/unreadable/malformed/incomplete all render the same
  ``[missing: scorecard]`` marker, never zeros, never a partial row),
  formatting, freshness disclosure, ledger telemetry entry, cap.
- **Provenance** (ADR-023's actual safety test): a test that proves —
  against the real production function, not by trusting its signature —
  that nothing the loop can commit moves any of the figures the block
  shows. ADR-023's own finding is that file ownership is not a valid test
  on this host (state/ and the instance repo share a uid); the real test is
  whether a figure's derivation ever reads the instance repository at all.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from nanobot.agent.context import ContextBuilder
from nanobot.runtime import scorecard

NOW = datetime(2026, 9, 19, 10, 0, 0, tzinfo=timezone.utc)


def _iso(minutes_ago: int = 0) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).isoformat().replace("+00:00", "Z")


def _write_ledger(state_dir: Path, rows: list[dict]) -> None:
    ledger_dir = state_dir / "ledger"
    ledger_dir.mkdir(parents=True, exist_ok=True)
    (ledger_dir / "cycles.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8",
    )


def _write_telemetry(state_dir: Path, day: str, rows: list[dict]) -> None:
    calls_dir = state_dir / "llm_calls"
    calls_dir.mkdir(parents=True, exist_ok=True)
    (calls_dir / f"{day}.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8",
    )


def _populated_state_dir(tmp_path: Path) -> Path:
    """A state_dir with enough ledger/telemetry/completed activity that the
    four figures are genuinely non-trivial (not all zero/None) -- so the
    provenance test below is comparing real numbers, not two empty ones."""
    state_dir = tmp_path / "state"
    _write_ledger(state_dir, [
        {"phase": "proposed", "cycle_id": "c1", "demand_id": "d1", "ts": _iso(50)},
        {"phase": "outcome", "cycle_id": "c1", "outcome": "success",
         "files_changed": ["scripts/a.py"], "ts": _iso(49)},
        {"phase": "proposed", "cycle_id": "c2", "demand_id": "d2", "ts": _iso(40)},
        {"phase": "outcome", "cycle_id": "c2", "outcome": "success",
         "files_changed": ["scripts/b.py"], "ts": _iso(39)},
        {"phase": "proposed", "cycle_id": "c3", "ts": _iso(30)},
        {"phase": "outcome", "cycle_id": "c3", "outcome": "failed", "ts": _iso(29)},
        {"phase": "proposer_reject", "reason": "self_dedup", "ts": _iso(20)},
    ])
    _write_telemetry(state_dir, NOW.strftime("%Y-%m-%d"), [
        {"prompt_tokens": 100, "completion_tokens": 50},
        {"prompt_tokens": 200, "completion_tokens": 80},
    ])
    (state_dir / "demand").mkdir(parents=True, exist_ok=True)
    (state_dir / "demand" / "completed.json").write_text(json.dumps({
        "entries": {"c1": {"cycle_id": "c1", "confirmed": True, "signal": "reference"}},
    }), encoding="utf-8")
    return state_dir


_SAFE_FIELDS = (
    ("loop", "integrations"), ("loop", "confirmed_integrations"),
    ("loop", "confirmed_integration_ratio"), ("loop", "attempts"),
    ("loop", "wasted_attempts"), ("cost", "calls_per_integration"),
)


# ─── provenance (ADR-023) ─────────────────────────────────────────────────────


def test_the_safe_figures_are_identical_regardless_of_instance_repo_content(tmp_path):
    """ADR-023: the safety test is provenance of the number, not ownership
    of the file. ``_loop_section``/``_cost_section`` never take the
    instance repo as an input -- proven here, not merely read off their
    signatures, by computing the scorecard twice against the IDENTICAL
    state_dir but two adversarially different instance repos and asserting
    every figure the block shows is byte-identical.

    ``zero_static_consumers`` (the one figure #1766's draft wanted and
    dropped) DOES differ between the two repos -- included below to prove
    the adversarial repo is a genuine adversarial input, not a no-op, and
    that the boundary excludes exactly the one figure ADR-023 names."""
    state_dir = _populated_state_dir(tmp_path)

    empty_repo = tmp_path / "repo_empty"
    (empty_repo / "scripts").mkdir(parents=True)

    # A repo the loop could plausibly have committed: two scripts, one
    # consumed by the other, so zero_static_consumers is genuinely different
    # (1, not 0) from the empty repo's baseline.
    adversarial_repo = tmp_path / "repo_adversarial"
    (adversarial_repo / "scripts").mkdir(parents=True)
    (adversarial_repo / "scripts" / "used.py").write_text("x = 1\n", encoding="utf-8")
    (adversarial_repo / "scripts" / "consumer.py").write_text("import used\n", encoding="utf-8")
    (adversarial_repo / "surfaces").mkdir(parents=True)
    (adversarial_repo / "surfaces" / "extra.py").write_text("y = 2\n", encoding="utf-8")

    snap_empty = scorecard.compute_scorecard(state_dir, empty_repo, now=NOW, force=True)
    snap_adversarial = scorecard.compute_scorecard(state_dir, adversarial_repo, now=NOW, force=True)

    for section, key in _SAFE_FIELDS:
        value_empty = snap_empty[section][key]
        value_adversarial = snap_adversarial[section][key]
        assert value_empty == value_adversarial, (
            f"{section}.{key} moved from {value_empty!r} to {value_adversarial!r} "
            "when only the instance repo changed -- not safe to present as "
            "harness-owned fact"
        )

    zero_consumers_empty = snap_empty["quality"]["retention_cost"]["zero_static_consumers"]
    zero_consumers_adversarial = snap_adversarial["quality"]["retention_cost"]["zero_static_consumers"]
    assert zero_consumers_empty != zero_consumers_adversarial, (
        "the adversarial repo must actually move the dropped figure, or this "
        "test proves nothing about the boundary"
    )


def test_no_state_dir_input_reaches_the_four_figures_by_signature():
    """A second, cheap line of defense next to the functional proof above:
    the functions that compute the four figures do not even accept a repo
    argument, so there is no parameter through which a loop-writable path
    could reach them."""
    import inspect

    loop_params = set(inspect.signature(scorecard._loop_section).parameters)
    cost_params = set(inspect.signature(scorecard._cost_section).parameters)
    assert not any("repo" in name for name in loop_params), loop_params
    assert not any("repo" in name for name in cost_params), cost_params


# ─── rendering: fail-open shape ──────────────────────────────────────────────


def _builder_with_state(tmp_path: Path, data: object) -> ContextBuilder:
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    state = tmp_path / "state"
    (state / "scorecard").mkdir(parents=True, exist_ok=True)
    if data is not None:
        text = data if isinstance(data, str) else json.dumps(data)
        (state / "scorecard" / "latest.json").write_text(text, encoding="utf-8")
    return ContextBuilder(ws, release_root=None, state_dir=state)


_GOOD_DATA = {
    "schema_version": "scorecard-v1",
    "computed_at_utc": "2026-09-19T10:11:14Z",
    "window_days": 7,
    "window_start_utc": "2026-09-12T10:11:14Z",
    "window_end_utc": "2026-09-19T10:06:19Z",
    "loop": {
        "integrations": 187, "confirmed_integrations": 108,
        "confirmed_integration_ratio": 0.705, "attempts": 751, "wasted_attempts": 516,
    },
    "cost": {"calls_per_integration": 36.4286},
}


def test_no_state_dir_renders_missing():
    builder = ContextBuilder(Path("."), release_root=None, state_dir=None)
    assert builder._load_scorecard_block() == "[missing: scorecard]"


def test_absent_file_renders_missing(tmp_path):
    builder = _builder_with_state(tmp_path, None)
    assert builder._load_scorecard_block() == "[missing: scorecard]"


def test_malformed_json_renders_missing(tmp_path):
    builder = _builder_with_state(tmp_path, "{not json")
    assert builder._load_scorecard_block() == "[missing: scorecard]"


def test_wrong_schema_version_renders_missing(tmp_path):
    builder = _builder_with_state(tmp_path, {**_GOOD_DATA, "schema_version": "scorecard-v0"})
    assert builder._load_scorecard_block() == "[missing: scorecard]"


def test_not_a_dict_renders_missing(tmp_path):
    builder = _builder_with_state(tmp_path, [1, 2, 3])
    assert builder._load_scorecard_block() == "[missing: scorecard]"


def test_missing_computed_at_renders_missing(tmp_path):
    data = dict(_GOOD_DATA)
    del data["computed_at_utc"]
    builder = _builder_with_state(tmp_path, data)
    assert builder._load_scorecard_block() == "[missing: scorecard]"


def test_missing_loop_section_renders_missing(tmp_path):
    data = dict(_GOOD_DATA)
    del data["loop"]
    builder = _builder_with_state(tmp_path, data)
    assert builder._load_scorecard_block() == "[missing: scorecard]"


def test_null_ratio_renders_missing_never_a_partial_row(tmp_path):
    """A legitimate zero-denominator None (no confirmable integrations yet)
    still renders the whole block as missing -- never a row with one figure
    silently absent, which would read as healthy for the parts it does show."""
    data = json.loads(json.dumps(_GOOD_DATA))
    data["loop"]["confirmed_integration_ratio"] = None
    builder = _builder_with_state(tmp_path, data)
    assert builder._load_scorecard_block() == "[missing: scorecard]"


def test_non_numeric_field_renders_missing(tmp_path):
    data = json.loads(json.dumps(_GOOD_DATA))
    data["loop"]["attempts"] = "751"  # string, not a number
    builder = _builder_with_state(tmp_path, data)
    assert builder._load_scorecard_block() == "[missing: scorecard]"


def test_bool_field_is_not_treated_as_numeric(tmp_path):
    """bool is a subclass of int in Python -- must not silently pass as a
    figure (e.g. a truthy/falsy value rendered as 0 or 1 model calls)."""
    data = json.loads(json.dumps(_GOOD_DATA))
    data["cost"]["calls_per_integration"] = True
    builder = _builder_with_state(tmp_path, data)
    assert builder._load_scorecard_block() == "[missing: scorecard]"


def test_never_renders_zeros_when_the_source_is_absent(tmp_path):
    builder = _builder_with_state(tmp_path, None)
    block = builder._load_scorecard_block()
    assert "0" not in block
    assert block == "[missing: scorecard]"


# ─── rendering: healthy content ──────────────────────────────────────────────


def test_healthy_block_states_figures_window_and_computed_time(tmp_path):
    builder = _builder_with_state(tmp_path, _GOOD_DATA)
    block = builder._load_scorecard_block()

    assert block != "[missing: scorecard]"
    assert "187" in block  # integrations
    assert "108" in block  # confirmed_integrations
    assert "70%" in block  # confirmed_integration_ratio, rounded
    assert "516" in block  # wasted_attempts
    assert "751" in block  # attempts
    assert "36" in block  # calls_per_integration, rounded
    # freshness: the window and computation time are stated, not implied.
    assert "2026-09-12" in block and "2026-09-19" in block
    assert "2026-09-19T10:11:14Z" in block
    assert "7 days" in block


def test_healthy_block_is_stated_not_scored(tmp_path):
    """No target/quota/optimisation language -- the block reports state,
    the charter states goals (issue's own non-goal)."""
    builder = _builder_with_state(tmp_path, _GOOD_DATA)
    block = builder._load_scorecard_block().lower()
    for banned in ("target", "quota", "goal", "should", "must", "optimi"):
        assert banned not in block, f"{banned!r} found in the state block: {block!r}"


def test_healthy_block_is_within_the_600_char_cap(tmp_path):
    builder = _builder_with_state(tmp_path, _GOOD_DATA)
    assert len(builder._load_scorecard_block()) <= ContextBuilder._SCORECARD_BLOCK_CAP


def test_oversized_content_is_trimmed_not_overflowing(tmp_path):
    """A pathological window/id string still respects the block's own cap
    (defense in depth beyond schema validation)."""
    data = json.loads(json.dumps(_GOOD_DATA))
    data["computed_at_utc"] = "2026-09-19T10:11:14Z" + ("x" * 2000)
    builder = _builder_with_state(tmp_path, data)
    assert len(builder._load_scorecard_block()) <= ContextBuilder._SCORECARD_BLOCK_CAP


# ─── wired into the loop-profile prompt + its own ledger telemetry entry ────


def test_scorecard_block_is_present_in_loop_profile_prompt_with_its_own_ledger_entry(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    release = tmp_path / "release"
    release.mkdir()
    for name in ("IDENTITY.md", "SOUL.md", "goals.md", "USER.md", "OPERATING.md"):
        (release / name).write_text(f"{name} content", encoding="utf-8")
    (ws / "AGENTS.md").write_text("# Instance AGENTS.md", encoding="utf-8")
    state = tmp_path / "state"
    (state / "scorecard").mkdir(parents=True)
    (state / "scorecard" / "latest.json").write_text(json.dumps(_GOOD_DATA), encoding="utf-8")

    builder = ContextBuilder(ws, release_root=release, state_dir=state)
    prompt = builder.build_system_prompt(loop_profile=True)

    assert "How the last 7 days went" in prompt
    assert "scorecard" in builder.last_fit["sections"]
    assert builder.last_fit["sections"]["scorecard"] == len(builder._load_scorecard_block())
    assert builder.last_fit["sections"]["scorecard"] > 0


def test_interactive_prompt_unaffected(tmp_path):
    """Loop profile only (issue's own AC): interactive sessions never see
    the block, never carry a scorecard section key."""
    ws = tmp_path / "ws"
    ws.mkdir()
    state = tmp_path / "state"
    (state / "scorecard").mkdir(parents=True)
    (state / "scorecard" / "latest.json").write_text(json.dumps(_GOOD_DATA), encoding="utf-8")

    builder = ContextBuilder(ws, release_root=None, state_dir=state)
    prompt = builder.build_system_prompt(loop_profile=False)

    assert "How the last 7 days went" not in prompt
    assert "scorecard" not in builder.last_fit["sections"]
