from __future__ import annotations

from nanobot.runtime.autoevolve import derive_selfevo_branch_name, ensure_selfevo_pr


def test_derive_selfevo_branch_name():
    assert derive_selfevo_branch_name(issue_number=12, source_task_id='analyze-last-failed-candidate').startswith('fix/issue-12-analyze-last-failed-candidate')
    assert derive_selfevo_branch_name(issue_number=7, source_task_id='record-reward').startswith('chore/issue-7-record-reward')


def test_ensure_selfevo_pr_metadata_shape():
    data = ensure_selfevo_pr(repo='ozand/eeebot-self-evolving', head_branch='fix/issue-1-test', base_branch='main', title='Test PR', body='Body', dry_run=True)
    assert data['head_branch'] == 'fix/issue-1-test'
    assert data['base_branch'] == 'main'
    assert data['dry_run'] is True
