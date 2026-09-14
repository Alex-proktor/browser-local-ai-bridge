from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import envelopes, process_control, state


@dataclass(frozen=True)
class ExecutionObserver:
    db_path: Path
    run_id: str

    def on_spawn(self, *, pid: int, birth_token: str, timeout_seconds: int) -> None:
        record_process(
            self.db_path,
            self.run_id,
            pid=pid,
            birth_token=birth_token,
            timeout_seconds=timeout_seconds,
        )

    def on_progress(self, progress: str) -> None:
        record_progress(self.db_path, self.run_id, progress)


def start_run(db_path: Path, *, run_id: str, task_id: str, executor: str) -> dict[str, Any]:
    state.ensure_schema(db_path)
    started = time.time()
    with state._connect(db_path) as con:
        con.execute(
            """
            INSERT INTO execution_runs(run_id,task_id,executor,started_at)
            VALUES(?,?,?,?)
            """,
            (run_id, task_id, str(executor or "")[:200], started),
        )
        row = con.execute("SELECT * FROM execution_runs WHERE run_id=?", (run_id,)).fetchone()
    return dict(row)


def record_process(
    db_path: Path,
    run_id: str,
    *,
    pid: int,
    birth_token: str,
    timeout_seconds: int,
) -> None:
    with state._connect(db_path) as con:
        con.execute(
            "UPDATE execution_runs SET pid=?,process_birth_token=?,timeout_seconds=? WHERE run_id=?",
            (int(pid), str(birth_token or "")[:200], int(timeout_seconds or 0), run_id),
        )


def record_progress(db_path: Path, run_id: str, progress: str) -> None:
    now = time.time()
    with state._connect(db_path) as con:
        con.execute(
            "UPDATE execution_runs SET progress_excerpt=?,progress_updated_at=? WHERE run_id=?",
            (str(progress or "")[:1200], now, run_id),
        )


def finish_run(
    db_path: Path,
    run_id: str,
    *,
    outcome: str,
    error_type: str = "",
    result_ref: str = "",
) -> None:
    with state._connect(db_path) as con:
        con.execute(
            """
            UPDATE execution_runs
               SET finished_at=?,outcome=?,error_type=?,result_ref=?
             WHERE run_id=?
            """,
            (time.time(), str(outcome or "")[:100], str(error_type or "")[:300], str(result_ref or "")[:500], run_id),
        )


def get_run(db_path: Path, run_id: str) -> dict[str, Any] | None:
    state.ensure_schema(db_path)
    with state._connect(db_path) as con:
        row = con.execute("SELECT * FROM execution_runs WHERE run_id=?", (run_id,)).fetchone()
    return dict(row) if row else None


def latest_run(db_path: Path, task_id: str) -> dict[str, Any] | None:
    state.ensure_schema(db_path)
    with state._connect(db_path) as con:
        row = con.execute(
            "SELECT * FROM execution_runs WHERE task_id=? ORDER BY started_at DESC LIMIT 1",
            (task_id,),
        ).fetchone()
    return dict(row) if row else None


def status(db_path: Path, task_id: str) -> dict[str, Any]:
    task = state.get_task(db_path, task_id)
    if task is None:
        raise state.TaskNotFound(task_id)
    run = latest_run(db_path, task_id) or {}
    pid = run.get("pid")
    birth = str(run.get("process_birth_token") or "")
    return {
        "task_id": task_id,
        "status": task["status"],
        "repo": task["repo"],
        "branch": task["branch"],
        "run_id": run.get("run_id", ""),
        "executor": run.get("executor", ""),
        "pid": pid,
        "process_alive": process_control.process_alive(pid, birth),
        "progress": run.get("progress_excerpt", ""),
        "outcome": run.get("outcome", ""),
        "error_type": run.get("error_type", ""),
    }


