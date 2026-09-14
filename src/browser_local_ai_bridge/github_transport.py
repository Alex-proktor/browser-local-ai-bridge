from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import envelopes, state
from .config import RepoTarget, resolve_repo

TASK_MARKER = envelopes.TASK_SCHEMA
RESULT_MARKER = envelopes.RESULT_SCHEMA
TRANSPORT_VERSION = 1
MAX_COMMENT_BYTES = 128 * 1024
TRANSPORT_KIND = "github-issues"

GhRunner = Callable[..., subprocess.CompletedProcess[str]]

_WINDOWS_ABS = re.compile(r"\b[A-Za-z]:[\\/][^\s\"']+")
_POSIX_ABS = re.compile(r"(?<![\w:])/(?:Users|home|workspace|tmp|var|etc|mnt|opt)/[^\s\"']+")


class GitHubTransportError(ValueError):
    pass


@dataclass(frozen=True)
class IngestResult:
    task_id: str
    event_id: str
    source_comment_id: str
    replay: bool = False


@dataclass(frozen=True)
class PublishResult:
    task_id: str
    event_id: str
    remote_id: str
    remote_ref: str
    replay: bool = False


def is_task_candidate(text: str) -> bool:
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if stripped:
            return stripped == TASK_MARKER
    return False


def extract_payload(text: str) -> dict[str, Any]:
    raw = str(text or "")
    if len(raw.encode("utf-8")) > MAX_COMMENT_BYTES:
        raise GitHubTransportError("task comment exceeds size limit")
    if not is_task_candidate(raw):
        raise GitHubTransportError("missing top-level task marker")
    after = raw.split(TASK_MARKER, 1)[1].strip()
    if after.startswith("```"):
        lines = after.splitlines()
        if not lines:
            raise GitHubTransportError("missing task payload")
        lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        after = "\n".join(lines).strip()
        if after.startswith("json"):
            after = after[4:].lstrip()
    try:
        value = json.loads(after)
    except json.JSONDecodeError as exc:
        raise GitHubTransportError("malformed task JSON") from exc
    if not isinstance(value, dict):
        raise GitHubTransportError("task transport payload must be an object")
    return value


