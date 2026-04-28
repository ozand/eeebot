import json


def test_strong_reflection_artifact_writer(tmp_path):
    from app.main import _write_strong_reflection_artifact

    path = _write_strong_reflection_artifact(state_root=tmp_path / 'state', workspace=tmp_path, summary='Self-evolving cycle PASS — evidence=e1')
    payload = json.loads(path.read_text(encoding='utf-8'))
    assert payload['schema_version'] == 'strong-reflection-run-v1'
    assert payload['mode'] == 'strong-reflection'
    assert payload['summary'].endswith('e1')


def test_main_persists_strong_reflection_on_normal_cycle(tmp_path, monkeypatch, capsys):
    import app.main as main_mod

    state_root = tmp_path / 'state'
    monkeypatch.setenv('NANOBOT_RUNTIME_STATE_ROOT', str(state_root))
    monkeypatch.setenv('NANOBOT_WORKSPACE', str(tmp_path))
    async def fake_cycle(**_kwargs):
        return 'Self-evolving cycle PASS — evidence=normal'

    monkeypatch.setattr(main_mod, 'run_self_evolving_cycle', fake_cycle)
    monkeypatch.setattr(main_mod.sys, 'argv', ['app/main.py'])

    assert main_mod.main() == 0

    payload = json.loads((state_root / 'strong_reflection' / 'latest.json').read_text(encoding='utf-8'))
    assert payload['mode'] == 'strong-reflection'
    assert payload['summary'].endswith('normal')
    assert 'Strong reflection artifact persisted:' not in capsys.readouterr().out