def cancel_active(db_path: Path, task_id: str) -> dict[str, Any]:
    task = state.get_task(db_path, task_id)
    if task is None:
        raise state.TaskNotFound(task_id)
    run = latest_run(db_path, task_id)
    if task["status"] != "RUNNING" or not run or run.get("finished_at") is not None:
        return {"task_id": task_id, "cancelled": False, "reason": "no_active_run", "status": task["status"]}
    pid = run.get("pid")
    birth = str(run.get("process_birth_token") or "")
    if not pid or not birth:
        return {"task_id": task_id, "cancelled": False, "reason": "process_identity_unavailable", "status": "RUNNING"}
    killed, reason = process_control.terminate_process_tree(int(pid), birth)
    if not killed:
        return {"task_id": task_id, "cancelled": False, "reason": reason, "status": "RUNNING"}
    updated = state.transition(
        db_path,
        task_id=task_id,
        event_id=f"execution:{run['run_id']}:cancel",
        to_status="CANCELLED",
        outcome="user_cancelled",
    )
    finish_run(db_path, str(run["run_id"]), outcome="CANCELLED", error_type="user_cancelled")
    for ref_field in ("result_envelope_ref", "checkpoint_ref"):
        ref = str(task.get(ref_field) or "")
        if ref:
            Path(ref).unlink(missing_ok=True)
    state.update_task_refs(db_path, task_id, result_envelope_ref="", checkpoint_ref="")
    return {"task_id": task_id, "cancelled": True, "reason": reason, "status": updated["status"]}


def reconcile_running(db_path: Path) -> list[dict[str, Any]]:
    state.ensure_schema(db_path)
    actions: list[dict[str, Any]] = []
    with state._connect(db_path) as con:
        rows = con.execute("SELECT * FROM tasks WHERE status='RUNNING' ORDER BY updated_at").fetchall()
    for row in rows:
        task = dict(row)
        task_id = str(task["task_id"])
        run = latest_run(db_path, task_id)
        if task.get("result_envelope_ref") and task.get("checkpoint_ref"):
            result_path = Path(str(task["result_envelope_ref"]))
            checkpoint_path = Path(str(task["checkpoint_ref"]))
            try:
                result = envelopes.load_json(result_path, envelopes.RESULT_SCHEMA)
                checkpoint = envelopes.load_json(checkpoint_path, envelopes.CHECKPOINT_SCHEMA)
                valid_terminal = result.get("task_id") == task_id and checkpoint.get("task_id") == task_id
            except (OSError, envelopes.EnvelopeError):
                valid_terminal = False
                result = {}
            if valid_terminal:
                result_status = str(result.get("status") or "").upper()
                final_status = {"SUCCESS": "WAITING_CONTROLLER", "FAILED": "FAILED", "INTERRUPTED": "INTERRUPTED"}.get(result_status)
                if final_status:
                    state.transition(
                        db_path, task_id=task_id, event_id=f"recovery:{task_id}:terminal",
                        to_status=final_status, outcome="recovered-terminal-result", evidence_ref=str(result_path),
                    )
                    if run and run.get("finished_at") is None:
                        finish_run(db_path, str(run["run_id"]), outcome=result_status, result_ref=str(result_path))
                    actions.append({"task_id": task_id, "action": "terminal_result", "status": final_status})
                    continue
        if run and process_control.process_alive(run.get("pid"), str(run.get("process_birth_token") or "")):
            actions.append({"task_id": task_id, "action": "still_running", "status": "RUNNING"})
            continue
        if run and run.get("finished_at") is None:
            finish_run(db_path, str(run["run_id"]), outcome="INTERRUPTED", error_type="stale_running")
        state.transition(
            db_path,
            task_id=task_id,
            event_id=f"recovery:{task_id}:stale",
            to_status="INTERRUPTED",
            outcome="stale_running",
        )
        actions.append({"task_id": task_id, "action": "stale_running", "status": "INTERRUPTED"})
    return actions
