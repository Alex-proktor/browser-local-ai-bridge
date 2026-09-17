from pathlib import Path

import pytest

from browser_local_ai_bridge import envelopes, state
from browser_local_ai_bridge.config import RepoTarget
from browser_local_ai_bridge.execution import ExecutionError, checkout_lock, execute_task
from browser_local_ai_bridge.executors.base import ExecutionOutcome


class FakeExecutor:
    name = "fake"

    def __init__(self, outcome: ExecutionOutcome):
        self.outcome = outcome
        self.calls = 0

    def execute(self, *, task, checkout, observer=None):
        self.calls += 1
        return self.outcome


def _prepare(tmp_path: Path, *, branch: str = "main"):
    db = tmp_path / "state.db"
    tasks_root = tmp_path / "tasks"
    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)
    task = envelopes.task_envelope(
        task_id="task-1",
        goal="Fix sample",
        repo="sample/repo",
        branch=branch,
        why_local_required="Needs local files",
        allowed_actions=["read", "edit", "test"],
        forbidden_actions=["deploy"],
        known_facts=["synthetic"],
        do_not_repeat=[],
        expected_output="Return tests",
    )
    task_path = envelopes.task_dir(tasks_root, "task-1") / "task.json"
    envelopes.atomic_write_json(task_path, task)
    state.create_task(
        db,
        task_id="task-1",
        status="READY",
        goal=task["goal"],
        repo=task["repo"],
        branch=task["branch"],
        task_envelope_ref=str(task_path),
    )
    allowlist = {"sample/repo": RepoTarget("sample/repo", checkout)}
    return db, tasks_root, checkout, allowlist


def _success_outcome():
    return ExecutionOutcome(
        status="SUCCESS",
        result={
            "status": "SUCCESS",
            "head": "abc123",
            "changed_files": ["sample.py"],
            "commands_run": ["pytest"],
            "tests": ["1 passed"],
            "first_error": "",
            "git_status": "clean",
            "blocker": "",
            "artifacts": [],
            "next_recommended_action": "done",
        },
        executor="fake",
    )


def test_success_writes_envelopes_and_waits_for_controller(tmp_path: Path):
    db, tasks_root, _, allowlist = _prepare(tmp_path)
    executor = FakeExecutor(_success_outcome())

    outcome = execute_task(
        db_path=db,
        tasks_root=tasks_root,
        lock_root=tmp_path / "locks",
        task_id="task-1",
        allowlist=allowlist,
        executor=executor,
        branch_reader=lambda _: "main",
    )

    task = state.get_task(db, "task-1")
    assert outcome.status == "SUCCESS"
    assert executor.calls == 1
    assert task is not None and task["status"] == "WAITING_CONTROLLER"
    result = envelopes.load_json(Path(task["result_envelope_ref"]), envelopes.RESULT_SCHEMA)
    checkpoint = envelopes.load_json(Path(task["checkpoint_ref"]), envelopes.CHECKPOINT_SCHEMA)
    assert result["tests"] == ["1 passed"]
    assert checkpoint["changed"] == ["sample.py"]
    assert not list((tmp_path / "locks").glob("*.lock"))


def test_failed_executor_writes_publishable_failure(tmp_path: Path):
    db, tasks_root, _, allowlist = _prepare(tmp_path)
    executor = FakeExecutor(
        ExecutionOutcome(
            status="FAILED",
            exit_code=2,
            error_type="nonzero_exit",
            error="executor failed",
            executor="fake",
        )
    )

    execute_task(
        db_path=db,
        tasks_root=tasks_root,
        lock_root=tmp_path / "locks",
        task_id="task-1",
        allowlist=allowlist,
        executor=executor,
        branch_reader=lambda _: "main",
    )

    task = state.get_task(db, "task-1")
    assert task is not None and task["status"] == "FAILED"
    result = envelopes.load_json(Path(task["result_envelope_ref"]), envelopes.RESULT_SCHEMA)
    assert result["status"] == "FAILED"
    assert "executor failed" in result["first_error"]


