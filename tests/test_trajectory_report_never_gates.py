"""ADR-011 rule 3 (#1825): trajectory concentration figures are report
only. A number that says "this shape/target is over-represented" wired
into task selection is a target the loop can satisfy by varying SHAPE
without varying VALUE -- the exact failure this rule exists to prevent.

Enforced structurally, the same way ADR-028 rule 4 ("the diary never
enters the prompt") is enforced in ``tests/test_day_diary.py`` and
``tests/test_context_prompt_fit.py``: grep the source of every module
that could plumb a number into task selection for this module's own
name, and fail if any of them import it. The ONLY sanctioned caller is
``nanobot.runtime.bridge``'s post-cycle report-writing step (already
covered by ``tests/test_bridge_post_cycle_censuses.py``), which
publishes the report for the dashboard and nothing else.
"""
from __future__ import annotations

from pathlib import Path

import nanobot.agent.context as context_module
import nanobot.runtime.demand as demand_module
import nanobot.runtime.demand_ranking as demand_ranking_module
import nanobot.runtime.llm_proposer as llm_proposer_module
import nanobot.runtime.scorecard as scorecard_module

#: Every module that could plumb a concentration figure into task
#: selection: demand collection, the proposer, queue ranking, the
#: scorecard, and the prompt-assembly path that selects what the loop
#: sees. bridge.py is deliberately EXCLUDED -- it is the one sanctioned
#: report-writing caller, covered by test_bridge_post_cycle_censuses.py.
_GUARDED_MODULES = (
    demand_module,
    llm_proposer_module,
    demand_ranking_module,
    scorecard_module,
    context_module,
)


def test_no_task_selection_module_imports_the_trajectory_report():
    for module in _GUARDED_MODULES:
        src = Path(str(module.__file__)).read_text(encoding="utf-8")
        assert "trajectory" not in src.lower(), (
            f"{module.__file__} must not import or reference nanobot.runtime.trajectory "
            "(ADR-011 rule 3: report only, never a task-selection input)"
        )


def test_task_shape_classifier_is_also_absent_from_every_guarded_module():
    """The classifier itself (task_shape.py) is a second module the same
    rule covers -- a shape figure reaching selection through the
    classifier directly, bypassing trajectory.py, is the same failure."""
    for module in _GUARDED_MODULES:
        src = Path(str(module.__file__)).read_text(encoding="utf-8")
        assert "task_shape" not in src.lower(), (
            f"{module.__file__} must not import nanobot.runtime.task_shape "
            "(ADR-011 rule 3: report only, never a task-selection input)"
        )
