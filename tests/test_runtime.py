from pathlib import Path

from browser_local_ai_bridge import envelopes, runtime, state


def _task(db: Path, task_id: str = "task-1", status: str = "RUNNING"):
    return state.create_task(db, task_id=task_id, status=status, repo="sample/repo", branch="main")


def test_run_metadata_and_status(tmp_path: Path, monkeypatch):
    db = tmp_path / "state.db"
    _task(db)
    runtime.start_run(db, run_id="run-1", task_id="task-1", executor="fake")
    runtime.record_process(db, "run-1", pid=123, birth_token="birth", timeout_seconds=30)
    runtime.record_progress(db, "run-1", "turn.started")
    monkeypatch.setattr("browser_local_ai_bridge.process_control.process_alive", lambda pid, token: True)

    current = runtime.status(db, "task-1")
    assert current["run_id"] == "run-1"
    assert current["pid"] == 123
    assert current["process_alive"] is True
    assert current["progress"] == "turn.started"


def test_cancel_rejects_stale_pid_and_keeps_running(tmp_path: Path, monkeypatch):
    db = tmp_path / "state.db"
    _task(db)
    runtime.start_run(db, run_id="run-1", task_id="task-1", executor="fake")
    runtime.record_process(db, "run-1", pid=123, birth_token="birth", timeout_seconds=30)
    monkeypatch.setattr("browser_local_ai_bridge.process_control.terminate_process_tree", lambda pid, token: (False, "stale_or_reused_pid"))

    result = runtime.cancel_active(db, "task-1")
    assert result["cancelled"] is False
    assert result["reason"] == "stale_or_reused_pid"
    assert state.get_task(db, "task-1")["status"] == "RUNNING"


def test_cancel_verified_process_transitions_cancelled(tmp_path: Path, monkeypatch):
    db = tmp_path / "state.db"
    _task(db)
    runtime.start_run(db, run_id="run-1", task_id="task-1", executor="fake")
    runtime.record_process(db, "run-1", pid=123, birth_token="birth", timeout_seconds=30)
    monkeypatch.setattr("browser_local_ai_bridge.process_control.terminate_process_tree", lambda pid, token: (True, "terminated"))

    result = runtime.cancel_active(db, "task-1")
    assert result["cancelled"] is True
    assert state.get_task(db, "task-1")["status"] == "CANCELLED"
    assert runtime.latest_run(db, "task-1")["outcome"] == "CANCELLED"


def test_reconcile_stale_running_becomes_interrupted(tmp_path: Path, monkeypatch):
    db = tmp_path / "state.db"
    _task(db)
    runtime.start_run(db, run_id="run-1", task_id="task-1", executor="fake")
    runtime.record_process(db, "run-1", pid=123, birth_token="birth", timeout_seconds=30)
    monkeypatch.setattr("browser_local_ai_bridge.process_control.process_alive", lambda pid, token: False)

    actions = runtime.reconcile_running(db)
    assert actions == [{"task_id": "task-1", "action": "stale_running", "status": "INTERRUPTED"}]
    assert state.get_task(db, "task-1")["status"] == "INTERRUPTED"
    assert runtime.latest_run(db, "task-1")["error_type"] == "stale_running"


def test_reconcile_terminal_envelopes_does_not_reexecute(tmp_path: Path):
    db = tmp_path / "state.db"
    result = tmp_path / "result.json"
    checkpoint = tmp_path / "checkpoint.json"
    envelopes.atomic_write_json(result, envelopes.result_envelope(task_id="task-1", status="SUCCESS"))
    envelopes.atomic_write_json(checkpoint, envelopes.checkpoint_envelope(task_id="task-1", goal="synthetic"))
    state.create_task(
        db, task_id="task-1", status="RUNNING", repo="sample/repo", branch="main",
        result_envelope_ref=str(result), checkpoint_ref=str(checkpoint),
    )
    runtime.start_run(db, run_id="run-1", task_id="task-1", executor="fake")

    actions = runtime.reconcile_running(db)
    assert actions[0]["action"] == "terminal_result"
    assert state.get_task(db, "task-1")["status"] == "WAITING_CONTROLLER"
    assert runtime.latest_run(db, "task-1")["outcome"] == "SUCCESS"


def test_cancel_clears_terminal_refs_if_they_appeared(tmp_path: Path, monkeypatch):
    db = tmp_path / "state.db"
    result = tmp_path / "result.json"
    checkpoint = tmp_path / "checkpoint.json"
    result.write_text("{}", encoding="utf-8")
    checkpoint.write_text("{}", encoding="utf-8")
    state.create_task(
        db, task_id="task-1", status="RUNNING", repo="sample/repo", branch="main",
        result_envelope_ref=str(result), checkpoint_ref=str(checkpoint),
    )
    runtime.start_run(db, run_id="run-1", task_id="task-1", executor="fake")
    runtime.record_process(db, "run-1", pid=123, birth_token="birth", timeout_seconds=30)
    monkeypatch.setattr("browser_local_ai_bridge.process_control.terminate_process_tree", lambda pid, token: (True, "terminated"))

    runtime.cancel_active(db, "task-1")
    task = state.get_task(db, "task-1")
    assert task["status"] == "CANCELLED"
    assert task["result_envelope_ref"] == ""
    assert task["checkpoint_ref"] == ""
    assert not result.exists()
    assert not checkpoint.exists()
