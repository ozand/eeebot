import json


def test_strong_reflection_artifact_writer(tmp_path):
    from app.main import _write_strong_reflection_artifact

    path = _write_strong_reflection_artifact(state_root=tmp_path / 'state', workspace=tmp_path, summary='Self-evolving cycle PASS — evidence=e1')
    payload = json.loads(path.read_text(encoding='utf-8'))
    assert payload['schema_version'] == 'strong-reflection-run-v1'
    assert payload['mode'] == 'strong-reflection'
    assert payload['summary'].endswith('e1')
