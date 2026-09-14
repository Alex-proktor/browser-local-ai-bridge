import json
import subprocess
from pathlib import Path

import pytest

from browser_local_ai_bridge import envelopes, state
from browser_local_ai_bridge.config import RepoTarget
from browser_local_ai_bridge.github_transport import (
    GitHubTransportError,
    ingest_comments,
    is_task_candidate,
    publish_result,
)


def _allowlist(tmp_path: Path) -> dict[str, RepoTarget]:
    return {"sample/repo": RepoTarget("sample/repo", tmp_path / "checkout")}


def _payload(*, event_id: str = "event-1", repo: str = "sample/repo", goal: str = "Fix the sample"):
    return {
        "schema": "BRIDGE_TASK_V1",
        "version": 1,
        "event_id": event_id,
        "task": envelopes.task_envelope(
            task_id="task-1",
            goal=goal,
            repo=repo,
            branch="main",
            why_local_required="Needs local files",
            allowed_actions=["read sample", "edit sample"],
            forbidden_actions=["network side effects"],
            known_facts=["synthetic fixture"],
            do_not_repeat=[],
            expected_output="Return tests",
        ),
        "transport": {"result_repo": "sample/mailbox", "result_issue": 7},
    }


def _comment(payload: dict, *, comment_id: int = 10) -> dict:
    body = "BRIDGE_TASK_V1\n\n```json\n" + json.dumps(payload) + "\n```\n"
    return {
        "id": comment_id,
        "html_url": f"https://github.example/sample/mailbox/issues/7#issuecomment-{comment_id}",
        "body": body,
    }


def test_marker_must_be_first_logical_line():
    assert is_task_candidate("\n BRIDGE_TASK_V1\n{}")
    assert not is_task_candidate("result text contains BRIDGE_TASK_V1 later")


def test_ingest_is_idempotent_and_ignores_embedded_marker(tmp_path: Path):
    db = tmp_path / "state.db"
    tasks_root = tmp_path / "tasks"
    comments = [
        {"id": 1, "body": "BRIDGE_RESULT_V1\n{\"note\":\"BRIDGE_TASK_V1\"}"},
        _comment(_payload()),
    ]

    first = ingest_comments(
        db_path=db,
        tasks_root=tasks_root,
        allowlist=_allowlist(tmp_path),
        source_repo="sample/mailbox",
        source_issue=7,
        comments=comments,
    )
    second = ingest_comments(
        db_path=db,
        tasks_root=tasks_root,
        allowlist=_allowlist(tmp_path),
        source_repo="sample/mailbox",
        source_issue=7,
        comments=comments,
    )

    task = state.get_task(db, "task-1")
    assert first.replay is False
    assert second.replay is True
    assert task is not None and task["status"] == "READY"
    assert task["repo"] == "sample/repo"
    assert len(state.list_events(db, "task-1")) == 1
    saved = envelopes.load_json(Path(task["task_envelope_ref"]), envelopes.TASK_SCHEMA)
    assert saved["goal"] == "Fix the sample"


def test_malformed_top_level_marker_fails_closed(tmp_path: Path):
    with pytest.raises(GitHubTransportError, match="malformed"):
        ingest_comments(
            db_path=tmp_path / "state.db",
            tasks_root=tmp_path / "tasks",
            allowlist=_allowlist(tmp_path),
            source_repo="sample/mailbox",
            source_issue=7,
            comments=[{"id": 1, "body": "BRIDGE_TASK_V1\n{bad json"}],
        )


def test_unknown_repo_fails_before_task_creation(tmp_path: Path):
    with pytest.raises(ValueError):
        ingest_comments(
            db_path=tmp_path / "state.db",
            tasks_root=tmp_path / "tasks",
            allowlist=_allowlist(tmp_path),
            source_repo="sample/mailbox",
            source_issue=7,
            comments=[_comment(_payload(repo="unknown/repo"))],
        )
    assert state.get_task(tmp_path / "state.db", "task-1") is None


def test_conflicting_event_replay_fails_closed(tmp_path: Path):
    kwargs = dict(
        db_path=tmp_path / "state.db",
        tasks_root=tmp_path / "tasks",
        allowlist=_allowlist(tmp_path),
        source_repo="sample/mailbox",
        source_issue=7,
    )
    ingest_comments(comments=[_comment(_payload(goal="first"))], **kwargs)
    with pytest.raises(GitHubTransportError, match="conflicting"):
        ingest_comments(comments=[_comment(_payload(goal="changed"))], **kwargs)


def test_publish_redacts_local_paths_and_is_idempotent(tmp_path: Path):
    db = tmp_path / "state.db"
    tasks_root = tmp_path / "tasks"
    ingest_comments(
        db_path=db,
        tasks_root=tasks_root,
        allowlist=_allowlist(tmp_path),
        source_repo="sample/mailbox",
        source_issue=7,
        comments=[_comment(_payload())],
    )

    result_path = tmp_path / "result.json"
    checkpoint_path = tmp_path / "checkpoint.json"
    envelopes.atomic_write_json(
        result_path,
        envelopes.result_envelope(
            task_id="task-1",
            status="SUCCESS",
            head="abc123",
            changed_files=["sample.py"],
            commands_run=["pytest"],
            tests=["1 passed"],
            first_error=r"see C:\Users\Example\private\file.txt",
            git_status="clean",
            blocker="",
            artifacts=[],
            next_recommended_action="done",
        ),
    )
    envelopes.atomic_write_json(
        checkpoint_path,
        envelopes.checkpoint_envelope(
            task_id="task-1",
            goal="Fix the sample",
            decisions=[],
            verified=["tests pass"],
            changed=["sample.py"],
            failed_attempts=[],
            do_not_repeat=[],
            remaining=[],
            next_action="done",
            evidence_refs=[],
        ),
    )
    state.update_task_refs(
        db,
        "task-1",
        result_envelope_ref=str(result_path),
        checkpoint_ref=str(checkpoint_path),
    )

    calls: list[list[str]] = []

    def runner(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(
            args,
            0,
            stdout=json.dumps({"id": 99, "html_url": "https://github.example/result/99"}),
            stderr="",
        )

    first = publish_result(task_id="task-1", db_path=db, runner=runner)
    second = publish_result(task_id="task-1", db_path=db, runner=runner)

    assert first.replay is False
    assert second.replay is True
    assert len(calls) == 1
    posted = next(part for part in calls[0] if part.startswith("body="))[5:]
    assert "BRIDGE_RESULT_V1" in posted
    assert "C:\\Users\\Example" not in posted
    assert "[LOCAL_PATH_REDACTED]" in posted