def _payload_hash(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _clean_event_id(value: Any) -> str:
    event_id = " ".join(str(value or "").split())[:300]
    if not event_id:
        raise GitHubTransportError("event_id is required")
    return event_id


def _validate_task_payload(
    payload: dict[str, Any],
    allowlist: dict[str, RepoTarget],
) -> tuple[str, dict[str, Any], str, int]:
    if payload.get("schema") != TASK_MARKER:
        raise GitHubTransportError("invalid task transport schema")
    if payload.get("version") != TRANSPORT_VERSION:
        raise GitHubTransportError("unsupported task transport version")
    event_id = _clean_event_id(payload.get("event_id"))
    raw_task = envelopes.validate_envelope(payload.get("task"), envelopes.TASK_SCHEMA)
    task = envelopes.task_envelope(**raw_task)
    for field in ("goal", "repo", "why_local_required", "expected_output"):
        if not task.get(field):
            raise GitHubTransportError(f"task field is required: {field}")
    for field in ("allowed_actions", "forbidden_actions"):
        if not task.get(field):
            raise GitHubTransportError(f"task field is required: {field}")
    resolve_repo(str(task["repo"]), allowlist)

    transport = payload.get("transport")
    if not isinstance(transport, dict):
        raise GitHubTransportError("transport result target is required")
    result_repo = " ".join(str(transport.get("result_repo") or "").split())[:300]
    try:
        result_issue = int(transport.get("result_issue") or 0)
    except (TypeError, ValueError) as exc:
        raise GitHubTransportError("invalid result issue") from exc
    if not result_repo or result_issue <= 0:
        raise GitHubTransportError("transport result target is required")
    return event_id, task, result_repo, result_issue


def _source_ref(repo: str, issue: int, comment: dict[str, Any]) -> str:
    remote = str(comment.get("html_url") or comment.get("url") or "")[:500]
    if remote:
        return remote
    comment_id = str(comment.get("id") or "")[:100]
    return f"github://{repo}/issues/{int(issue)}/comments/{comment_id}"


def _task_path(tasks_root: Path, task_id: str) -> Path:
    return envelopes.task_dir(tasks_root, task_id) / "task.json"


def ingest_comments(
    *,
    db_path: Path,
    tasks_root: Path,
    allowlist: dict[str, RepoTarget],
    source_repo: str,
    source_issue: int,
    comments: list[dict[str, Any]],
) -> IngestResult:
    state.ensure_schema(db_path)
    replay: IngestResult | None = None
    for comment in comments:
        body = str(comment.get("body") or "")
        if not is_task_candidate(body):
            continue
        payload = extract_payload(body)
        event_id, task, result_repo, result_issue = _validate_task_payload(payload, allowlist)
        task_id = str(task["task_id"])
        source_ref = _source_ref(source_repo, source_issue, comment)
        fingerprint = _payload_hash(payload)

        existing_ingest = state.get_transport_ingest(db_path, event_id)
        if existing_ingest is not None:
            expected = (task_id, TRANSPORT_KIND, source_ref, fingerprint)
            actual = (
                existing_ingest["task_id"],
                existing_ingest["transport_kind"],
                existing_ingest["source_ref"],
                existing_ingest["payload_hash"],
            )
            if actual != expected:
                raise GitHubTransportError(f"conflicting task event replay: {event_id}")
            if existing_ingest["outcome"] == "SUCCESS":
                replay = IngestResult(
                    task_id=task_id,
                    event_id=event_id,
                    source_comment_id=str(comment.get("id") or ""),
                    replay=True,
                )
                continue

        existing_task = state.get_task(db_path, task_id)
        if existing_task is not None and existing_ingest is None:
            raise GitHubTransportError(f"task_id already exists from another event: {task_id}")

        path = _task_path(tasks_root, task_id)
        envelopes.atomic_write_json(path, task)
        if existing_task is None:
            state.create_task(
                db_path,
                task_id=task_id,
                title=str(task.get("expected_output") or task_id),
                goal=str(task.get("goal") or ""),
                repo=str(task.get("repo") or ""),
                branch=str(task.get("branch") or ""),
                next_action=str(task.get("expected_output") or ""),
                task_envelope_ref=str(path),
            )
        else:
            state.update_task_refs(db_path, task_id, task_envelope_ref=str(path))

        target_ref = f"github://{result_repo}/issues/{result_issue}"
        state.set_transport_delivery_target(
            db_path,
            task_id=task_id,
            transport_kind=TRANSPORT_KIND,
            target_ref=target_ref,
            event_id=f"{event_id}:result",
        )
        current = state.get_task(db_path, task_id)
        assert current is not None
        if current["status"] == "QUEUED":
            state.transition(
                db_path,
                task_id=task_id,
                event_id=f"{event_id}:ready",
                to_status="READY",
                evidence_ref=source_ref,
            )
        elif current["status"] != "READY":
            raise GitHubTransportError(f"task replay has unexpected status: {current['status']}")
        state.record_transport_ingest(
            db_path,
            event_id=event_id,
            task_id=task_id,
            transport_kind=TRANSPORT_KIND,
            source_ref=source_ref,
            payload_hash=fingerprint,
            outcome="SUCCESS",
        )
        return IngestResult(
            task_id=task_id,
            event_id=event_id,
            source_comment_id=str(comment.get("id") or ""),
        )
    if replay is not None:
        return replay
    raise GitHubTransportError("no new task envelope found")


def _run_gh(args: list[str], runner: GhRunner) -> subprocess.CompletedProcess[str]:
    try:
        completed = runner(args, capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitHubTransportError("gh command failed") from exc
    if completed.returncode != 0:
        raise GitHubTransportError("gh command failed")
    return completed


def fetch_issue_comments(repo: str, issue: int, *, runner: GhRunner = subprocess.run) -> list[dict[str, Any]]:
    completed = _run_gh(["gh", "api", f"repos/{repo}/issues/{int(issue)}/comments"], runner)
    try:
        value = json.loads(completed.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise GitHubTransportError("gh returned invalid JSON") from exc
    if not isinstance(value, list):
        raise GitHubTransportError("gh returned invalid comments payload")
    return [item for item in value if isinstance(item, dict)]


def ingest_issue(
    *,
    repo: str,
    issue: int,
    db_path: Path,
    tasks_root: Path,
    allowlist: dict[str, RepoTarget],
    runner: GhRunner = subprocess.run,
) -> IngestResult:
    return ingest_comments(
        db_path=db_path,
        tasks_root=tasks_root,
        allowlist=allowlist,
        source_repo=repo,
        source_issue=issue,
        comments=fetch_issue_comments(repo, issue, runner=runner),
    )


def _strip_local_paths(text: str) -> str:
    value = _WINDOWS_ABS.sub("[LOCAL_PATH_REDACTED]", str(text or ""))
    return _POSIX_ABS.sub("[LOCAL_PATH_REDACTED]", value)


def _public_value(value: Any) -> Any:
    if isinstance(value, str):
        text = _strip_local_paths(value)
        return envelopes._clean_text(text)
    if isinstance(value, list):
        return [_public_value(item) for item in value[: envelopes.MAX_LIST_ITEMS]]
    if isinstance(value, dict):
        return {str(key): _public_value(val) for key, val in sorted(value.items())}
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return envelopes._clean_text(_strip_local_paths(str(value)))


def build_result_payload(
    result: dict[str, Any],
    checkpoint: dict[str, Any] | None,
    *,
    event_id: str,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema": RESULT_MARKER,
        "version": TRANSPORT_VERSION,
        "event_id": _clean_event_id(event_id),
        "task_id": _public_value(result.get("task_id")),
        "result": _public_value(result),
    }
    if checkpoint is not None:
        payload["checkpoint"] = _public_value(checkpoint)
    return payload


def render_result_comment(payload: dict[str, Any]) -> str:
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
    if _WINDOWS_ABS.search(body) or _POSIX_ABS.search(body):
        raise GitHubTransportError("result payload contains local absolute path")
    rendered = f"{RESULT_MARKER}\n\n```json\n{body}\n```\n"
    if len(rendered.encode("utf-8")) > MAX_COMMENT_BYTES:
        raise GitHubTransportError("result comment exceeds size limit")
    return rendered


def _parse_target_ref(target_ref: str) -> tuple[str, int]:
    match = re.fullmatch(r"github://([^/]+/[^/]+)/issues/(\d+)", str(target_ref or ""))
    if not match:
        raise GitHubTransportError("invalid GitHub delivery target")
    return match.group(1), int(match.group(2))


def publish_result(
    *,
    task_id: str,
    db_path: Path,
    runner: GhRunner = subprocess.run,
) -> PublishResult:
    task = state.get_task(db_path, task_id)
    if task is None:
        raise GitHubTransportError("task not found")
    delivery = state.get_transport_delivery(db_path, task_id)
    if delivery is None or delivery["transport_kind"] != TRANSPORT_KIND:
        raise GitHubTransportError("GitHub delivery target not found")
    event_id = str(delivery["event_id"])
    if delivery["outcome"] == "SUCCESS" and delivery["remote_id"]:
        return PublishResult(
            task_id=task_id,
            event_id=event_id,
            remote_id=str(delivery["remote_id"]),
            remote_ref=str(delivery["remote_ref"]),
            replay=True,
        )

    result_ref = str(task.get("result_envelope_ref") or "")
    if not result_ref:
        raise GitHubTransportError("result envelope ref missing")
    result = envelopes.load_json(Path(result_ref), envelopes.RESULT_SCHEMA)
    checkpoint: dict[str, Any] | None = None
    checkpoint_ref = str(task.get("checkpoint_ref") or "")
    if checkpoint_ref:
        checkpoint = envelopes.load_json(Path(checkpoint_ref), envelopes.CHECKPOINT_SCHEMA)
    if result["task_id"] != task_id or (checkpoint is not None and checkpoint["task_id"] != task_id):
        raise GitHubTransportError("result/checkpoint task_id mismatch")

    repo, issue = _parse_target_ref(str(delivery["target_ref"]))
    body = render_result_comment(build_result_payload(result, checkpoint, event_id=event_id))
    try:
        completed = _run_gh(
            ["gh", "api", f"repos/{repo}/issues/{issue}/comments", "-f", f"body={body}"],
            runner,
        )
        remote = json.loads(completed.stdout or "{}")
        if not isinstance(remote, dict):
            raise GitHubTransportError("gh returned invalid publish response")
        remote_id = str(remote.get("id") or "")
        remote_ref = str(remote.get("html_url") or remote.get("url") or "")
        if not remote_id:
            raise GitHubTransportError("gh publish response has no comment id")
    except (json.JSONDecodeError, GitHubTransportError) as exc:
        state.record_transport_delivery(db_path, task_id=task_id, outcome="FAILED", last_error=str(exc))
        raise GitHubTransportError(str(exc)) from exc

    state.record_transport_delivery(
        db_path,
        task_id=task_id,
        outcome="SUCCESS",
        remote_id=remote_id,
        remote_ref=remote_ref,
    )
    return PublishResult(
        task_id=task_id,
        event_id=event_id,
        remote_id=remote_id,
        remote_ref=remote_ref,
    )
