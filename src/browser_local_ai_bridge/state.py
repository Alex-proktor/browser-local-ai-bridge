from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 3
STATUSES = (
    "QUEUED",
    "READY",
    "RUNNING",
    "WAITING_CONTROLLER",
    "WAITING_EXECUTOR",
    "COMPLETED",
    "BLOCKED",
    "INTERRUPTED",
    "FAILED",
    "CANCELLED",
)
LEGAL_TRANSITIONS: dict[str, set[str]] = {
    "QUEUED": {"READY", "CANCELLED"},
    "READY": {"RUNNING", "BLOCKED", "CANCELLED"},
    "RUNNING": {
        "WAITING_CONTROLLER",
        "WAITING_EXECUTOR",
        "COMPLETED",
        "BLOCKED",
        "INTERRUPTED",
        "FAILED",
        "CANCELLED",
    },
    "WAITING_CONTROLLER": {"READY", "RUNNING", "BLOCKED", "CANCELLED"},
    "WAITING_EXECUTOR": {"READY", "RUNNING", "BLOCKED", "CANCELLED"},
    "BLOCKED": {"READY", "CANCELLED"},
    "INTERRUPTED": {"READY", "RUNNING", "FAILED", "CANCELLED"},
    "FAILED": {"READY", "CANCELLED"},
    "COMPLETED": set(),
    "CANCELLED": set(),
}


class StateError(ValueError):
    pass


class TaskConflict(StateError):
    pass


class TaskNotFound(StateError):
    pass


class InvalidTransition(StateError):
    pass


class SchemaVersionError(StateError):
    pass


class TransportConflict(StateError):
    pass


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=5000")
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def _existing_user_tables(con: sqlite3.Connection) -> set[str]:
    rows = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {str(row["name"]) for row in rows}


def _read_existing_schema_version(con: sqlite3.Connection) -> int | None:
    tables = _existing_user_tables(con)
    if not tables:
        return None
    if "meta" not in tables:
        raise SchemaVersionError("existing task-state database has no meta table")
    row = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if row is None:
        raise SchemaVersionError("existing task-state database has no schema_version")
    try:
        return int(row["value"])
    except (TypeError, ValueError) as exc:
        raise SchemaVersionError("invalid task-state schema_version") from exc


def _preflight_existing_schema(path: Path) -> int | None:
    if not path.exists() or path.stat().st_size == 0:
        return None
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as con:
        con.row_factory = sqlite3.Row
        return _read_existing_schema_version(con)


def _create_v1_schema(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        CREATE TABLE meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE tasks (
            task_id TEXT PRIMARY KEY,
            title TEXT NOT NULL DEFAULT '',
            goal TEXT NOT NULL DEFAULT '',
            repo TEXT NOT NULL DEFAULT '',
            branch TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            owner_worker TEXT NOT NULL DEFAULT '',
            last_completed_step TEXT NOT NULL DEFAULT '',
            next_action TEXT NOT NULL DEFAULT '',
            attempt INTEGER NOT NULL DEFAULT 0,
            task_envelope_ref TEXT NOT NULL DEFAULT '',
            result_envelope_ref TEXT NOT NULL DEFAULT '',
            checkpoint_ref TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE TABLE task_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL UNIQUE,
            task_id TEXT NOT NULL,
            from_status TEXT NOT NULL,
            to_status TEXT NOT NULL,
            outcome TEXT NOT NULL,
            evidence_ref TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
        );
        """
    )
    con.execute("INSERT INTO meta(key, value) VALUES('schema_version', '1')")


def _migrate_v1_to_v2(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        CREATE TABLE transport_ingests (
            event_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            transport_kind TEXT NOT NULL,
            source_ref TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            outcome TEXT NOT NULL,
            created_at REAL NOT NULL
        );
        CREATE INDEX idx_transport_ingests_task ON transport_ingests(task_id);
        CREATE TABLE transport_deliveries (
            task_id TEXT PRIMARY KEY,
            transport_kind TEXT NOT NULL,
            target_ref TEXT NOT NULL,
            event_id TEXT NOT NULL UNIQUE,
            outcome TEXT NOT NULL DEFAULT 'PENDING',
            remote_id TEXT NOT NULL DEFAULT '',
            remote_ref TEXT NOT NULL DEFAULT '',
            last_error TEXT NOT NULL DEFAULT '',
            updated_at REAL NOT NULL,
            FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
        );
        """
    )
    con.execute("UPDATE meta SET value='2' WHERE key='schema_version'")


