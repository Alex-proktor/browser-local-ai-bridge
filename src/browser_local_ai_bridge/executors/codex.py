from __future__ import annotations

import json
import os
import signal
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .. import envelopes, process_control
from .base import ExecutionOutcome

DEFAULT_TIMEOUT_SECONDS = 900
MIN_TIMEOUT_SECONDS = 30
MAX_TIMEOUT_SECONDS = 3600
RESULT_STATUSES = {"SUCCESS", "FAILED", "INTERRUPTED"}

ProcessLauncher = Callable[..., ExecutionOutcome]


class CodexExecutorError(ValueError):
    pass


def resolve_codex_executable(value: str = "codex") -> str:
    requested = str(value or "codex").strip() or "codex"
    path = Path(requested).expanduser()
    if path.is_absolute():
        if path.is_file():
            return str(path.resolve())
        if path.name.lower() == "codex.exe" and "OpenAI" in path.parts and "Codex" in path.parts:
            root = Path(os.getenv("LOCALAPPDATA", "")) / "OpenAI" / "Codex" / "bin"
            candidates = sorted(root.glob("*/codex.exe"), key=lambda item: item.stat().st_mtime, reverse=True) if root.is_dir() else []
            if candidates:
                return str(candidates[0].resolve())
        raise CodexExecutorError("configured Codex executable does not exist")
    found = shutil.which(requested)
    if found:
        return str(Path(found).resolve())
    if os.name == "nt":
        root = Path(os.getenv("LOCALAPPDATA", "")) / "OpenAI" / "Codex" / "bin"
        candidates = sorted(root.glob("*/codex.exe"), key=lambda item: item.stat().st_mtime, reverse=True) if root.is_dir() else []
        if candidates:
            return str(candidates[0].resolve())
    raise CodexExecutorError("Codex executable could not be resolved")


def validate_timeout_seconds(value: int | float | None) -> int:
    timeout = int(value if value is not None else DEFAULT_TIMEOUT_SECONDS)
    if timeout < MIN_TIMEOUT_SECONDS or timeout > MAX_TIMEOUT_SECONDS:
        raise CodexExecutorError(
            f"timeout must be between {MIN_TIMEOUT_SECONDS} and {MAX_TIMEOUT_SECONDS} seconds"
        )
    return timeout


def build_prompt(task: dict[str, Any]) -> str:
    sections: list[str] = []
    for label, key in (
        ("goal", "goal"),
        ("why_local_required", "why_local_required"),
        ("allowed_actions", "allowed_actions"),
        ("forbidden_actions", "forbidden_actions"),
        ("known_facts", "known_facts"),
        ("do_not_repeat", "do_not_repeat"),
        ("expected_output", "expected_output"),
    ):
        value = task.get(key)
        if isinstance(value, list):
            items = [str(item).strip() for item in value if str(item).strip()]
            if items:
                sections.append(f"{label}: " + "; ".join(items))
        elif str(value or "").strip():
            sections.append(f"{label}: {str(value).strip()}")

    required = ", ".join(envelopes.REQUIRED_EXECUTION_RESULT_FIELDS)
    sections.append(
        "Final answer contract: return exactly one JSON object with no markdown or prose outside it. "
        f"Required keys: {required}. "
        "status must be one of SUCCESS, FAILED, INTERRUPTED. "
        "changed_files, commands_run, tests must be arrays. "
        "first_error, git_status, blocker, next_recommended_action must be strings. "
        "Optional keys: head, artifacts. Do not include raw logs, secrets, prompt text or chat history."
    )
    return "\n\n".join(sections)


def _terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            process.kill()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()


def _progress_from_line(line: str) -> str:
    try:
        value = json.loads(line)
    except Exception:
        return ""
    if not isinstance(value, dict):
        return ""
    return str(value.get("progress") or value.get("type") or "")[:1200]


