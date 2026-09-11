from __future__ import annotations

import gzip
import json
from pathlib import Path

from nanobot.runtime import knowledge_curator as curator


def _write_archive(state: Path, cycle_id: str, *, day: str = "2026-09-10") -> None:
    ledger = state / "ledger"
    ledger.mkdir(parents=True, exist_ok=True)
    with gzip.open(ledger / f"cycles-{day}.jsonl.gz", "wt", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "phase": "outcome",
            "cycle_id": cycle_id,
            "outcome": "success",
            "ts": f"{day}T01:00:00Z",
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
    assert source in {"ledger_archive", "ledger_window"}

    text = curator._read_evidence_source_text(tmp_path / "workspace", cycle_id, state)
    assert "archived cycle evidence" in text


def test_cycle_reference_absent_from_retention_is_distinguishable(tmp_path: Path):
    state = tmp_path / "state"

    reason, source = curator._resolve_evidence_ref(
        "cycle-never-retained", tmp_path / "workspace", set(), state
    )

    assert source is None
    assert reason is not None
    assert "not in retained ledger" in reason or "cycle_id lookup unavailable" in reason
    assert reason != "cycle_id not in ledger tail: cycle-never-retained"


def test_archive_lookup_is_not_vacuous_against_active_tail_only_copy(tmp_path: Path):
    source = Path(curator.__file__).read_text(encoding="utf-8")
    assert "ledger_window" in source
    broken = source.replace(
        'ledger_window(\n            Path(state_dir),',
        'ledger_window(\n            Path(state_dir),',
        1,
    )
    broken = broken.replace(
        'since_ts=(now - timedelta(days=7)).isoformat().replace("+00:00", "Z"),',
        'since_ts=(now - timedelta(days=0)).isoformat().replace("+00:00", "Z"),',
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
