from nanobot.runtime.benchmark_evidence import _HARNESS_METRICS
from nanobot.runtime.task_criteria import (
    is_structurally_falsifiable,
    sanitize_criteria,
    validate_metric_reference,
)


def test_registered_metrics_validation():
    for metric in _HARNESS_METRICS:
        assert validate_metric_reference(metric) is True
    assert validate_metric_reference("fake_metric_name") is False
    assert validate_metric_reference("") is False


def test_structural_falsifiability():
    # If target path is scripts/foo.py, evaluation target cannot be scripts/foo.py
    assert is_structurally_falsifiable("scripts/foo.py", "scripts/foo.py") is False
    assert is_structurally_falsifiable("scripts/foo.py", "tests/test_foo.py") is True
    assert is_structurally_falsifiable("scripts/foo.py", "scripts/bar.py") is True
    # Case insensitivity
    assert is_structurally_falsifiable("scripts/Foo.py", "scripts/foo.py") is False


def test_sanitize_criteria():
    target = "scripts/eval_helper.py"

    # Valid metric check
    valid_metric = {
        "metric": "compile_clean_ratio",
        "eval_kind": "metric",
        "description": "compile clean ratio improves",
        "target": "tests/test_eval.py",
    }
    res = sanitize_criteria(valid_metric, target_path=target)
    assert res is not None
    assert res["metric"] == "compile_clean_ratio"
    assert res["eval_kind"] == "metric"
    assert res["target"] == "tests/test_eval.py"

    # Invalid metric name rejected
    invalid_metric = {
        "metric": "unregistered_custom_metric",
        "description": "should fail",
    }
    assert sanitize_criteria(invalid_metric, target_path=target) is None

    # Self-referential target rejected (structural falsifiability failure)
    self_referential = {
        "description": "file changed self verification",
        "eval_kind": "file_exists",
        "target": "scripts/eval_helper.py",
    }
    assert sanitize_criteria(self_referential, target_path=target) is None

    # Empty description rejected
    no_desc = {
        "metric": "tokens_per_integration",
        "description": "   ",
    }
    assert sanitize_criteria(no_desc, target_path=target) is None