def _migrate_v2_to_v3(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        CREATE TABLE execution_runs (
            run_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            executor TEXT NOT NULL DEFAULT '',
            pid INTEGER,
            process_birth_token TEXT NOT NULL DEFAULT '',
            started_at REAL NOT NULL,
            finished_at REAL,
            timeout_seconds INTEGER NOT NULL DEFAULT 0,
            progress_excerpt TEXT NOT NULL DEFAULT '',
            progress_updated_at REAL,
            outcome TEXT NOT NULL DEFAULT '',
            error_type TEXT NOT NULL DEFAULT '',
            result_ref TEXT NOT NULL DEFAULT '',
            FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
        );
        CREATE INDEX idx_execution_runs_task ON execution_runs(task_id, started_at);
        """
    )
    con.execute("UPDATE meta SET value='3' WHERE key='schema_version'")


def ensure_schema(path: Path) -> Path:
    existing_version = _preflight_existing_schema(path)
    if existing_version is not None and (existing_version < 1 or existing_version > SCHEMA_VERSION):
        raise SchemaVersionError(f"unsupported task-state schema_version: {existing_version}")

    with _connect(path) as con:
        con.execute("BEGIN IMMEDIATE")
        current_version = _read_existing_schema_version(con)
        if current_version is None:
            _create_v1_schema(con)
            current_version = 1
        if current_version == 1:
            _migrate_v1_to_v2(con)
            current_version = 2
        if current_version == 2:
            _migrate_v2_to_v3(con)
            current_version = 3
        if current_version != SCHEMA_VERSION:
            raise SchemaVersionError(f"unsupported task-state schema_version: {current_version}")
    return path


def _validate_status(status: str) -> str:
    value = str(status or "").strip().upper()
    if value not in STATUSES:
        raise InvalidTransition(f"unknown task status: {status}")
    return value


def create_task(path: Path, *, task_id: str, status: str = "QUEUED", **fields: Any) -> dict[str, Any]:
    ensure_schema(path)
    task_id = str(task_id or "").strip()
    if not task_id:
        raise StateError("task_id is required")
    status = _validate_status(status)
    now = time.time()
    values = {
        "task_id": task_id,
        "title": str(fields.get("title") or "")[:500],
        "goal": str(fields.get("goal") or "")[:1200],
        "repo": str(fields.get("repo") or "")[:500],
        "branch": str(fields.get("branch") or "")[:200],
        "status": status,
        "owner_worker": str(fields.get("owner_worker") or "")[:300],
        "last_completed_step": str(fields.get("last_completed_step") or "")[:1200],
        "next_action": str(fields.get("next_action") or "")[:1200],
        "attempt": max(int(fields.get("attempt") or 0), 0),
        "task_envelope_ref": str(fields.get("task_envelope_ref") or "")[:500],
        "result_envelope_ref": str(fields.get("result_envelope_ref") or "")[:500],
        "checkpoint_ref": str(fields.get("checkpoint_ref") or "")[:500],
        "created_at": now,
        "updated_at": now,
    }
    with _connect(path) as con:
        try:
            con.execute(
                """
                INSERT INTO tasks(
                    task_id,title,goal,repo,branch,status,owner_worker,last_completed_step,next_action,
                    attempt,task_envelope_ref,result_envelope_ref,checkpoint_ref,created_at,updated_at
                ) VALUES(
                    :task_id,:title,:goal,:repo,:branch,:status,:owner_worker,:last_completed_step,:next_action,
                    :attempt,:task_envelope_ref,:result_envelope_ref,:checkpoint_ref,:created_at,:updated_at
                )
                """,
                values,
            )
        except sqlite3.IntegrityError as exc:
            raise TaskConflict(f"task already exists: {task_id}") from exc
    task = get_task(path, task_id)
    assert task is not None
    return task


def get_task(path: Path, task_id: str) -> dict[str, Any] | None:
    ensure_schema(path)
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as con:
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    return dict(row) if row is not None else None


def update_task_refs(
    path: Path,
    task_id: str,
    *,
    task_envelope_ref: str | None = None,
    result_envelope_ref: str | None = None,
    checkpoint_ref: str | None = None,
) -> dict[str, Any]:
    ensure_schema(path)
    updates: list[str] = []
    values: list[Any] = []
    for field, value in (
        ("task_envelope_ref", task_envelope_ref),
        ("result_envelope_ref", result_envelope_ref),
        ("checkpoint_ref", checkpoint_ref),
    ):
        if value is not None:
            updates.append(f"{field}=?")
            values.append(str(value)[:500])
    if not updates:
        task = get_task(path, task_id)
        if task is None:
            raise TaskNotFound(task_id)
        return task
    updates.append("updated_at=?")
    values.append(time.time())
    values.append(task_id)
    with _connect(path) as con:
        cursor = con.execute(
            f"UPDATE tasks SET {', '.join(updates)} WHERE task_id=?",
            values,
        )
        if cursor.rowcount != 1:
            raise TaskNotFound(task_id)
    task = get_task(path, task_id)
    assert task is not None
    return task


def transition(path: Path, *, task_id: str, event_id: str, to_status: str, outcome: str = "", evidence_ref: str = "") -> dict[str, Any]:
    ensure_schema(path)
    to_status = _validate_status(to_status)
    event_id = str(event_id or "").strip()
    if not event_id:
        raise StateError("event_id is required")
    with _connect(path) as con:
        con.execute("BEGIN IMMEDIATE")
        existing = con.execute(
            "SELECT task_id, to_status FROM task_events WHERE event_id=?",
            (event_id,),
        ).fetchone()
        if existing is not None:
            if existing["task_id"] != task_id:
                raise StateError(f"event_id already belongs to another task: {event_id}")
            if existing["to_status"] != to_status:
                raise StateError(f"event_id replay conflicts with original transition: {event_id}")
            row = con.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if row is None:
                raise TaskNotFound(task_id)
            return dict(row)
        row = con.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            raise TaskNotFound(task_id)
        from_status = row["status"]
        if to_status not in LEGAL_TRANSITIONS[from_status]:
            raise InvalidTransition(f"illegal task transition: {from_status} -> {to_status}")
        now = time.time()
        con.execute("UPDATE tasks SET status=?, updated_at=? WHERE task_id=?", (to_status, now, task_id))
        con.execute(
            "INSERT INTO task_events(event_id,task_id,from_status,to_status,outcome,evidence_ref,created_at) VALUES(?,?,?,?,?,?,?)",
            (event_id, task_id, from_status, to_status, str(outcome or "")[:300], str(evidence_ref or "")[:500], now),
        )
        updated = con.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        assert updated is not None
        return dict(updated)


def list_events(path: Path, task_id: str) -> list[dict[str, Any]]:
    ensure_schema(path)
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute("SELECT * FROM task_events WHERE task_id=? ORDER BY id", (task_id,)).fetchall()
    return [dict(row) for row in rows]


def get_transport_ingest(path: Path, event_id: str) -> dict[str, Any] | None:
    ensure_schema(path)
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as con:
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT * FROM transport_ingests WHERE event_id=?", (event_id,)).fetchone()
    return dict(row) if row is not None else None


def record_transport_ingest(
    path: Path,
    *,
    event_id: str,
    task_id: str,
    transport_kind: str,
    source_ref: str,
    payload_hash: str,
    outcome: str = "SUCCESS",
) -> tuple[dict[str, Any], bool]:
    ensure_schema(path)
    event_id = str(event_id or "").strip()
    if not event_id:
        raise StateError("transport event_id is required")
    with _connect(path) as con:
        con.execute("BEGIN IMMEDIATE")
        existing = con.execute("SELECT * FROM transport_ingests WHERE event_id=?", (event_id,)).fetchone()
        if existing is not None:
            row = dict(existing)
            expected = (task_id, transport_kind, source_ref, payload_hash)
            actual = (row["task_id"], row["transport_kind"], row["source_ref"], row["payload_hash"])
            if actual != expected:
                raise TransportConflict(f"conflicting transport ingest replay: {event_id}")
            return row, True
        con.execute(
            "INSERT INTO transport_ingests(event_id,task_id,transport_kind,source_ref,payload_hash,outcome,created_at) VALUES(?,?,?,?,?,?,?)",
            (event_id, task_id, transport_kind, source_ref, payload_hash, outcome, time.time()),
        )
        row = con.execute("SELECT * FROM transport_ingests WHERE event_id=?", (event_id,)).fetchone()
        assert row is not None
        return dict(row), False


def set_transport_delivery_target(
    path: Path,
    *,
    task_id: str,
    transport_kind: str,
    target_ref: str,
    event_id: str,
) -> tuple[dict[str, Any], bool]:
    ensure_schema(path)
    with _connect(path) as con:
        con.execute("BEGIN IMMEDIATE")
        existing = con.execute("SELECT * FROM transport_deliveries WHERE task_id=?", (task_id,)).fetchone()
        if existing is not None:
            row = dict(existing)
            expected = (transport_kind, target_ref, event_id)
            actual = (row["transport_kind"], row["target_ref"], row["event_id"])
            if actual != expected:
                raise TransportConflict(f"conflicting transport delivery target for task: {task_id}")
            return row, True
        try:
            con.execute(
                "INSERT INTO transport_deliveries(task_id,transport_kind,target_ref,event_id,updated_at) VALUES(?,?,?,?,?)",
                (task_id, transport_kind, target_ref, event_id, time.time()),
            )
        except sqlite3.IntegrityError as exc:
            raise TransportConflict(f"transport delivery event_id already used: {event_id}") from exc
        row = con.execute("SELECT * FROM transport_deliveries WHERE task_id=?", (task_id,)).fetchone()
        assert row is not None
        return dict(row), False


def get_transport_delivery(path: Path, task_id: str) -> dict[str, Any] | None:
    ensure_schema(path)
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as con:
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT * FROM transport_deliveries WHERE task_id=?", (task_id,)).fetchone()
    return dict(row) if row is not None else None


def record_transport_delivery(
    path: Path,
    *,
    task_id: str,
    outcome: str,
    remote_id: str = "",
    remote_ref: str = "",
    last_error: str = "",
) -> dict[str, Any]:
    ensure_schema(path)
    with _connect(path) as con:
        cursor = con.execute(
            "UPDATE transport_deliveries SET outcome=?,remote_id=?,remote_ref=?,last_error=?,updated_at=? WHERE task_id=?",
            (outcome, remote_id[:300], remote_ref[:500], last_error[:1200], time.time(), task_id),
        )
        if cursor.rowcount != 1:
            raise TaskNotFound(task_id)
    delivery = get_transport_delivery(path, task_id)
    assert delivery is not None
    return delivery


def list_tasks(
    path: Path,
    *,
    statuses: set[str] | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    ensure_schema(path)
    bounded = max(1, min(int(limit), 1000))
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as con:
        con.row_factory = sqlite3.Row
        if statuses:
            normalized = sorted({_validate_status(status) for status in statuses})
            marks = ",".join("?" for _ in normalized)
            rows = con.execute(
                f"SELECT * FROM tasks WHERE status IN ({marks}) ORDER BY created_at, task_id LIMIT ?",
                [*normalized, bounded],
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM tasks ORDER BY created_at, task_id LIMIT ?",
                (bounded,),
            ).fetchall()
    return [dict(row) for row in rows]
