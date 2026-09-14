import sqlite3
from pathlib import Path

import pytest

from browser_local_ai_bridge.state import (
    InvalidTransition,
    SchemaVersionError,
    StateError,
    create_task,
    ensure_schema,
    list_events,
    transition,
)


def test_duplicate_event_is_idempotent(tmp_path: Path):
    db = tmp_path / "state.db"
    create_task(db, task_id="task-1", status="QUEUED")
    first = transition(db, task_id="task-1", event_id="event-1", to_status="READY")
    second = transition(db, task_id="task-1", event_id="event-1", to_status="READY")
    assert first["status"] == "READY"
    assert second["status"] == "READY"
    assert len(list_events(db, "task-1")) == 1


def test_conflicting_event_replay_fails(tmp_path: Path):
    db = tmp_path / "state.db"
    create_task(db, task_id="task-1", status="QUEUED")
    transition(db, task_id="task-1", event_id="event-1", to_status="READY")
    with pytest.raises(StateError):
        transition(db, task_id="task-1", event_id="event-1", to_status="RUNNING")
    assert len(list_events(db, "task-1")) == 1


def test_illegal_transition_fails(tmp_path: Path):
    db = tmp_path / "state.db"
    create_task(db, task_id="task-1", status="QUEUED")
    with pytest.raises(InvalidTransition):
        transition(db, task_id="task-1", event_id="event-1", to_status="COMPLETED")


def test_v1_database_migrates_to_transport_schema(tmp_path: Path):
    db = tmp_path / "state.db"
    with sqlite3.connect(db) as con:
        con.executescript(
            """
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO meta(key, value) VALUES('schema_version', '1');
            CREATE TABLE tasks (
                task_id TEXT PRIMARY KEY,
                title TEXT NOT NULL DEFAULT '', goal TEXT NOT NULL DEFAULT '',
                repo TEXT NOT NULL DEFAULT '', branch TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL, owner_worker TEXT NOT NULL DEFAULT '',
                last_completed_step TEXT NOT NULL DEFAULT '', next_action TEXT NOT NULL DEFAULT '',
                attempt INTEGER NOT NULL DEFAULT 0,
                task_envelope_ref TEXT NOT NULL DEFAULT '', result_envelope_ref TEXT NOT NULL DEFAULT '',
                checkpoint_ref TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, updated_at REAL NOT NULL
            );
            CREATE TABLE task_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
                task_id TEXT NOT NULL, from_status TEXT NOT NULL, to_status TEXT NOT NULL,
                outcome TEXT NOT NULL, evidence_ref TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL
            );
            """
        )

    ensure_schema(db)

    with sqlite3.connect(db) as con:
        version = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        tables = {
            row[0]
            for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
    assert version == "3"
    assert {"transport_ingests", "transport_deliveries"} <= tables


def test_newer_schema_fails_closed_without_mutation(tmp_path: Path):
    db = tmp_path / "state.db"
    with sqlite3.connect(db) as con:
        con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        con.execute("INSERT INTO meta(key, value) VALUES('schema_version', '4')")

    with pytest.raises(SchemaVersionError):
        ensure_schema(db)

    with sqlite3.connect(db) as con:
        tables = {
            row[0]
            for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
    assert tables == {"meta"}
