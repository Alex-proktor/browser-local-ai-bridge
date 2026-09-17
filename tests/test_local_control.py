import json
from pathlib import Path

import pytest

from browser_local_ai_bridge import envelopes, runtime, state
from browser_local_ai_bridge.config import RepoTarget
from browser_local_ai_bridge.executors.base import ExecutionOutcome
from browser_local_ai_bridge.local_control import DirectLocalControl, LocalControlError, RuntimePaths


class FakeExecutor:
    name = "fake"

    def execute(self, *, task, checkout, observer=None):
        return ExecutionOutcome(
            status="SUCCESS",
            result={
                "status": "SUCCESS",
                "head": "abc123",
                "changed_files": [],
                "commands_run": ["git status"],
                "tests": ["DIRECT_LOCAL_OK"],
                "first_error": "",
                "git_status": "clean",
                "blocker": "",
                "next_recommended_action": "done",
            },
            executor=self.name,
        )


def _setup(tmp_path: Path, *, branch: str = "main"):
    home = tmp_path / "home"
    checkout = tmp_path / "repo"
    (checkout / ".git").mkdir(parents=True)
    home.mkdir()
    (home / "repos.json").write_text(
        json.dumps({"version": 1, "repos": {"sample/repo": str(checkout)}}),
        encoding="utf-8",
    )
    task = envelopes.task_envelope(
        task_id="direct-task-1",
        goal="Read repository state",
        repo="sample/repo",
        branch=branch,
        why_local_required="Needs local checkout",
        allowed_actions=["read repository state"],
        forbidden_actions=["edit files"],
        known_facts=["synthetic"],
        do_not_repeat=[],
        expected_output="Return DIRECT_LOCAL_OK",
    )
    control = DirectLocalControl(
        paths=RuntimePaths.from_home(home),
        executor=FakeExecutor(),
        branch_reader=lambda _: "main",
    )
    return control, task


def test_health_and_repo_list_do_not_expose_paths(tmp_path: Path):
    control, _ = _setup(tmp_path)
    assert control.health()["status"] == "PASS"
    repos = control.list_repos()
    assert repos == {"repos": ["sample/repo"]}
    assert str(tmp_path) not in json.dumps(repos)


def test_validate_and_execute_synthetic_direct_local(tmp_path: Path):
    control, task = _setup(tmp_path)
    assert control.validate_task(task)["valid"] is True
    result = control.submit(task)
    assert result["status"] == "WAITING_CONTROLLER"
    assert result["result"]["status"] == "SUCCESS"
    assert result["result"]["tests"] == ["DIRECT_LOCAL_OK"]


def test_unknown_repo_and_branch_fail_closed(tmp_path: Path):
    control, task = _setup(tmp_path)
    bad_repo = dict(task, repo="unknown/repo")
    with pytest.raises(ValueError):
        control.validate_task(bad_repo)
    bad_branch = dict(task, branch="feature")
    with pytest.raises(ValueError, match="branch mismatch"):
        control.validate_task(bad_branch)


def test_cancel_ready_and_missing_runtime_fails_closed(tmp_path: Path):
    control, task = _setup(tmp_path)
    task_path = envelopes.task_dir(control.paths.tasks, task["task_id"]) / "task.json"
    envelopes.atomic_write_json(task_path, task)
    state.create_task(
        control.paths.db,
        task_id=task["task_id"],
        status="READY",
        repo=task["repo"],
        branch=task["branch"],
        task_envelope_ref=str(task_path),
    )
    cancelled = control.cancel(task["task_id"])
    assert cancelled["status"] == "CANCELLED"
    assert control.cancel(task["task_id"])["replay"] is True

    running_id = "running-task"
    state.create_task(control.paths.db, task_id=running_id, status="RUNNING")
    with pytest.raises(LocalControlError, match="no_active_run"):
        control.cancel(running_id)


def test_inspect_returns_canonical_content_without_internal_refs(tmp_path: Path):
    control, task = _setup(tmp_path)
    control.submit(task)
    inspected = control.inspect(task["task_id"])
    assert "task_envelope_ref" not in inspected
    assert inspected["task"]["schema"] == envelopes.TASK_SCHEMA
    assert inspected["result"]["schema"] == envelopes.RESULT_SCHEMA


def test_active_cancel_requires_matching_process_identity(tmp_path: Path, monkeypatch):
    control, task = _setup(tmp_path)
    running_id = "running-task"
    state.create_task(control.paths.db, task_id=running_id, status="RUNNING", repo=task["repo"], branch=task["branch"])
    runtime.start_run(control.paths.db, run_id="run-1", task_id=running_id, executor="fake")
    runtime.record_process(control.paths.db, "run-1", pid=1234, birth_token="birth", timeout_seconds=30)
    monkeypatch.setattr("browser_local_ai_bridge.process_control.terminate_process_tree", lambda pid, token: (False, "stale_or_reused_pid"))

    with pytest.raises(LocalControlError, match="stale_or_reused_pid"):
        control.cancel(running_id)
    assert state.get_task(control.paths.db, running_id)["status"] == "RUNNING"


def test_active_cancel_kills_verified_tree_and_transitions(tmp_path: Path, monkeypatch):
    control, task = _setup(tmp_path)
    running_id = "running-task"
    state.create_task(control.paths.db, task_id=running_id, status="RUNNING", repo=task["repo"], branch=task["branch"])
    runtime.start_run(control.paths.db, run_id="run-1", task_id=running_id, executor="fake")
    runtime.record_process(control.paths.db, "run-1", pid=4321, birth_token="birth", timeout_seconds=30)
    calls = []
    monkeypatch.setattr("browser_local_ai_bridge.process_control.terminate_process_tree", lambda pid, token: calls.append((pid, token)) or (True, "terminated"))

    result = control.cancel(running_id)
    assert result == {"task_id": running_id, "status": "CANCELLED", "replay": False, "reason": "terminated"}
    assert calls == [(4321, "birth")]


def test_active_cancel_does_not_remove_foreign_checkout_lock(tmp_path, monkeypatch):
    control, task = _setup(tmp_path)
    running_id = "running-owned-lock"
    state.create_task(control.paths.db, task_id=running_id, status="RUNNING", repo=task["repo"], branch=task["branch"])
    runtime.start_run(control.paths.db, run_id="run-1", task_id=running_id, executor="fake")
    runtime.record_process(control.paths.db, "run-1", pid=4321, birth_token="birth", timeout_seconds=30)
    monkeypatch.setattr("browser_local_ai_bridge.process_control.terminate_process_tree", lambda pid, token: (True, "terminated"))
    checkout = Path(json.loads(control.paths.repos.read_text())["repos"]["sample/repo"])
    from browser_local_ai_bridge.execution import _lock_name
    control.paths.locks.mkdir(parents=True, exist_ok=True)
    lock = control.paths.locks / _lock_name(checkout)
    lock.write_text(json.dumps({"task_id": "other-task", "run_id": "other-run"}))
    assert control.cancel(running_id)["status"] == "CANCELLED"
    assert lock.exists()
