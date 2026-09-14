from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import github_transport, runtime, state
from .config import RepoTarget, load_repo_allowlist
from .execution import execute_task
from .executors import CodexExecutor
from .executors.base import Executor


class WorkerConfigError(ValueError):
    pass


@dataclass(frozen=True)
class MailboxSource:
    repo: str
    issue: int


@dataclass(frozen=True)
class WorkerConfig:
    sources: tuple[MailboxSource, ...]
    repos_path: Path = Path("repos.json")
    db_path: Path = Path("task-state.db")
    tasks_root: Path = Path("tasks")
    lock_root: Path = Path("locks")
    poll_seconds: float = 5.0
    executor_kind: str = "codex"
    executor_executable: str = "codex"
    timeout_seconds: int = 900
    max_deliveries_per_tick: int = 1


@dataclass(frozen=True)
class TickResult:
    recovered: tuple[dict[str, Any], ...]
    ingested: tuple[str, ...]
    executed: str
    published: tuple[str, ...]
    errors: tuple[str, ...]


def load_worker_config(path: Path) -> WorkerConfig:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise WorkerConfigError(f"worker config not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise WorkerConfigError("worker config is not valid JSON") from exc
    if not isinstance(raw, dict) or raw.get("version") != 1:
        raise WorkerConfigError("unsupported worker config format")
    sources_raw = raw.get("sources")
    if not isinstance(sources_raw, list) or not sources_raw:
        raise WorkerConfigError("worker config requires non-empty sources")
    runtime_raw = raw.get("runtime")
    if not isinstance(runtime_raw, dict):
        raise WorkerConfigError("worker config requires runtime paths")
    base = path.parent.resolve()
    runtime_paths: dict[str, Path] = {}
    for key in ("repos", "db", "tasks", "locks"):
        value = runtime_raw.get(key)
        if not isinstance(value, str) or not value.strip():
            raise WorkerConfigError(f"worker runtime path is required: {key}")
        candidate = Path(value).expanduser()
        runtime_paths[key] = (candidate if candidate.is_absolute() else base / candidate).resolve()
    sources: list[MailboxSource] = []
    for item in sources_raw:
        if not isinstance(item, dict):
            raise WorkerConfigError("worker source must be an object")
        repo = " ".join(str(item.get("repo") or "").split())
        try:
            issue = int(item.get("issue") or 0)
        except (TypeError, ValueError) as exc:
            raise WorkerConfigError("worker source issue is invalid") from exc
        if not repo or issue <= 0:
            raise WorkerConfigError("worker source requires repo and issue")
        sources.append(MailboxSource(repo=repo, issue=issue))
    executor = raw.get("executor") or {}
    if not isinstance(executor, dict):
        raise WorkerConfigError("worker executor config must be an object")
    kind = str(executor.get("kind") or "codex").strip().lower()
    if kind != "codex":
        raise WorkerConfigError(f"unsupported worker executor: {kind}")
    return WorkerConfig(
        sources=tuple(sources),
        repos_path=runtime_paths["repos"],
        db_path=runtime_paths["db"],
        tasks_root=runtime_paths["tasks"],
        lock_root=runtime_paths["locks"],
        poll_seconds=max(0.1, float(raw.get("poll_seconds") or 5.0)),
        executor_kind=kind,
        executor_executable=str(executor.get("executable") or "codex"),
        timeout_seconds=max(30, min(int(executor.get("timeout_seconds") or 900), 3600)),
        max_deliveries_per_tick=max(1, min(int(raw.get("max_deliveries_per_tick") or 1), 20)),
    )


def build_executor(config: WorkerConfig) -> Executor:
    if config.executor_kind != "codex":
        raise WorkerConfigError(f"unsupported worker executor: {config.executor_kind}")
    return CodexExecutor(
        executable=config.executor_executable,
        timeout_seconds=config.timeout_seconds,
    )


def _ready_task(db_path: Path) -> dict[str, Any] | None:
    tasks = state.list_tasks(db_path, statuses={"READY"}, limit=1)
    return tasks[0] if tasks else None


def _pending_delivery_tasks(db_path: Path, *, limit: int) -> list[dict[str, Any]]:
    terminal = {"WAITING_CONTROLLER", "FAILED", "INTERRUPTED"}
    candidates = state.list_tasks(db_path, statuses=terminal, limit=max(limit * 4, limit))
    pending: list[dict[str, Any]] = []
    for task in candidates:
        if not task.get("result_envelope_ref"):
            continue
        delivery = state.get_transport_delivery(db_path, str(task["task_id"]))
        if delivery is None or delivery.get("outcome") == "SUCCESS":
            continue
        pending.append(task)
        if len(pending) >= limit:
            break
    return pending


def worker_tick(
    *,
    db_path: Path,
    tasks_root: Path,
    lock_root: Path,
    allowlist: dict[str, RepoTarget],
    config: WorkerConfig,
    executor: Executor,
    ingest_fn: Callable[..., Any] = github_transport.ingest_issue,
    publish_fn: Callable[..., Any] = github_transport.publish_result,
    branch_reader: Callable[[Path], str] | None = None,
) -> TickResult:
    recovered = tuple(runtime.reconcile_running(db_path))
    ingested: list[str] = []
    errors: list[str] = []
    for source in config.sources:
        try:
            result = ingest_fn(
                repo=source.repo,
                issue=source.issue,
                db_path=db_path,
                tasks_root=tasks_root,
                allowlist=allowlist,
            )
            if not bool(getattr(result, "replay", False)):
                ingested.append(str(result.task_id))
        except github_transport.GitHubTransportError as exc:
            if str(exc) != "no new task envelope found":
                errors.append(f"ingest:{source.repo}#{source.issue}:{exc}")

    executed = ""
    ready = _ready_task(db_path)
    if ready is not None:
        task_id = str(ready["task_id"])
        try:
            kwargs: dict[str, Any] = {
                "db_path": db_path,
                "tasks_root": tasks_root,
                "lock_root": lock_root,
                "task_id": task_id,
                "allowlist": allowlist,
                "executor": executor,
            }
            if branch_reader is not None:
                kwargs["branch_reader"] = branch_reader
            execute_task(**kwargs)
            executed = task_id
        except Exception as exc:
            errors.append(f"execute:{task_id}:{type(exc).__name__}:{exc}")

    published: list[str] = []
    for task in _pending_delivery_tasks(db_path, limit=config.max_deliveries_per_tick):
        task_id = str(task["task_id"])
        try:
            publish_fn(task_id=task_id, db_path=db_path)
            published.append(task_id)
        except github_transport.GitHubTransportError as exc:
            errors.append(f"publish:{task_id}:{exc}")

    return TickResult(
        recovered=recovered,
        ingested=tuple(ingested),
        executed=executed,
        published=tuple(published),
        errors=tuple(errors),
    )


def run_worker(
    *,
    config: WorkerConfig,
    stop_event: threading.Event,
    executor: Executor | None = None,
    tick_fn: Callable[..., TickResult] = worker_tick,
) -> None:
    allowlist = load_repo_allowlist(config.repos_path)
    selected_executor = executor or build_executor(config)
    while not stop_event.is_set():
        tick_fn(
            db_path=config.db_path,
            tasks_root=config.tasks_root,
            lock_root=config.lock_root,
            allowlist=allowlist,
            config=config,
            executor=selected_executor,
        )
        stop_event.wait(config.poll_seconds)
