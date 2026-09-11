from __future__ import annotations

import gzip
import json
from pathlib import Path

from nanobot.runtime import knowledge_curator as curator


def _write_archive(state: Path, cycle_id: str, *, day: str = "2026-09-01") -> None:
    ledger = state / "ledger"
    ledger.mkdir(parents=True, exist_ok=True)
    with gzip.open(ledger / f"cycles-{day}.jsonl.gz", "wt", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "phase": "outcome",
            "cycle_id": cycle_id,
            "outcome": "success",
            "summary": "archived cycle evidence",
        }) + "\n")


def test_cycle_reference_in_retained_archive_resolves_with_archive_source(tmp_path: Path):
    state = tmp_path / "state"
    cycle_id = "cycle-rotated"
    _write_archive(state, cycle_id)

    reason, source = curator._resolve_evidence_ref(
        cycle_id, tmp_path / "workspace", set(), state
    )

    assert reason is None
    assert source == "ledger_archive"

    text = curator._read_evidence_source_text(tmp_path / "workspace", cycle_id, state)
    assert "archived cycle evidence" in text


def test_cycle_reference_absent_from_retention_is_distinguishable(tmp_path: Path):
    state = tmp_path / "state"

    reason, source = curator._resolve_evidence_ref(
        "cycle-never-retained", tmp_path / "workspace", set(), state
    )

    assert source is None
    assert reason is not None
    assert "not in retained ledger" in reason
    assert reason != "cycle_id not in ledger tail: cycle-never-retained"


def test_archive_lookup_is_not_vacuous_against_active_tail_only_copy(tmp_path: Path):
    source = Path(curator.__file__).read_text(encoding="utf-8")
    assert "ledger_archive" in source
    broken = source.replace(
        'archives = sorted(ledger_dir.glob("cycles-*.jsonl.gz"), reverse=True)',
        'archives = []',
        1,
    )
    assert broken != source
    broken_path = tmp_path / "broken_knowledge_curator.py"
    broken_path.write_text(broken, encoding="utf-8")

    namespace = {"__file__": str(broken_path), "__name__": "broken_knowledge_curator"}
    exec(compile(broken, str(broken_path), "exec"), namespace)
    state = tmp_path / "state"
    _write_archive(state, "cycle-rotated")
    reason, source_name = namespace["_resolve_evidence_ref"](
        "cycle-rotated", tmp_path / "workspace", set(), state
    )

    assert reason is not None
    assert source_name is None
