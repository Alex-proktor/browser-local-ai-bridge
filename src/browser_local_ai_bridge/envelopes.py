from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
MAX_TEXT = 1200
MAX_LIST_ITEMS = 50
MAX_JSON_BYTES = 64 * 1024
REDACTED = "[REDACTED]"

TASK_SCHEMA = "BRIDGE_TASK_V1"
RESULT_SCHEMA = "BRIDGE_RESULT_V1"
CHECKPOINT_SCHEMA = "BRIDGE_CHECKPOINT_V1"

REQUIRED_EXECUTION_RESULT_FIELDS = (
    "status",
    "changed_files",
    "commands_run",
    "tests",
    "first_error",
    "git_status",
    "blocker",
    "next_recommended_action",
)

_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(authorization|cookie|api[_-]?key|password|passwd|secret|token)\b\s*[:=]"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)


class EnvelopeError(ValueError):
    pass


def _clean_text(value: Any, limit: int = MAX_TEXT) -> str:
    text = " ".join(str(value or "").split())[:limit]
    if text and any(pattern.search(text) for pattern in _SECRET_PATTERNS):
        return REDACTED
    return text


def _clean_list(value: Any, limit: int = MAX_LIST_ITEMS) -> list[str]:
    if value is None or value == "":
        return []
    items = value if isinstance(value, list) else [value]
    result: list[str] = []
    for item in items[:limit]:
        cleaned = _clean_text(item)
        if cleaned:
            result.append(cleaned)
    return result


def safe_task_dir_name(task_id: str) -> str:
    raw = str(task_id or "").strip()
    if not raw:
        raise EnvelopeError("task_id is required")
    prefix = re.sub(r"[^A-Za-z0-9._-]+", "-", raw)[:48].strip(".-") or "task"
    digest = hashlib.sha256(raw.encode("utf-8", "ignore")).hexdigest()[:12]
    return f"{prefix}-{digest}"


def task_dir(root: Path, task_id: str) -> Path:
    base = root.resolve()
    path = (base / safe_task_dir_name(task_id)).resolve()
    if base != path and base not in path.parents:
        raise EnvelopeError("task directory escapes tasks root")
    return path


def task_envelope(**fields: Any) -> dict[str, Any]:
    return {
        "schema": TASK_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "task_id": _clean_text(fields.get("task_id"), 300),
        "goal": _clean_text(fields.get("goal")),
        "repo": _clean_text(fields.get("repo"), 500),
        "branch": _clean_text(fields.get("branch"), 200),
        "why_local_required": _clean_text(fields.get("why_local_required")),
        "allowed_actions": _clean_list(fields.get("allowed_actions")),
        "forbidden_actions": _clean_list(fields.get("forbidden_actions")),
        "known_facts": _clean_list(fields.get("known_facts")),
        "do_not_repeat": _clean_list(fields.get("do_not_repeat")),
        "expected_output": _clean_text(fields.get("expected_output")),
    }


def result_envelope(**fields: Any) -> dict[str, Any]:
    return {
        "schema": RESULT_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "task_id": _clean_text(fields.get("task_id"), 300),
        "status": _clean_text(fields.get("status"), 80),
        "head": _clean_text(fields.get("head"), 200),
        "changed_files": _clean_list(fields.get("changed_files")),
        "commands_run": _clean_list(fields.get("commands_run")),
        "tests": _clean_list(fields.get("tests")),
        "first_error": _clean_text(fields.get("first_error")),
        "git_status": _clean_text(fields.get("git_status")),
        "blocker": _clean_text(fields.get("blocker")),
        "artifacts": _clean_list(fields.get("artifacts")),
        "next_recommended_action": _clean_text(fields.get("next_recommended_action")),
        "requested_model": _clean_text(fields.get("requested_model"), 200),
        "attempted_models": _clean_list(fields.get("attempted_models")),
        "effective_model": _clean_text(fields.get("effective_model"), 200),
        "fallback_reason": _clean_text(fields.get("fallback_reason")),
    }


def checkpoint_envelope(**fields: Any) -> dict[str, Any]:
    return {
        "schema": CHECKPOINT_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "task_id": _clean_text(fields.get("task_id"), 300),
        "goal": _clean_text(fields.get("goal")),
        "decisions": _clean_list(fields.get("decisions")),
        "verified": _clean_list(fields.get("verified")),
        "changed": _clean_list(fields.get("changed")),
        "failed_attempts": _clean_list(fields.get("failed_attempts")),
        "do_not_repeat": _clean_list(fields.get("do_not_repeat")),
        "remaining": _clean_list(fields.get("remaining")),
        "next_action": _clean_text(fields.get("next_action")),
        "evidence_refs": _clean_list(fields.get("evidence_refs")),
    }


def parse_execution_result(text: str) -> dict[str, Any]:
    raw = str(text or "").strip()
    if not raw:
        raise EnvelopeError("execution result is empty")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise EnvelopeError("execution result is not valid JSON") from exc
    if not isinstance(value, dict):
        raise EnvelopeError("execution result must be a JSON object")
    missing = [field for field in REQUIRED_EXECUTION_RESULT_FIELDS if field not in value]
    if missing:
        raise EnvelopeError("execution result missing fields: " + ",".join(missing))
    for field in ("changed_files", "commands_run", "tests"):
        if not isinstance(value.get(field), list):
            raise EnvelopeError(f"execution result field must be an array: {field}")
    for field in ("status", "first_error", "git_status", "blocker", "next_recommended_action"):
        if not isinstance(value.get(field), str):
            raise EnvelopeError(f"execution result field must be a string: {field}")
    return value


def dumps_envelope(value: dict[str, Any]) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if len(raw.encode("utf-8")) > MAX_JSON_BYTES:
        raise EnvelopeError("envelope exceeds size limit")
    return raw


def validate_envelope(value: Any, expected_schema: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise EnvelopeError("envelope must be a JSON object")
    if value.get("schema") != expected_schema:
        raise EnvelopeError(f"unexpected envelope schema: {value.get('schema')!r}")
    version = value.get("schema_version")
    if version != SCHEMA_VERSION:
        raise EnvelopeError(f"unsupported envelope schema_version: {version}")
    task_id = value.get("task_id")
    if not isinstance(task_id, str) or not task_id.strip():
        raise EnvelopeError("task_id is required")
    return value


def atomic_write_json(path: Path, value: dict[str, Any]) -> Path:
    raw = dumps_envelope(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(raw)
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
    return path


def load_json(path: Path, expected_schema: str) -> dict[str, Any]:
    if path.stat().st_size > MAX_JSON_BYTES:
        raise EnvelopeError("envelope file exceeds size limit")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EnvelopeError("invalid JSON envelope") from exc
    return validate_envelope(value, expected_schema)
