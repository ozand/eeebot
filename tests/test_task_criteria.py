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
    # Empty evaluation_target is NEVER falsifiable
    assert is_structurally_falsifiable("") is False
    assert is_structurally_falsifiable("   ") is False

    # Paths loop can commit to (tests/, scripts/) are self-referential -> NOT falsifiable
    assert is_structurally_falsifiable("tests/test_x.py") is False
    assert is_structurally_falsifiable("scripts/foo.py") is False
    assert is_structurally_falsifiable("./scripts/../tests/test_foo.py") is False
    assert is_structurally_falsifiable(r"tests\sub\test_bar.py") is False

    # Paths loop CANNOT commit to are structurally falsifiable:
    # 1. Forbidden paths under MUTATION_POLICY (state/, ops/)
    assert is_structurally_falsifiable("state/runs.jsonl") is True
    assert is_structurally_falsifiable("ops/dashboard.py") is True
    assert is_structurally_falsifiable("./state/../state/runs.jsonl") is True

    # 2. Release-owned immutable files (goals.md, IDENTITY.md, etc.)
    assert is_structurally_falsifiable("goals.md") is True
    assert is_structurally_falsifiable("IDENTITY.md") is True

    # 3. Harness metrics / external evaluator metrics (not in commit prefixes)
    assert is_structurally_falsifiable("repeat_failure_rate") is True
    assert is_structurally_falsifiable("compile_clean_ratio") is True



def test_sanitize_criteria():
    target = "scripts/eval_helper.py"

    # Valid metric check with external harness metric
    valid_metric = {
        "metric": "compile_clean_ratio",
        "eval_kind": "metric",
        "description": "compile clean ratio improves",
        "target": "compile_clean_ratio",
    }
    res = sanitize_criteria(valid_metric, target_path=target)
    assert res is not None
    assert res["metric"] == "compile_clean_ratio"
    assert res["eval_kind"] == "metric"
    assert res["target"] == "compile_clean_ratio"

    # Valid external state/ invariant target
    valid_state = {
        "description": "runs ledger records integration",
        "eval_kind": "file_exists",
        "target": "state/runs.jsonl",
    }
    assert sanitize_criteria(valid_state, target_path=target) is not None

    # Invalid metric name rejected
    invalid_metric = {
        "metric": "unregistered_custom_metric",
        "description": "should fail",
        "target": "state/runs.jsonl",
    }
    assert sanitize_criteria(invalid_metric, target_path=target) is None

    # Self-referential target rejected (loop-writable tests/ or scripts/)
    loop_writable = {
        "description": "test file passes",
        "eval_kind": "file_exists",
        "target": "tests/test_eval.py",
    }
    assert sanitize_criteria(loop_writable, target_path=target) is None

    # Empty target rejected (#1859 rule 1)
    empty_target = {
        "description": "no target",
        "eval_kind": "metric",
        "target": "",
    }
    assert sanitize_criteria(empty_target, target_path=target) is None

    # Empty description rejected
    no_desc = {
        "metric": "tokens_per_integration",
        "description": "   ",
        "target": "tokens_per_integration",
    }
    assert sanitize_criteria(no_desc, target_path=target) is None

