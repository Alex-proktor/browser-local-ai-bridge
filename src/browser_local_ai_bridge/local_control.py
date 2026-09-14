from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import envelopes, runtime, state
from .config import RepoTarget, default_home, load_repo_allowlist, resolve_repo
from .execution import BranchReader, _git_branch, _lock_name, _validate_checkout, execute_task
from .executors.base import Executor


class LocalControlError(ValueError):
    pass


@dataclass(frozen=True)
class RuntimePaths:
    home: Path
    repos: Path
    db: Path
    tasks: Path
    locks: Path

    @classmethod
    def from_home(cls, home: Path | None = None) -> "RuntimePaths":
        root = (home or default_home()).expanduser().resolve()
        return cls(root, root / "repos.json", root / "task-state.db", root / "tasks", root / "locks")


def _canonical_task(value: Any) -> dict[str, Any]:
    raw = envelopes.validate_envelope(value, envelopes.TASK_SCHEMA)
    task = envelopes.task_envelope(**raw)
    for field in ("goal", "repo", "why_local_required", "expected_output"):
        if not task.get(field):
            raise LocalControlError(f"task field is required: {field}")
    for field in ("allowed_actions", "forbidden_actions"):
        if not task.get(field):
            raise LocalControlError(f"task field is required: {field}")
    return task


def _load_optional_envelope(ref: str, schema: str) -> dict[str, Any] | None:
    if not ref:
        return None
    try:
        return envelopes.load_json(Path(ref), schema)
    except (OSError, envelopes.EnvelopeError) as exc:
        raise LocalControlError("stored envelope is invalid") from exc


class DirectLocalControl:
    def __init__(
        self,
        *,
        paths: RuntimePaths | None = None,
        executor: Executor | None = None,
        branch_reader: BranchReader = _git_branch,
    ) -> None:
        self.paths = paths or RuntimePaths.from_home()
        self.executor = executor
        self.branch_reader = branch_reader

    def _allowlist(self) -> dict[str, RepoTarget]:
        return load_repo_allowlist(self.paths.repos)

    def health(self) -> dict[str, Any]:
        allowlist = self._allowlist()
        state.ensure_schema(self.paths.db)
        return {
            "status": "PASS",
            "mode": "direct-local",
            "authorized_repo_count": len(allowlist),
            "executor_configured": self.executor is not None,
        }

    def list_repos(self) -> dict[str, Any]:
        return {"repos": sorted(self._allowlist())}

    def validate_task(self, value: Any) -> dict[str, Any]:
        task = _canonical_task(value)
        target = resolve_repo(str(task["repo"]), self._allowlist())
        _validate_checkout(target, str(task.get("branch") or ""), self.branch_reader)
        return {
            "valid": True,
            "task_id": task["task_id"],
            "repo": task["repo"],
            "branch": task["branch"],
        }

    def submit(self, value: Any) -> dict[str, Any]:
        if self.executor is None:
            raise LocalControlError("executor is not configured")
        task = _canonical_task(value)
        allowlist = self._allowlist()
        target = resolve_repo(str(task["repo"]), allowlist)
        _validate_checkout(target, str(task.get("branch") or ""), self.branch_reader)
        task_id = str(task["task_id"])
        task_path = envelopes.task_dir(self.paths.tasks, task_id) / "task.json"
        if state.get_task(self.paths.db, task_id) is not None:
            raise LocalControlError(f"task already exists: {task_id}")
        envelopes.atomic_write_json(task_path, task)
        state.create_task(
            self.paths.db,
            task_id=task_id,
            status="QUEUED",
            title=str(task.get("expected_output") or task_id),
            goal=str(task.get("goal") or ""),
            repo=str(task.get("repo") or ""),
            branch=str(task.get("branch") or ""),
            next_action=str(task.get("expected_output") or ""),
            task_envelope_ref=str(task_path),
        )
        state.transition(
            self.paths.db,
            task_id=task_id,
            event_id=f"direct-local:{task_id}:ready",
            to_status="READY",
            outcome="direct-local",
        )
        outcome = execute_task(
            db_path=self.paths.db,
            tasks_root=self.paths.tasks,
            lock_root=self.paths.locks,
            task_id=task_id,
            allowlist=allowlist,
            executor=self.executor,
            branch_reader=self.branch_reader,
        )
        inspected = self.inspect(task_id)
        inspected["execution"] = {
            "status": outcome.status,
            "executor": outcome.executor,
            "duration_ms": outcome.duration_ms,
            "error_type": outcome.error_type,
        }
        return inspected

    def task_status(self, task_id: str) -> dict[str, Any]:
        return runtime.status(self.paths.db, task_id)

    def inspect(self, task_id: str) -> dict[str, Any]:
        record = state.get_task(self.paths.db, task_id)
        if record is None:
            raise LocalControlError("task not found")
        task = _load_optional_envelope(str(record.get("task_envelope_ref") or ""), envelopes.TASK_SCHEMA)
        result = _load_optional_envelope(str(record.get("result_envelope_ref") or ""), envelopes.RESULT_SCHEMA)
        checkpoint = _load_optional_envelope(str(record.get("checkpoint_ref") or ""), envelopes.CHECKPOINT_SCHEMA)
        return {
            "task_id": task_id,
            "status": record["status"],
            "repo": record["repo"],
            "branch": record["branch"],
            "task": task,
            "result": result,
            "checkpoint": checkpoint,
            "runtime": runtime.status(self.paths.db, task_id),
        }

    def cancel(self, task_id: str) -> dict[str, Any]:
        record = state.get_task(self.paths.db, task_id)
        if record is None:
            raise LocalControlError("task not found")
        status = str(record["status"])
        if status == "CANCELLED":
            return {"task_id": task_id, "status": status, "replay": True}
        if status == "RUNNING":
            result = runtime.cancel_active(self.paths.db, task_id)
            if not result.get("cancelled"):
                raise LocalControlError(f"active cancellation failed: {result.get('reason', 'unknown')}")
            target = resolve_repo(str(record.get("repo") or ""), self._allowlist())
            (self.paths.locks / _lock_name(target.checkout_path)).unlink(missing_ok=True)
            return {
                "task_id": task_id,
                "status": result["status"],
                "replay": False,
                "reason": result["reason"],
            }
        if status == "COMPLETED":
            raise LocalControlError("completed task cannot be cancelled")
        if "CANCELLED" not in state.LEGAL_TRANSITIONS.get(status, set()):
            raise LocalControlError(f"task cannot be cancelled from status: {status}")
        updated = state.transition(
            self.paths.db,
            task_id=task_id,
            event_id=f"direct-local:{task_id}:cancel",
            to_status="CANCELLED",
            outcome="direct-local-cancel",
        )
        return {"task_id": task_id, "status": updated["status"], "replay": False}
