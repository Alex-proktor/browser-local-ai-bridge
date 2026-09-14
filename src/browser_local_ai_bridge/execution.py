from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from . import envelopes, runtime, state
from .config import RepoTarget, resolve_repo
from .executors.base import ExecutionOutcome, Executor

BranchReader = Callable[[Path], str]


class ExecutionError(ValueError):
    pass


def _git_branch(checkout: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=str(checkout),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ExecutionError("unable to inspect checkout branch") from exc
    if completed.returncode != 0:
        raise ExecutionError("unable to inspect checkout branch")
    return (completed.stdout or "").strip()


def _validate_checkout(target: RepoTarget, expected_branch: str, branch_reader: BranchReader) -> Path:
    checkout = target.checkout_path.resolve()
    if not checkout.is_dir():
        raise ExecutionError("allowed checkout does not exist")
    if not (checkout / ".git").exists():
        raise ExecutionError("allowed checkout is not a git checkout")
    expected = str(expected_branch or "").strip()
    if expected:
        current = branch_reader(checkout)
        if current != expected:
            raise ExecutionError(f"checkout branch mismatch: expected {expected}, got {current or '<detached>'}")
    return checkout


def _lock_name(checkout: Path) -> str:
    digest = hashlib.sha256(str(checkout.resolve()).encode("utf-8", "ignore")).hexdigest()
    return f"{digest}.lock"


@contextmanager
def checkout_lock(lock_root: Path, checkout: Path, *, task_id: str, run_id: str) -> Iterator[Path]:
    lock_root.mkdir(parents=True, exist_ok=True)
    path = lock_root / _lock_name(checkout)
    payload = json.dumps(
        {"task_id": task_id, "run_id": run_id, "created_at": time.time()},
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise ExecutionError("checkout is already claimed by another execution") from exc
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
        yield path
    finally:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _load_canonical_task(task_record: dict[str, Any], tasks_root: Path) -> dict[str, Any]:
    task_id = str(task_record.get("task_id") or "")
    ref = str(task_record.get("task_envelope_ref") or "").strip()
    if not ref:
        raise ExecutionError("task envelope ref is missing")
    path = Path(ref)
    expected_dir = envelopes.task_dir(tasks_root, task_id)
    if path.resolve().parent != expected_dir.resolve() or path.name != "task.json":
        raise ExecutionError("task envelope ref is outside canonical task directory")
    try:
        task = envelopes.load_json(path, envelopes.TASK_SCHEMA)
    except (OSError, envelopes.EnvelopeError) as exc:
        raise ExecutionError("canonical task envelope is invalid") from exc
    if task.get("task_id") != task_id:
        raise ExecutionError("task envelope task_id mismatch")
    if task.get("repo") != task_record.get("repo"):
        raise ExecutionError("task envelope repo mismatch")
    if str(task.get("branch") or "") != str(task_record.get("branch") or ""):
        raise ExecutionError("task envelope branch mismatch")
    return task


def _result_and_checkpoint(
    *,
    task: dict[str, Any],
    outcome: ExecutionOutcome,
) -> tuple[dict[str, Any], dict[str, Any]]:
    task_id = str(task["task_id"])
    parsed = outcome.result if outcome.result else None
    if parsed is not None:
        status = outcome.status if outcome.status in {"SUCCESS", "FAILED", "INTERRUPTED"} else "FAILED"
        result = envelopes.result_envelope(
            task_id=task_id,
            status=status,
            head=parsed.get("head"),
            changed_files=parsed.get("changed_files"),
            commands_run=parsed.get("commands_run"),
            tests=parsed.get("tests"),
            first_error=parsed.get("first_error"),
            git_status=parsed.get("git_status"),
            blocker=parsed.get("blocker"),
            artifacts=parsed.get("artifacts"),
            next_recommended_action=parsed.get("next_recommended_action"),
        )
        checkpoint = envelopes.checkpoint_envelope(
            task_id=task_id,
            goal=task.get("goal"),
            decisions=[],
            verified=parsed.get("tests"),
            changed=parsed.get("changed_files"),
            failed_attempts=[parsed.get("first_error")] if parsed.get("first_error") else [],
            do_not_repeat=task.get("do_not_repeat"),
            remaining=[parsed.get("blocker")] if parsed.get("blocker") else [],
            next_action=parsed.get("next_recommended_action"),
            evidence_refs=[],
        )
        return result, checkpoint

    status = "INTERRUPTED" if outcome.status == "INTERRUPTED" else "FAILED"
    message = str(outcome.error or outcome.error_type or status)
    result = envelopes.result_envelope(
        task_id=task_id,
        status=status,
        head="",
        changed_files=[],
        commands_run=[],
        tests=[],
        first_error=message,
        git_status="",
        blocker=message,
        artifacts=[],
        next_recommended_action="Inspect the execution failure before retrying.",
    )
    checkpoint = envelopes.checkpoint_envelope(
        task_id=task_id,
        goal=task.get("goal"),
        decisions=[],
        verified=[],
        changed=[],
        failed_attempts=[message],
        do_not_repeat=task.get("do_not_repeat"),
        remaining=[task.get("expected_output")],
        next_action="Inspect the execution failure before retrying.",
        evidence_refs=[],
    )
    return result, checkpoint


def execute_task(
    *,
    db_path: Path,
    tasks_root: Path,
    lock_root: Path,
    task_id: str,
    allowlist: dict[str, RepoTarget],
    executor: Executor,
    branch_reader: BranchReader = _git_branch,
) -> ExecutionOutcome:
    record = state.get_task(db_path, task_id)
    if record is None:
        raise ExecutionError("task not found")
    if record["status"] != "READY":
        raise ExecutionError(f"task is not READY: {record['status']}")
    task = _load_canonical_task(record, tasks_root)
    target = resolve_repo(str(task["repo"]), allowlist)
    checkout = _validate_checkout(target, str(task.get("branch") or ""), branch_reader)
    run_id = uuid.uuid4().hex

    with checkout_lock(lock_root, checkout, task_id=task_id, run_id=run_id):
        state.transition(
            db_path,
            task_id=task_id,
            event_id=f"execution:{run_id}:start",
            to_status="RUNNING",
            outcome=executor.name,
        )
        runtime.start_run(db_path, run_id=run_id, task_id=task_id, executor=executor.name)
        observer = runtime.ExecutionObserver(db_path, run_id)
        try:
            outcome = executor.execute(task=task, checkout=checkout, observer=observer)
        except Exception as exc:
            outcome = ExecutionOutcome(
                status="FAILED",
                error_type="executor_exception",
                error=str(exc)[:1200],
                executor=executor.name,
            )

        current = state.get_task(db_path, task_id)
        if current is not None and current["status"] == "CANCELLED":
            runtime.finish_run(db_path, run_id, outcome="CANCELLED", error_type="user_cancelled")
            return ExecutionOutcome(
                status="INTERRUPTED",
                error_type="user_cancelled",
                error="execution cancelled",
                executor=executor.name,
            )

        result, checkpoint = _result_and_checkpoint(task=task, outcome=outcome)
        task_dir = envelopes.task_dir(tasks_root, task_id)
        result_path = envelopes.atomic_write_json(task_dir / "result.json", result)
        checkpoint_path = envelopes.atomic_write_json(task_dir / "checkpoint.json", checkpoint)

        if outcome.status == "SUCCESS":
            final_status = "WAITING_CONTROLLER"
        elif outcome.status == "INTERRUPTED":
            final_status = "INTERRUPTED"
        else:
            final_status = "FAILED"
        try:
            state.transition(
                db_path,
                task_id=task_id,
                event_id=f"execution:{run_id}:finish",
                to_status=final_status,
                outcome=outcome.error_type or outcome.status,
                evidence_ref=str(result_path),
            )
        except state.InvalidTransition:
            current = state.get_task(db_path, task_id)
            if current is None or current["status"] != "CANCELLED":
                raise
            result_path.unlink(missing_ok=True)
            checkpoint_path.unlink(missing_ok=True)
            runtime.finish_run(db_path, run_id, outcome="CANCELLED", error_type="user_cancelled")
            return ExecutionOutcome(
                status="INTERRUPTED",
                error_type="user_cancelled",
                error="execution cancelled",
                executor=executor.name,
            )
        current = state.get_task(db_path, task_id)
        if current is not None and current["status"] == "CANCELLED":
            result_path.unlink(missing_ok=True)
            checkpoint_path.unlink(missing_ok=True)
            runtime.finish_run(db_path, run_id, outcome="CANCELLED", error_type="user_cancelled")
            return ExecutionOutcome(
                status="INTERRUPTED",
                error_type="user_cancelled",
                error="execution cancelled",
                executor=executor.name,
            )
        state.update_task_refs(
            db_path,
            task_id,
            result_envelope_ref=str(result_path),
            checkpoint_ref=str(checkpoint_path),
        )
        runtime.finish_run(
            db_path,
            run_id,
            outcome=outcome.status,
            error_type=outcome.error_type,
            result_ref=str(result_path),
        )
        return outcome
