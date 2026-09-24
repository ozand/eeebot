"""Issue #1785 labeled regression corpus and historical token-gate baseline."""
import json
from pathlib import Path

from nanobot.runtime.goal_text_utils import _title_already_done_in_git_log


FIXTURE = Path(__file__).parent / "fixtures" / "self_dedup_1785.json"


def test_1785_labeled_cases_match_recorded_gate_baseline():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    root = Path(__file__).resolve().parents[1]

    false_cases = data["false_new_file_rejections"]
    assert len(false_cases) == 13
    assert sum(case["count"] for case in false_cases) == 121
    for case in false_cases:
        assert case["label"] == "legitimate"
        assert case["path_absent_from_head"] is True
        assert not (root / case["target_path"]).exists()

    duplicates = data["duplicates"]
    assert len(duplicates) == 3
    assert sum(case["count"] for case in duplicates) == 108
    for case in duplicates:
        assert case["label"] == "duplicate"
        assert case["matched_sha"]
        assert case["commit_subject"]

    legitimate = data["legitimate_existing_file_improvements"]
    confirmed = [case for case in legitimate if case["path_existed_before_commit"]]
    assert len(confirmed) == 14
    measured = [case for case in confirmed if case.get("proposal_ts")]
    assert len(measured) == 12
    assert all(case["cycle_id"] for case in measured)
    matches = [case for case in measured if case["duplicate_gate_match"]]
    assert len(matches) == 3
    for case in matches:
        assert case["matched_prior_subject"]
        assert _title_already_done_in_git_log(
            case["task_title"], case["matched_prior_subject"]
        )
    for case in measured:
        if not case["duplicate_gate_match"]:
            assert case["matched_prior_subject"] is None
