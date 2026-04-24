from __future__ import annotations

from pathlib import Path

from nanobot_ops_dashboard.storage import init_db, connect


def test_init_db_creates_tables(tmp_path: Path):
    db = tmp_path / 'test.sqlite3'
    init_db(db)
    with connect(db) as conn:
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert 'collections' in names
    assert 'events' in names