def test_structured_failure_keeps_tests_and_blocker(tmp_path: Path):
    db, tasks_root, _, allowlist = _prepare(tmp_path)
    executor = FakeExecutor(
        ExecutionOutcome(
            status="FAILED",
            error_type="structured_failure",
            error="sample failed",
            result={
                "status": "FAILED",
                "changed_files": ["sample.py"],
                "commands_run": ["pytest"],
                "tests": ["1 failed"],
                "first_error": "sample failed",
                "git_status": "modified sample.py",
                "blocker": "sample failed",
                "next_recommended_action": "fix sample",
            },
            executor="fake",
        )
    )
    execute_task(
        db_path=db,
        tasks_root=tasks_root,
        lock_root=tmp_path / "locks",
        task_id="task-1",
        allowlist=allowlist,
        executor=executor,
        branch_reader=lambda _: "main",
    )
    task = state.get_task(db, "task-1")
    assert task is not None and task["status"] == "FAILED"
    result = envelopes.load_json(Path(task["result_envelope_ref"]), envelopes.RESULT_SCHEMA)
    assert result["tests"] == ["1 failed"]
    assert result["blocker"] == "sample failed"


def test_interrupted_executor_sets_interrupted(tmp_path: Path):
    db, tasks_root, _, allowlist = _prepare(tmp_path)
    executor = FakeExecutor(
        ExecutionOutcome(
            status="INTERRUPTED",
            error_type="wall_timeout",
            error="timed out",
            executor="fake",
        )
    )
    execute_task(
        db_path=db,
        tasks_root=tasks_root,
        lock_root=tmp_path / "locks",
        task_id="task-1",
        allowlist=allowlist,
        executor=executor,
        branch_reader=lambda _: "main",
    )
    assert state.get_task(db, "task-1")["status"] == "INTERRUPTED"


def test_branch_mismatch_fails_before_executor(tmp_path: Path):
    db, tasks_root, _, allowlist = _prepare(tmp_path, branch="feature")
    executor = FakeExecutor(_success_outcome())
    with pytest.raises(ExecutionError, match="branch mismatch"):
        execute_task(
            db_path=db,
            tasks_root=tasks_root,
            lock_root=tmp_path / "locks",
            task_id="task-1",
            allowlist=allowlist,
            executor=executor,
            branch_reader=lambda _: "main",
        )
    assert executor.calls == 0
    assert state.get_task(db, "task-1")["status"] == "READY"


def test_checkout_lock_rejects_parallel_claim(tmp_path: Path):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    lock_root = tmp_path / "locks"
    with checkout_lock(lock_root, checkout, task_id="task-1", run_id="run-1"):
        with pytest.raises(ExecutionError, match="already claimed"):
            with checkout_lock(lock_root, checkout, task_id="task-2", run_id="run-2"):
                pass



def test_external_cancel_wins_finalization_race(tmp_path: Path, monkeypatch):
    import threading
    from browser_local_ai_bridge import runtime

    db, tasks_root, _, allowlist = _prepare(tmp_path)
    spawned = threading.Event()
    release = threading.Event()

    class BlockingExecutor:
        name = "blocking"
        def execute(self, *, task, checkout, observer=None):
            assert observer is not None
            observer.on_spawn(pid=999, birth_token="birth", timeout_seconds=30)
            spawned.set()
            release.wait(5)
            return ExecutionOutcome(status="FAILED", error_type="killed", executor=self.name)

    holder = {}
    def run():
        holder["outcome"] = execute_task(
            db_path=db, tasks_root=tasks_root, lock_root=tmp_path / "locks",
            task_id="task-1", allowlist=allowlist, executor=BlockingExecutor(),
            branch_reader=lambda _: "main",
        )
    thread = threading.Thread(target=run)
    thread.start()
    assert spawned.wait(2)
    monkeypatch.setattr("browser_local_ai_bridge.process_control.terminate_process_tree", lambda pid, token: (True, "terminated"))
    cancelled = runtime.cancel_active(db, "task-1")
    release.set()
    thread.join(5)

    assert cancelled["status"] == "CANCELLED"
    assert state.get_task(db, "task-1")["status"] == "CANCELLED"
    assert holder["outcome"].error_type == "user_cancelled"


