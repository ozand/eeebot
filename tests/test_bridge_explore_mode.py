import json

from nanobot.runtime.bridge import _parse_explore_mode
from nanobot.runtime.session_clock import compute_explore_cycle_max_call_gap


def test_explore_cycle_call_gap_uses_base_id_for_planning_and_candidates(tmp_path, monkeypatch):
    """Explore candidate IDs are suffixed, but bridge LLM calls use base cycle_id."""
    from nanobot.observability import llm_telemetry

    call_dir = tmp_path / "calls"
    call_dir.mkdir()
    monkeypatch.setenv("LLM_CALLS_DIR", str(call_dir))
    base_cycle_id = "cycle-explore"
    records = [
        {"ts": "2026-09-26T10:00:00Z", "cycle_id": base_cycle_id},
        {"ts": "2026-09-26T10:00:10Z", "cycle_id": base_cycle_id},
        {"ts": "2026-09-26T10:01:10Z", "cycle_id": base_cycle_id},
        {"ts": "2026-09-26T10:01:20Z", "cycle_id": base_cycle_id},
    ]
    with (call_dir / "calls.jsonl").open("w", encoding="utf-8") as calls:
        for record in records:
            calls.write(json.dumps(record) + "\n")

    # Candidate-local fallback gaps (10s each) omit the 60s planning→candidate gap.
    assert llm_telemetry._llm_calls_dir() == call_dir
    assert compute_explore_cycle_max_call_gap(
        tmp_path / "state", base_cycle_id, [10.0, 10.0]
    ) == 60.0
    assert compute_explore_cycle_max_call_gap(
        tmp_path / "state", base_cycle_id, [75.0, 10.0]
    ) == 75.0


def test_parse_explore_mode():

    req = {"task": "fix something"}
    assert _parse_explore_mode(req) == (1, "")

    req = {"task": "explore: 3\nfix this"}
    assert _parse_explore_mode(req) == (3, "")

    req = {"task": "explore: 2\nmeasurement: causal_gap"}
    assert _parse_explore_mode(req) == (2, "causal_gap")
