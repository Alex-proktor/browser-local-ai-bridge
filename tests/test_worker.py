import json
from pathlib import Path

import pytest

from browser_local_ai_bridge import envelopes, github_transport, state
from browser_local_ai_bridge.config import RepoTarget
from browser_local_ai_bridge.executors.base import ExecutionOutcome
from browser_local_ai_bridge.worker import MailboxSource, WorkerConfig, worker_tick


class FakeExecutor:
    name = "fake"

    def __init__(self):
        self.calls = 0

    def execute(self, *, task, checkout, observer=None):
        self.calls += 1
        return ExecutionOutcome(
            status="SUCCESS",
            result={
                "status": "SUCCESS", "head": "abc", "changed_files": [],
                "commands_run": ["pytest"], "tests": ["1 passed"],
                "first_error": "", "git_status": "clean", "blocker": "",
                "next_recommended_action": "done",
            },
            executor=self.name,
        )


def _task(task_id: str = "worker-task"):
    return envelopes.task_envelope(
        task_id=task_id,
        goal="Fix synthetic sample",
        repo="sample/repo",
        branch="main",
        why_local_required="Needs local checkout",
        allowed_actions=["read", "edit", "test"],
        forbidden_actions=["deploy"],
        known_facts=["synthetic"],
        do_not_repeat=[],
        expected_output="Return tests",
    )


def _setup(tmp_path: Path):
    db = tmp_path / "state.db"
    tasks = tmp_path / "tasks"
    locks = tmp_path / "locks"
    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)
    allowlist = {"sample/repo": RepoTarget("sample/repo", checkout)}
    config = WorkerConfig(sources=(MailboxSource("sample/mailbox", 7),))
    return db, tasks, locks, allowlist, config


def _comment_payload(task_id: str = "worker-task") -> dict:
    payload = {
        "schema": envelopes.TASK_SCHEMA,
        "version": 1,
        "event_id": f"event-{task_id}",
        "task": _task(task_id),
        "transport": {"result_repo": "sample/mailbox", "result_issue": 7},
    }
    return {
        "id": 10,
        "body": envelopes.TASK_SCHEMA + "\n\n```json\n" + json.dumps(payload) + "\n```",
    }


def test_tick_ingest_execute_publish_and_replay(tmp_path: Path):
    db, tasks, locks, allowlist, config = _setup(tmp_path)
    executor = FakeExecutor()
    published: list[str] = []

    def ingest_fn(**kwargs):
        return github_transport.ingest_comments(
            db_path=kwargs["db_path"], tasks_root=kwargs["tasks_root"],
            allowlist=kwargs["allowlist"], source_repo=kwargs["repo"],
            source_issue=kwargs["issue"], comments=[_comment_payload()],
        )

    def publish_fn(*, task_id, db_path):
        published.append(task_id)
        state.record_transport_delivery(db_path, task_id=task_id, outcome="SUCCESS", remote_id="99")

    first = worker_tick(
        db_path=db, tasks_root=tasks, lock_root=locks, allowlist=allowlist,
        config=config, executor=executor, ingest_fn=ingest_fn,
        publish_fn=publish_fn, branch_reader=lambda _: "main",
    )
    second = worker_tick(
        db_path=db, tasks_root=tasks, lock_root=locks, allowlist=allowlist,
        config=config, executor=executor, ingest_fn=ingest_fn,
        publish_fn=publish_fn, branch_reader=lambda _: "main",
    )

    assert first.ingested == ("worker-task",)
    assert first.executed == "worker-task"
    assert first.published == ("worker-task",)
    assert first.errors == ()
    assert second.ingested == ()
    assert second.executed == ""
    assert second.published == ()
    assert executor.calls == 1
    assert published == ["worker-task"]


def test_source_failure_is_bounded_and_next_source_continues(tmp_path: Path):
    db, tasks, locks, allowlist, _ = _setup(tmp_path)
    config = WorkerConfig(sources=(MailboxSource("bad/source", 1), MailboxSource("sample/mailbox", 7)))
    executor = FakeExecutor()
    def ingest_fn(**kwargs):
        if kwargs["repo"] == "bad/source":
            raise github_transport.GitHubTransportError("synthetic source failure")
        return github_transport.ingest_comments(
            db_path=kwargs["db_path"], tasks_root=kwargs["tasks_root"],
            allowlist=kwargs["allowlist"], source_repo=kwargs["repo"],
            source_issue=kwargs["issue"], comments=[_comment_payload("worker-task-2")],
        )

    def publish_fn(*, task_id, db_path):
        state.record_transport_delivery(db_path, task_id=task_id, outcome="SUCCESS", remote_id="100")

    result = worker_tick(
        db_path=db, tasks_root=tasks, lock_root=locks, allowlist=allowlist,
        config=config, executor=executor, ingest_fn=ingest_fn,
        publish_fn=publish_fn, branch_reader=lambda _: "main",
    )

    assert result.ingested == ("worker-task-2",)
    assert result.executed == "worker-task-2"
    assert result.published == ("worker-task-2",)
    assert len(result.errors) == 1
    assert result.errors[0].startswith("ingest:bad/source#1:")
    assert executor.calls == 1


