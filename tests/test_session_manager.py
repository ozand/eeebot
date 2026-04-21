import json
from pathlib import Path

from nanobot.session.manager import Session, SessionManager


def test_session_manager_repairs_corrupt_jsonl(tmp_path):
    mgr = SessionManager(tmp_path)
    path = mgr._get_session_path('cli:test')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '\n'.join([
            json.dumps({"_type": "metadata", "key": "cli:test", "created_at": "2026-04-21T10:00:00", "updated_at": "2026-04-21T10:00:00", "metadata": {}, "last_consolidated": 0}),
            json.dumps({"role": "user", "content": "hello"}),
            '{bad json',
            json.dumps({"role": "assistant", "content": "world"}),
        ]),
        encoding='utf-8',
    )

    session = mgr.get_or_create('cli:test')
    assert [m['content'] for m in session.messages] == ['hello', 'world']


def test_session_manager_save_is_atomic(tmp_path):
    mgr = SessionManager(tmp_path)
    session = Session(key='cli:test')
    session.add_message('user', 'hello')
    mgr.save(session)
    path = mgr._get_session_path('cli:test')
    tmp_file = path.with_suffix('.jsonl.tmp')
    assert path.exists()
    assert not tmp_file.exists()
