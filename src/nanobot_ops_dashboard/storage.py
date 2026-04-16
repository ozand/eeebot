from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS collections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    collected_at TEXT NOT NULL,
    source TEXT NOT NULL,
    status TEXT,
    active_goal TEXT,
    current_task TEXT,
    task_list_json TEXT,
    reward_signal TEXT,
    plan_history_json TEXT,
    approval_gate TEXT,
    gate_state TEXT,
    report_source TEXT,
    outbox_source TEXT,
    artifact_paths_json TEXT,
    promotion_summary TEXT,
    promotion_candidate_path TEXT,
    promotion_decision_record TEXT,
    promotion_accepted_record TEXT,
    raw_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    collected_at TEXT NOT NULL,
    source TEXT NOT NULL,
    event_type TEXT NOT NULL,
    identity_key TEXT NOT NULL,
    title TEXT,
    status TEXT,
    detail_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_collections_source_time ON collections(source, collected_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_source_type_time ON events(source, event_type, collected_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_events_unique_identity ON events(source, event_type, identity_key);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Path) -> None:
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)
        existing_columns = {row[1] for row in conn.execute("PRAGMA table_info(collections)")}
        for column_name, column_type in (
            ("current_task", "TEXT"),
            ("task_list_json", "TEXT"),
            ("reward_signal", "TEXT"),
            ("plan_history_json", "TEXT"),
        ):
            if column_name not in existing_columns:
                conn.execute(f"ALTER TABLE collections ADD COLUMN {column_name} {column_type}")
        conn.commit()


def insert_collection(db_path: Path, payload: dict[str, Any]) -> int:
    with connect(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO collections (
                collected_at, source, status, active_goal, current_task, task_list_json, reward_signal, plan_history_json, approval_gate, gate_state,
                report_source, outbox_source, artifact_paths_json, promotion_summary,
                promotion_candidate_path, promotion_decision_record, promotion_accepted_record,
                raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload.get("collected_at"),
                payload.get("source"),
                payload.get("status"),
                payload.get("active_goal"),
                payload.get("current_task"),
                payload.get("task_list_json"),
                payload.get("reward_signal"),
                payload.get("plan_history_json"),
                payload.get("approval_gate"),
                payload.get("gate_state"),
                payload.get("report_source"),
                payload.get("outbox_source"),
                payload.get("artifact_paths_json"),
                payload.get("promotion_summary"),
                payload.get("promotion_candidate_path"),
                payload.get("promotion_decision_record"),
                payload.get("promotion_accepted_record"),
                payload.get("raw_json"),
            ),
        )
        conn.commit()
        return int(cur.lastrowid)


def upsert_event(db_path: Path, event: dict[str, Any]) -> None:
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO events (collected_at, source, event_type, identity_key, title, status, detail_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source, event_type, identity_key)
            DO UPDATE SET
              collected_at=excluded.collected_at,
              title=excluded.title,
              status=excluded.status,
              detail_json=excluded.detail_json
            """,
            (
                event["collected_at"],
                event["source"],
                event["event_type"],
                event["identity_key"],
                event.get("title"),
                event.get("status"),
                event["detail_json"],
            ),
        )
        conn.commit()


def fetch_latest_collections(db_path: Path, source: str, limit: int = 50) -> list[sqlite3.Row]:
    with connect(db_path) as conn:
        return list(conn.execute(
            "SELECT * FROM collections WHERE source=? ORDER BY collected_at DESC LIMIT ?",
            (source, limit),
        ))


def count_collections(db_path: Path, source: str | None = None) -> int:
    query = 'SELECT COUNT(*) FROM collections'
    params: tuple[Any, ...] = ()
    if source is not None:
        query += ' WHERE source=?'
        params = (source,)
    with connect(db_path) as conn:
        return int(conn.execute(query, params).fetchone()[0])


def fetch_events(db_path: Path, source: str, event_type: str, limit: int = 100) -> list[sqlite3.Row]:
    with connect(db_path) as conn:
        return list(conn.execute(
            "SELECT * FROM events WHERE source=? AND event_type=? ORDER BY collected_at DESC LIMIT ?",
            (source, event_type, limit),
        ))


def count_events(db_path: Path, source: str | None = None, event_type: str | None = None) -> int:
    query = 'SELECT COUNT(*) FROM events'
    clauses: list[str] = []
    params: list[Any] = []
    if source is not None:
        clauses.append('source=?')
        params.append(source)
    if event_type is not None:
        clauses.append('event_type=?')
        params.append(event_type)
    if clauses:
        query += ' WHERE ' + ' AND '.join(clauses)
    with connect(db_path) as conn:
        return int(conn.execute(query, tuple(params)).fetchone()[0])