def test_publish_failure_is_bounded_and_retried_next_tick(tmp_path: Path):
    db, tasks, locks, allowlist, config = _setup(tmp_path)
    executor = FakeExecutor()
    calls = {"publish": 0}

    def ingest_fn(**kwargs):
        return github_transport.ingest_comments(
            db_path=kwargs["db_path"], tasks_root=kwargs["tasks_root"],
            allowlist=kwargs["allowlist"], source_repo=kwargs["repo"],
            source_issue=kwargs["issue"], comments=[_comment_payload("worker-task-3")],
        )

    def publish_fn(*, task_id, db_path):
        calls["publish"] += 1
        if calls["publish"] == 1:
            raise github_transport.GitHubTransportError("synthetic publish failure")
        state.record_transport_delivery(db_path, task_id=task_id, outcome="SUCCESS", remote_id="101")

    first = worker_tick(
        db_path=db, tasks_root=tasks, lock_root=locks, allowlist=allowlist,
        config=config, executor=executor, ingest_fn=ingest_fn,
        publish_fn=publish_fn, branch_reader=lambda _: "main",
    )
    second = worker_tick(
        db_path=db, tasks_root=tasks, lock_root=locks, allowlist=allowlist,
        config=config, executor=executor, ingest_fn=ingest_fn,
        publish_fn=publish_fn, branch_reader=lambda _: "main",
    )

    assert first.executed == "worker-task-3"
    assert first.published == ()
    assert len(first.errors) == 1 and first.errors[0].startswith("publish:worker-task-3:")
    assert second.executed == ""
    assert second.published == ("worker-task-3",)
    assert executor.calls == 1
    assert calls["publish"] == 2


def test_recovery_runs_before_new_execution(tmp_path: Path, monkeypatch):
    from browser_local_ai_bridge import runtime

    db, tasks, locks, allowlist, config = _setup(tmp_path)
    stale = "stale-task"
    state.create_task(db, task_id=stale, status="RUNNING", repo="sample/repo", branch="main")
    runtime.start_run(db, run_id="stale-run", task_id=stale, executor="fake")
    runtime.record_process(db, "stale-run", pid=123, birth_token="old", timeout_seconds=30)
    monkeypatch.setattr("browser_local_ai_bridge.process_control.process_alive", lambda pid, token: False)

    executor = FakeExecutor()
    def ingest_fn(**kwargs):
        return github_transport.ingest_comments(
            db_path=kwargs["db_path"], tasks_root=kwargs["tasks_root"],
            allowlist=kwargs["allowlist"], source_repo=kwargs["repo"],
            source_issue=kwargs["issue"], comments=[_comment_payload("worker-task-4")],
        )

    def publish_fn(*, task_id, db_path):
        state.record_transport_delivery(db_path, task_id=task_id, outcome="SUCCESS", remote_id="102")

    result = worker_tick(
        db_path=db, tasks_root=tasks, lock_root=locks, allowlist=allowlist,
        config=config, executor=executor, ingest_fn=ingest_fn,
        publish_fn=publish_fn, branch_reader=lambda _: "main",
    )

    assert result.recovered == ({"task_id": stale, "action": "stale_running", "status": "INTERRUPTED"},)
    assert state.get_task(db, stale)["status"] == "INTERRUPTED"
    assert result.executed == "worker-task-4"
    assert executor.calls == 1


def test_worker_config_requires_explicit_runtime_paths(tmp_path: Path):
    from browser_local_ai_bridge.worker import WorkerConfigError, load_worker_config

    path = tmp_path / "worker.json"
    path.write_text(json.dumps({"version": 1, "sources": [{"repo": "sample/mailbox", "issue": 7}]}), encoding="utf-8")
    with pytest.raises(WorkerConfigError, match="runtime paths"):
        load_worker_config(path)

    path.write_text(json.dumps({
        "version": 1,
        "runtime": {"repos": "repos.json", "db": "state.db", "tasks": "tasks", "locks": "locks"},
        "sources": [{"repo": "sample/mailbox", "issue": 7}],
        "executor": {"kind": "codex", "executable": "codex", "timeout_seconds": 120},
        "poll_seconds": 0.25,
    }), encoding="utf-8")
    config = load_worker_config(path)
    assert config.repos_path == (tmp_path / "repos.json").resolve()
    assert config.db_path == (tmp_path / "state.db").resolve()
    assert config.tasks_root == (tmp_path / "tasks").resolve()
    assert config.lock_root == (tmp_path / "locks").resolve()
    assert config.poll_seconds == 0.25
    assert config.timeout_seconds == 120
