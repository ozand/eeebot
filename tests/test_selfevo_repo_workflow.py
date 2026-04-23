from __future__ import annotations

from nanobot.runtime.autoevolve import derive_selfevo_branch_name


def test_derive_selfevo_branch_name():
    assert derive_selfevo_branch_name(issue_number=12, source_task_id='analyze-last-failed-candidate').startswith('fix/issue-12-analyze-last-failed-candidate')
    assert derive_selfevo_branch_name(issue_number=7, source_task_id='record-reward').startswith('chore/issue-7-record-reward')
