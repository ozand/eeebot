from __future__ import annotations

import json
import subprocess
from pathlib import Path

from nanobot.runtime.autoevolve import (
    create_self_mutation_request,
    write_guarded_evolution_state,
)
from test_autoevolve import _init_repo


def test_create_self_mutation_request_writes_latest_request(tmp_path: Path):
    repo = tmp_path / 'repo'
    workspace = tmp_path / 'workspace'
    _init_repo(repo)
    request = create_self_mutation_request(
        workspace=workspace,
        objective='repair autonomous loop',
        source_task_id='analyze-last-failed-candidate',
        commit_message='autoevolve: repair loop',
        goal_id='goal-bootstrap',
        current_task_id='analyze-last-failed-candidate',
        selected_task_id='analyze-last-failed-candidate',
        selected_task_title='Analyze the last failed self-evolution candidate before retrying mutation',
        selection_source='generated_from_failure_learning',
        selected_tasks='Analyze the last failed self-evolution candidate before retrying mutation [task_id=analyze-last-failed-candidate]',
        feedback_decision={'selected_task_id': 'analyze-last-failed-candidate'},
        mutation_lane={'lane': 'read_only'},
        selfevo_issue={'number': 1, 'title': 'repair autonomous loop', 'url': 'https://github.com/ozand/eeebot-self-evolving/issues/1'},
        selfevo_branch='fix/issue-1-analyze-last-failed-candidate',
    )
    latest = json.loads((workspace / 'state' / 'self_evolution' / 'requests' / 'latest.json').read_text())
    assert latest['request_id'] == request['request_id']
    assert latest['objective'] == 'repair autonomous loop'
    assert latest['source_task_id'] == 'analyze-last-failed-candidate'
    assert latest['goal_id'] == 'goal-bootstrap'
    assert latest['selected_task_id'] == 'analyze-last-failed-candidate'
    assert latest['selection_source'] == 'generated_from_failure_learning'
    assert latest['selfevo_issue']['number'] == 1
    assert latest['selfevo_branch'] == 'fix/issue-1-analyze-last-failed-candidate'


def test_write_guarded_evolution_state_aggregates_latest_artifacts(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    state = workspace / 'state' / 'self_evolution'
    (state / 'candidates').mkdir(parents=True, exist_ok=True)
    (state / 'runtime').mkdir(parents=True, exist_ok=True)
    (state / 'failure_learning').mkdir(parents=True, exist_ok=True)
    (state / 'requests').mkdir(parents=True, exist_ok=True)

    (state / 'candidates' / 'latest.json').write_text(json.dumps({'candidate_id': 'candidate-1'}), encoding='utf-8')
    (state / 'runtime' / 'latest_apply.json').write_text(json.dumps({'release_dir': '/tmp/release-1'}), encoding='utf-8')
    (state / 'runtime' / 'latest_rollback.json').write_text(json.dumps({'rolled_back_to_release_dir': '/tmp/release-0'}), encoding='utf-8')
    (state / 'failure_learning' / 'latest.json').write_text(json.dumps({'candidate_id': 'candidate-bad', 'learning_summary': 'repair first'}), encoding='utf-8')
    (state / 'requests' / 'latest.json').write_text(json.dumps({'request_id': 'request-1', 'objective': 'repair'}), encoding='utf-8')

    payload = write_guarded_evolution_state(workspace=workspace)
    latest = json.loads((workspace / 'state' / 'self_evolution' / 'current_state.json').read_text())
    assert payload['current_candidate']['candidate_id'] == 'candidate-1'
    assert payload['last_apply']['release_dir'] == '/tmp/release-1'
    assert payload['last_rollback']['rolled_back_to_release_dir'] == '/tmp/release-0'
    assert payload['last_failure_learning']['candidate_id'] == 'candidate-bad'
    assert payload['latest_request']['request_id'] == 'request-1'
    assert latest['current_candidate']['candidate_id'] == 'candidate-1'


def test_write_guarded_evolution_state_records_observed_product_head_without_rewriting_candidate(tmp_path: Path):
    repo = tmp_path / 'repo'
    _init_repo(repo)
    workspace = repo / 'workspace'
    state = workspace / 'state' / 'self_evolution'
    (state / 'candidates').mkdir(parents=True, exist_ok=True)
    stale_commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repo, text=True).strip()
    (repo / 'README.md').write_text('advanced product head\n', encoding='utf-8')
    subprocess.run(['git', 'commit', '-q', '-am', 'advance product head'], cwd=repo, check=True)
    product_head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repo, text=True).strip()
    (state / 'candidates' / 'latest.json').write_text(json.dumps({'candidate_id': 'candidate-1', 'commit': stale_commit}), encoding='utf-8')

    payload = write_guarded_evolution_state(workspace=workspace)

    assert payload['current_candidate']['commit'] == stale_commit
    assert payload['observed_product_head']['commit'] == product_head
    assert payload['observed_product_head']['source'] == 'git_rev_parse_head'
    assert payload['product_head'] == product_head