def test_cancel_between_result_write_and_final_transition_wins(tmp_path: Path, monkeypatch):
    db, tasks_root, _, allowlist = _prepare(tmp_path)
    original_transition = state.transition

    def racing_transition(path, *, task_id, event_id, to_status, **kwargs):
        if event_id.endswith(":finish"):
            original_transition(
                path, task_id=task_id, event_id="race:cancel",
                to_status="CANCELLED", outcome="test-cancel",
            )
        return original_transition(
            path, task_id=task_id, event_id=event_id,
            to_status=to_status, **kwargs,
        )

    monkeypatch.setattr(state, "transition", racing_transition)
    outcome = execute_task(
        db_path=db, tasks_root=tasks_root, lock_root=tmp_path / "locks",
        task_id="task-1", allowlist=allowlist,
        executor=FakeExecutor(_success_outcome()), branch_reader=lambda _: "main",
    )
    task = state.get_task(db, "task-1")
    assert task["status"] == "CANCELLED"
    assert outcome.error_type == "user_cancelled"
    assert not (envelopes.task_dir(tasks_root, "task-1") / "result.json").exists()
    assert not (envelopes.task_dir(tasks_root, "task-1") / "checkpoint.json").exists()


def test_cancel_after_final_transition_still_wins_before_refs(tmp_path: Path, monkeypatch):
    db, tasks_root, _, allowlist = _prepare(tmp_path)
    original_transition = state.transition

    def transition_then_cancel(path, *, task_id, event_id, to_status, **kwargs):
        updated = original_transition(
            path, task_id=task_id, event_id=event_id,
            to_status=to_status, **kwargs,
        )
        if event_id.endswith(":finish"):
            original_transition(
                path, task_id=task_id, event_id="race:late-cancel",
                to_status="CANCELLED", outcome="test-cancel",
            )
        return updated

    monkeypatch.setattr(state, "transition", transition_then_cancel)
    outcome = execute_task(
        db_path=db, tasks_root=tasks_root, lock_root=tmp_path / "locks",
        task_id="task-1", allowlist=allowlist,
        executor=FakeExecutor(_success_outcome()), branch_reader=lambda _: "main",
    )
    task = state.get_task(db, "task-1")
    assert task["status"] == "CANCELLED"
    assert outcome.error_type == "user_cancelled"
    assert not task["result_envelope_ref"]
    assert not task["checkpoint_ref"]


@pytest.mark.parametrize("branches,expected,error", [
    (("main", "feature"), "feature", None),
    (("feature", "feature"), "feature", "matched 2"),
    (("main", "other"), "feature", "matched 0"),
    (("main", "feature"), "", "branch is required"),
])
def test_multiple_checkout_selection(tmp_path, branches, expected, error):
    db, tasks_root, first, _ = _prepare(tmp_path, branch=expected)
    second = tmp_path / "second"
    (second / ".git").mkdir(parents=True)
    paths = (first, second)
    allowlist = {"sample/repo": (RepoTarget("sample/repo", first), RepoTarget("sample/repo", second))}
    executor = FakeExecutor(_success_outcome())
    kwargs = dict(db_path=db, tasks_root=tasks_root, lock_root=tmp_path / "locks", task_id="task-1",
                  allowlist=allowlist, executor=executor, branch_reader=lambda p: branches[paths.index(p)])
    if error:
        with pytest.raises(ExecutionError, match=error): execute_task(**kwargs)
        assert executor.calls == 0
    else:
        assert execute_task(**kwargs).status == "SUCCESS"
        assert executor.calls == 1
