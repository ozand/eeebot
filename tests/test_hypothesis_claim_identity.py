"""Claim-level identity contract for the live #1345 hypothesis corpus."""
from __future__ import annotations

import json
from pathlib import Path

from nanobot.runtime import hypothesis_backlog
from nanobot.runtime.cycle_ledger import read_events

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "hypotheses-durable-live-2026-09-05.json"

HARNESS_IDS = {"hyp-0010", "hyp-0013", "hyp-0016", "hyp-0020", "hyp-0021"}
HOST_METRICS_IDS = {"hyp-0008", "hyp-0012", "hyp-0015", "hyp-0019", "hyp-0022"}
DEDUP_TITLES = {
    "Dedup-Throttled Demand Selection to Lower Wasted Attempt Rate",
    "Pre-Proposal Demand Deduplication to Dissolve Repeat Failure Waste",
    "Pre-Proposer Existence Gating to Dissolve Self-Dedup Churn",
    "Saturation Gating for High-Deduplication Demand IDs",
    "Pre-filtering exhausted demand IDs in candidate selection resolves proposer starvation",
    "Inject recent git commit subjects into proposer context to dissolve self-dedup rejects",
}
DISTINCT_TITLES = {
    "Atomic self-testing script tasks restore executor commit yield",
    "Pre-Proposer Suppression of Plateaued AST Refactors",
    "Turn-bounded direct-edit gate dissolves inspection-versus-completion budget trade-off",
    "Pre-seed standard library module cache to dissolve search turn-burn in subagent runs",
}


def _fixture_entries() -> list[dict]:
    data = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert data["updated_at"] == "2026-09-05T00:00:39.954771+00:00"
    assert len(data["entries"]) == 20
    return data["entries"]


def _entries_with_ids(entries: list[dict], ids: set[str], titles: set[str] | None = None) -> list[dict]:
    return [
        entry
        for entry in entries
        if entry.get("hypothesis_id") in ids
        and (titles is None or entry.get("title") in titles)
    ]


def test_live_fixture_claim_groups_are_specific_not_universal() -> None:
    entries = _fixture_entries()
    # hyp-0021 and hyp-0022 are duplicated FIFO ids in the live source, so
    # titles disambiguate the groups without rewriting the read-only fixture.
    harness = _entries_with_ids(
        entries,
        HARNESS_IDS,
        {"Harness Execution Coupling to Dissolve Unconfirmed Integration Deficit",
         "Harness Execution Coupling for Script Confirmations",
         "Harness Auto-Execution Coupling for Standalone Helpers",
         "Automated Harness Coupling for Standalone Validator Scripts",
         "exercise-unconfirmed-scripts-via-test-harness"},
    )
    host_metrics = _entries_with_ids(
        entries,
        HOST_METRICS_IDS,
        {"Validator Parent Heartbeat Touch to Clear Stale Host Metrics",
         "Scheduled Host Metrics Sampling to Eliminate Stale Feed Gap",
         "Runner Heartbeat Telemetry Touch for Host Metrics Feed",
         "Host metrics telemetry runner execution resolves stale feed gap",
         "suppress-in-tree-proposals-for-host-metrics-staleness"},
    )
    dedup = [entry for entry in entries if entry.get("title") in DEDUP_TITLES]
    distinct = [entry for entry in entries if entry.get("title") in DISTINCT_TITLES]

    assert len(harness) == 5
    assert len(host_metrics) == 5
    assert len(dedup) == 6
    assert len(distinct) == 4

    harness_keys = {hypothesis_backlog.hypothesis_identity_key(entry) for entry in harness}
    host_metrics_keys = {hypothesis_backlog.hypothesis_identity_key(entry) for entry in host_metrics}
    dedup_keys = {hypothesis_backlog.hypothesis_identity_key(entry) for entry in dedup}
    distinct_keys = {hypothesis_backlog.hypothesis_identity_key(entry) for entry in distinct}

    assert len(harness_keys) == 1
    assert len(host_metrics_keys) == 1
    # Five proposals change pre-proposer demand selection; the sixth supplies
    # recent commit subjects to the proposer prompt, so its mechanism is distinct.
    assert len(dedup_keys) == 2
    assert len(distinct_keys) == 4
    assert len(harness_keys | host_metrics_keys | dedup_keys | distinct_keys) == 8


def test_missing_structured_fields_fall_back_to_title_identity() -> None:
    first = {"title": "A custom title without structured claim fields"}
    renamed = {"title": "Renamed custom title without structured claim fields"}

    assert hypothesis_backlog.hypothesis_identity_key({}) == ""
    assert hypothesis_backlog.hypothesis_identity_key(first) == "title-a-custom-title-without-structured-claim-fields"
    assert hypothesis_backlog.hypothesis_identity_key(first) != hypothesis_backlog.hypothesis_identity_key(renamed)


def test_collision_strengthens_existing_record_and_records_both_ids(tmp_path: Path) -> None:
    existing = {
        "hypothesis_id": "hyp-orig",
        "title": "Harness Execution Coupling for Script Confirmations",
        "hypothesis": "Unconfirmed standalone scripts persist because standard verification does not execute them.",
        "action": "Register standalone validator scripts in the test harness.",
        "priority": "medium",
        "evidence": "confirmed_ratio=0.44",
    }
    restatement = {
        "hypothesis_id": "hyp-restatement",
        "title": "Automated Harness Coupling for Standalone Validator Scripts",
        "hypothesis": "Unconfirmed standalone scripts persist because harness verification does not run them.",
        "action": "Register validator scripts in the automated test harness.",
        "priority": "high",
        "evidence": "five validator scripts lack execution evidence",
    }

    assert hypothesis_backlog.append_hypotheses(tmp_path, [existing]) == 1
    assert hypothesis_backlog.append_hypotheses(tmp_path, [restatement]) == 0

    durable = json.loads(
        (tmp_path / "hypotheses" / "durable.json").read_text(encoding="utf-8")
    )
    assert len(durable["entries"]) == 1
    strengthened = durable["entries"][0]
    assert strengthened["hypothesis_id"] == "hyp-orig"
    assert strengthened["seen_count"] == 2
    assert strengthened["priority"] == "high"
    assert strengthened["evidence"] == (
        "confirmed_ratio=0.44\n"
        "five validator scripts lack execution evidence"
    )

    collisions = [
        row
        for row in read_events(tmp_path)
        if row.get("phase") == "hypothesis" and row.get("reason") == "claim_collision"
    ]
    assert len(collisions) == 1
    assert collisions[0]["existing_hypothesis_id"] == "hyp-orig"
    assert collisions[0]["incoming_hypothesis_id"] == "hyp-restatement"