def launch_codex_process(
    command: list[str],
    checkout: Path,
    output_path: Path,
    timeout_seconds: int,
    prompt: str,
    observer: Any | None = None,
    idle_timeout_seconds: int = 120,
    repeated_progress_limit: int = 50,
) -> ExecutionOutcome:
    started = time.monotonic()
    popen_kwargs: dict[str, Any] = {
        "cwd": str(checkout),
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "bufsize": 1,
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True
    try:
        process = subprocess.Popen(command, **popen_kwargs)
    except OSError as exc:
        return ExecutionOutcome(status="FAILED", duration_ms=int((time.monotonic()-started)*1000), error_type="start_error", error=str(exc)[:1200], executor="codex")
    birth_token = process_control.process_birth_token(process.pid)
    if observer is not None:
        observer.on_spawn(pid=process.pid, birth_token=birth_token, timeout_seconds=timeout_seconds)
    try:
        if process.stdin is None:
            raise OSError("Codex stdin pipe is unavailable")
        process.stdin.write(prompt)
        process.stdin.close()
    except (OSError, BrokenPipeError) as exc:
        _terminate_process_tree(process)
        return ExecutionOutcome(status="FAILED", duration_ms=int((time.monotonic()-started)*1000), error_type="stdin_error", error=str(exc)[:1200], executor="codex")

    execution_started = time.monotonic()
    progress_state = {"updated": execution_started, "last": "", "repeats": 0}
    lock = threading.Lock()
    def pump() -> None:
        if process.stdout is None:
            return
        for line in iter(process.stdout.readline, ""):
            progress = _progress_from_line(line)
            if not progress:
                continue
            with lock:
                progress_state["updated"] = time.monotonic()
                if progress == progress_state["last"]:
                    progress_state["repeats"] += 1
                else:
                    progress_state["last"] = progress
                    progress_state["repeats"] = 1
            if observer is not None:
                observer.on_progress(progress)
    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    error_type = ""
    while True:
        now = time.monotonic()
        if now - execution_started >= timeout_seconds:
            error_type = "wall_timeout"
            break
        with lock:
            idle_for = now - float(progress_state["updated"])
            repeats = int(progress_state["repeats"])
        if idle_timeout_seconds > 0 and idle_for >= idle_timeout_seconds:
            error_type = "idle_timeout"
            break
        if repeated_progress_limit > 0 and repeats >= repeated_progress_limit:
            error_type = "repeated_progress"
            break
        try:
            exit_code = process.wait(timeout=min(0.25, max(0.05, timeout_seconds-(now-execution_started))))
            break
        except subprocess.TimeoutExpired:
            continue
    if error_type:
        _terminate_process_tree(process)
        reader.join(timeout=1)
        return ExecutionOutcome(status="INTERRUPTED", duration_ms=int((time.monotonic()-started)*1000), error_type=error_type, error=f"Codex execution interrupted: {error_type}", executor="codex")
    reader.join(timeout=1)
    duration_ms = int((time.monotonic() - started) * 1000)
    if exit_code != 0:
        return ExecutionOutcome(status="FAILED", exit_code=exit_code, duration_ms=duration_ms, error_type="nonzero_exit", error=f"Codex exited with code {exit_code}", executor="codex")
    try:
        if output_path.stat().st_size > envelopes.MAX_JSON_BYTES:
            raise envelopes.EnvelopeError("execution result exceeds size limit")
        parsed = envelopes.parse_execution_result(output_path.read_text(encoding="utf-8"))
        result_status = str(parsed.get("status") or "").strip().upper()
        if result_status not in RESULT_STATUSES:
            raise envelopes.EnvelopeError(f"unsupported execution result status: {result_status!r}")
    except (OSError, UnicodeError, envelopes.EnvelopeError) as exc:
        return ExecutionOutcome(status="FAILED", exit_code=exit_code, duration_ms=duration_ms, error_type="invalid_result", error=str(exc)[:1200], executor="codex")
    return ExecutionOutcome(status=result_status, exit_code=exit_code, duration_ms=duration_ms, result=parsed, error_type="structured_failure" if result_status=="FAILED" else ("structured_interruption" if result_status=="INTERRUPTED" else ""), error=str(parsed.get("first_error") or parsed.get("blocker") or "")[:1200], executor="codex")


class CodexExecutor:
    name = "codex"

    def __init__(
        self,
        *,
        executable: str = "codex",
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        idle_timeout_seconds: int = 120,
        repeated_progress_limit: int = 50,
        launcher: ProcessLauncher = launch_codex_process,
    ) -> None:
        self.executable = resolve_codex_executable(executable)
        self.timeout_seconds = validate_timeout_seconds(timeout_seconds)
        self.idle_timeout_seconds = max(0, int(idle_timeout_seconds))
        self.repeated_progress_limit = max(0, int(repeated_progress_limit))
        self.launcher = launcher

    def execute(self, *, task: dict[str, Any], checkout: Path, observer: Any | None = None) -> ExecutionOutcome:
        prompt = build_prompt(task)
        with tempfile.TemporaryDirectory(prefix="browser-local-ai-bridge-codex-") as temp_dir:
            output_path = Path(temp_dir) / "last-message.json"
            command = [
                self.executable,
                "exec",
                "-C",
                str(checkout),
                "--ignore-user-config",
                "--ephemeral",
                "--approve-for-me",
                "--json",
                "--output-last-message",
                str(output_path),
                "-",
            ]
            return self.launcher(
                command, checkout, output_path, self.timeout_seconds, prompt, observer,
                self.idle_timeout_seconds, self.repeated_progress_limit,
            )
