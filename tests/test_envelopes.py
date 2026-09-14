from pathlib import Path

import pytest

from browser_local_ai_bridge.envelopes import (
    EnvelopeError,
    TASK_SCHEMA,
    dumps_envelope,
    safe_task_dir_name,
    task_dir,
    task_envelope,
    validate_envelope,
)


def test_task_envelope_round_trip_shape():
    value = task_envelope(
        task_id="task-1",
        goal="Fix a tiny bug",
        repo="example/demo",
        branch="main",
        allowed_actions=["read files", "run pytest"],
    )
    assert value["schema"] == TASK_SCHEMA
    assert value["schema_version"] == 1
    assert value["task_id"] == "task-1"
    assert "Fix a tiny bug" in dumps_envelope(value)


def test_secret_like_text_is_redacted():
    value = task_envelope(task_id="task-1", goal="token=abc123456789")
    assert value["goal"] == "[REDACTED]"


def test_newer_schema_fails_closed():
    value = task_envelope(task_id="task-1")
    value["schema_version"] = 2
    with pytest.raises(EnvelopeError):
        validate_envelope(value, TASK_SCHEMA)


def test_wrong_schema_fails_closed():
    value = task_envelope(task_id="task-1")
    with pytest.raises(EnvelopeError):
        validate_envelope(value, "BRIDGE_RESULT_V1")


def test_task_id_cannot_escape_root(tmp_path: Path):
    name = safe_task_dir_name("../../outside")
    assert "/" not in name
    assert "\\" not in name
    resolved = task_dir(tmp_path, "../../outside")
    assert tmp_path.resolve() in resolved.parents
