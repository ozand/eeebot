from __future__ import annotations

import json
import subprocess
from pathlib import Path

from nanobot.runtime import experiment_ledger
from nanobot.runtime.experiment_ledger import (
    COLUMNS,
    _MAX_ROWS,
    append_experiment_result,
    ledger_path,
    read_experiment_ledger,
)


def test_missing_ledger_is_explicit_and_empty(tmp_path: Path):
    result = read_experiment_ledger(tmp_path / "state")
    assert result == {
        "status": "missing", "columns": list(COLUMNS), "rows": [], "truncated": False,
    }


def test_valid_empty_ledger_is_distinct(tmp_path: Path):
    state = tmp_path / "state"
    path = ledger_path(state)
    path.parent.mkdir(parents=True)
    path.write_text("", encoding="utf-8")
    assert read_experiment_ledger(state)["status"] == "empty"
    assert read_experiment_ledger(state)["rows"] == []


def test_corrupt_ledger_is_unavailable(tmp_path: Path):
    state = tmp_path / "state"
    path = ledger_path(state)
    path.parent.mkdir(parents=True)
    path.write_text("not json\n", encoding="utf-8")
    result = read_experiment_ledger(state)
    assert result["status"] == "unavailable"
    assert result["rows"] == []


def test_writer_reader_round_trip_preserves_exact_five_columns(tmp_path: Path):
    state = tmp_path / "state"
    assert append_experiment_result(
        state,
        matrix_cell="model=un/qwen;prompt=v1",
        arm="control",
        what_changed="baseline prompt",
        measured_delta="+0.12",
        verdict="keep",
    )
    result = read_experiment_ledger(state)
    assert result["status"] == "present"
    assert result["columns"] == list(COLUMNS)
    assert result["rows"] == [{
        "matrix_cell": "model=un/qwen;prompt=v1",
        "arm": "control",
        "what_changed": "baseline prompt",
        "measured_delta": "+0.12",
        "verdict": "keep",
    }]
    assert set(result["rows"][0]) == set(COLUMNS)


def test_failed_write_is_fail_open(tmp_path: Path):
    state = tmp_path / "state"
    path = ledger_path(state)
    path.parent.mkdir(parents=True)
    path.mkdir()
    assert append_experiment_result(
        state, matrix_cell="x", arm="a", what_changed="c", measured_delta="0", verdict="discard"
    ) is False


def test_key_order_is_not_a_schema_violation(tmp_path: Path):
    """A valid row whose keys arrive in another order must still read."""
    state = tmp_path / "state"
    path = ledger_path(state)
    path.parent.mkdir(parents=True)
    reordered = {
        "arm": "b", "matrix_cell": "c", "what_changed": "w",
        "measured_delta": "0", "verdict": "keep",
    }
    assert tuple(reordered) != COLUMNS, "fixture must not be in declared order"
    path.write_text(json.dumps(reordered) + "\n", encoding="utf-8")
    result = read_experiment_ledger(state)
    assert result["status"] == "present"
    assert result["rows"] == [reordered]


def test_row_cap_truncates_and_says_so_instead_of_blanking(tmp_path: Path):
    """The cap must bound the read, not turn the ledger unreadable.

    An append-only ledger crosses its cap with age. Returning zero rows there
    would make the artifact permanently invisible with nothing pruning it.
    """
    state = tmp_path / "state"
    path = ledger_path(state)
    path.parent.mkdir(parents=True)
    row = {name: "x" for name in COLUMNS}
    path.write_text(
        "".join(json.dumps(row) + "\n" for _ in range(_MAX_ROWS + 25)), encoding="utf-8"
    )
    result = read_experiment_ledger(state)
    assert result["status"] == "present"
    assert result["truncated"] is True
    assert len(result["rows"]) == _MAX_ROWS


def test_ledger_survives_the_real_cycle_reset(tmp_path: Path):
    """Run the reset the bridge actually runs, not a stand-in for it.

    The cycle reset is ``git clean -fd`` on the instance repo checkout
    (``bridge.py`` lines 785, 1335, 1343). The experiment ledger lives under
    ``state_dir``, a tree outside that checkout — which is the whole reason it
    survives. Truncating a cycle-ledger file by hand would exercise neither
    the command nor the boundary.
    """
    repo = tmp_path / "instance-repo"
    repo.mkdir()
    git = ["git", "-C", str(repo)]
    subprocess.run([*git, "init", "-q", "-b", "main"], check=True, capture_output=True)
    (repo / "tracked.txt").write_text("kept\n", encoding="utf-8")
    subprocess.run([*git, "add", "tracked.txt"], check=True, capture_output=True)
    subprocess.run(
        [*git, "-c", "user.email=t@t", "-c", "user.name=T", "commit", "-qm", "init"],
        check=True, capture_output=True,
    )

    state = tmp_path / "state"  # sibling of the checkout, as on the host
    assert state.resolve() not in repo.resolve().parents
    assert append_experiment_result(
        state, matrix_cell="cell", arm="variant", what_changed="change",
        measured_delta="-1", verdict="discard",
    )
    # An untracked file inside the checkout proves the clean really ran.
    (repo / "scratch.txt").write_text("swept\n", encoding="utf-8")

    subprocess.run([*git, "clean", "-fd"], check=True, capture_output=True)

    assert not (repo / "scratch.txt").exists(), "clean did not run"
    result = read_experiment_ledger(state)
    assert result["status"] == "present"
    assert result["rows"][0]["verdict"] == "discard"


def test_no_runtime_or_llm_dependency():
    source = Path(experiment_ledger.__file__).read_text(encoding="utf-8")
    assert "llm" not in source.lower()
    assert "cycle_ledger" not in source
