import json
import pytest
import sys
from pathlib import Path

from browser_local_ai_bridge.executors.base import ExecutionOutcome
from browser_local_ai_bridge.executors.codex import (
    CodexExecutor,
    build_prompt,
    launch_codex_process,
)


def _task():
    return {
        "task_id": "task-1",
        "goal": "Fix sample",
        "why_local_required": "Needs local files",
        "allowed_actions": ["read", "edit"],
        "forbidden_actions": ["deploy"],
        "known_facts": ["synthetic"],
        "do_not_repeat": [],
        "expected_output": "Return tests",
    }


def _valid_result(status: str = "SUCCESS"):
    return {
        "status": status,
        "changed_files": [],
        "commands_run": ["pytest"],
        "tests": ["1 passed"] if status == "SUCCESS" else ["1 failed"],
        "first_error": "" if status == "SUCCESS" else "sample failed",
        "git_status": "clean",
        "blocker": "" if status == "SUCCESS" else "sample failed",
        "next_recommended_action": "done" if status == "SUCCESS" else "fix sample",
    }


def test_prompt_contains_scoped_task_and_strict_contract():
    prompt = build_prompt(_task())
    assert "goal: Fix sample" in prompt
    assert "forbidden_actions: deploy" in prompt
    assert "return exactly one JSON object" in prompt
    assert "changed_files" in prompt


def test_codex_executor_uses_stdin_and_output_last_message(tmp_path: Path):
    captured = {}

    def launcher(command, checkout, output_path, timeout_seconds, prompt, observer=None, idle_timeout_seconds=120, repeated_progress_limit=50):
        captured["command"] = command
        captured["checkout"] = checkout
        captured["timeout"] = timeout_seconds
        captured["prompt"] = prompt
        output_path.write_text(json.dumps(_valid_result()), encoding="utf-8")
        return ExecutionOutcome(status="SUCCESS", result=_valid_result(), executor="codex")

    executor = CodexExecutor(timeout_seconds=60, launcher=launcher)
    outcome = executor.execute(task=_task(), checkout=tmp_path)

    assert outcome.status == "SUCCESS"
    assert captured["checkout"] == tmp_path
    assert captured["timeout"] == 60
    assert Path(captured["command"][0]).name.lower() in {"codex", "codex.exe"}
    assert captured["command"][1:4] == ["exec", "-C", str(tmp_path)]
    assert captured["command"][-1] == "-"
    assert "Fix sample" not in captured["command"]
    assert "goal: Fix sample" in captured["prompt"]
    assert "--json" in captured["command"]
    assert "--output-last-message" in captured["command"]
    assert "--ignore-user-config" in captured["command"]
    assert "--ephemeral" in captured["command"]
    assert "--sandbox" not in captured["command"]
    assert "--approve-for-me" in captured["command"]


def _write_result_script():
    return "from pathlib import Path; import sys; sys.stdin.buffer.read(); Path(sys.argv[1]).write_text(sys.argv[2], encoding='utf-8')"


def test_production_launcher_accepts_valid_last_message(tmp_path: Path):
    output = tmp_path / "last.json"
    outcome = launch_codex_process(
        [sys.executable, "-c", _write_result_script(), str(output), json.dumps(_valid_result())],
        tmp_path,
        output,
        5,
        "synthetic prompt",
    )
    assert outcome.status == "SUCCESS"
    assert outcome.result["tests"] == ["1 passed"]


def test_production_launcher_honors_structured_failure(tmp_path: Path):
    output = tmp_path / "last.json"
    outcome = launch_codex_process(
        [sys.executable, "-c", _write_result_script(), str(output), json.dumps(_valid_result("FAILED"))],
        tmp_path,
        output,
        5,
        "synthetic prompt",
    )
    assert outcome.status == "FAILED"
    assert outcome.error_type == "structured_failure"
    assert outcome.result["tests"] == ["1 failed"]


def test_production_launcher_rejects_malformed_last_message(tmp_path: Path):
    output = tmp_path / "last.json"
    script = "from pathlib import Path; import sys; sys.stdin.buffer.read(); Path(sys.argv[1]).write_text('not-json', encoding='utf-8')"
    outcome = launch_codex_process(
        [sys.executable, "-c", script, str(output)],
        tmp_path,
        output,
        5,
        "synthetic prompt",
    )
    assert outcome.status == "FAILED"
    assert outcome.error_type == "invalid_result"


def test_production_launcher_reports_nonzero_exit(tmp_path: Path):
    output = tmp_path / "last.json"
    outcome = launch_codex_process(
        [sys.executable, "-c", "import sys; sys.stdin.buffer.read(); raise SystemExit(3)"],
        tmp_path,
        output,
        5,
        "synthetic prompt",
    )
    assert outcome.status == "FAILED"
    assert outcome.exit_code == 3
    assert outcome.error_type == "nonzero_exit"


def test_production_launcher_terminates_on_timeout(tmp_path: Path):
    output = tmp_path / "last.json"
    outcome = launch_codex_process(
        [sys.executable, "-c", "import sys,time; sys.stdin.buffer.read(); time.sleep(10)"],
        tmp_path,
        output,
        1,
        "synthetic prompt",
    )
    assert outcome.status == "INTERRUPTED"
    assert outcome.error_type == "wall_timeout"



def test_production_launcher_idle_guard_interrupts(tmp_path: Path):
    output = tmp_path / "last.json"
    script = (
        "import sys,time; sys.stdin.read(); "
        "print('{\"type\":\"turn.started\"}', flush=True); time.sleep(5)"
    )
    outcome = launch_codex_process(
        [sys.executable, "-c", script], tmp_path, output, 5, "synthetic",
        None, 1, 0,
    )
    assert outcome.status == "INTERRUPTED"
    assert outcome.error_type == "idle_timeout"


def test_production_launcher_repeated_progress_guard_interrupts(tmp_path: Path):
    output = tmp_path / "last.json"
    script = (
        "import sys,time; sys.stdin.read(); "
        "[print('{\"type\":\"turn.started\"}', flush=True) for _ in range(5)]; time.sleep(5)"
    )
    outcome = launch_codex_process(
        [sys.executable, "-c", script], tmp_path, output, 5, "synthetic",
        None, 4, 3,
    )
    assert outcome.status == "INTERRUPTED"
    assert outcome.error_type == "repeated_progress"


def test_identity_lookup_overhead_does_not_consume_execution_timeout(tmp_path: Path, monkeypatch):
    import time

    output = tmp_path / "last.json"
    def slow_birth_token(pid):
        time.sleep(1.1)
        return "birth"
    monkeypatch.setattr(
        "browser_local_ai_bridge.executors.codex.process_control.process_birth_token",
        slow_birth_token,
    )
    outcome = launch_codex_process(
        [sys.executable, "-c", _write_result_script(), str(output), json.dumps(_valid_result())],
        tmp_path, output, 1, "synthetic prompt",
    )
    assert outcome.status == "SUCCESS"


def test_resolve_codex_executable_uses_existing_absolute_path(tmp_path):
    from browser_local_ai_bridge.executors.codex import resolve_codex_executable
    exe = tmp_path / "codex.exe"
    exe.write_bytes(b"")
    assert resolve_codex_executable(str(exe)) == str(exe.resolve())


def test_resolve_codex_executable_fails_for_missing_non_openai_absolute_path(tmp_path):
    from browser_local_ai_bridge.executors.codex import CodexExecutorError, resolve_codex_executable
    with pytest.raises(CodexExecutorError, match="does not exist"):
        resolve_codex_executable(str(tmp_path / "codex.exe"))
