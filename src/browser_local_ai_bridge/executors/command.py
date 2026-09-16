from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from .. import process_control
from ..config import ConfigError, RepoTarget
from .base import ExecutionOutcome


class CommandExecutor:
    """Run one locally selected, repo-scoped recipe; never interpret task text."""

    name = "command"

    def __init__(self, *, target: RepoTarget, recipe_name: str) -> None:
        if recipe_name not in target.recipes:
            raise ConfigError("unknown recipe for repository")
        self.target = target
        self.recipe_name = recipe_name
        self.recipe = target.recipes[recipe_name]

    def execute(self, *, task: dict[str, Any], checkout: Path, observer: Any | None = None) -> ExecutionOutcome:
        if task.get("repo") != self.target.logical_name or checkout.resolve() != self.target.checkout_path.resolve():
            raise ConfigError("recipe repository mismatch")
        recipe = self.recipe
        started = time.monotonic()
        count = 0
        exceeded = threading.Event()
        drained = threading.Event()
        read_failed = threading.Event()
        process = None
        reader = None
        error = ""
        exit_code = None

        def stop() -> None:
            if process is None:
                return
            if os.name == "nt" and process.poll() is None:
                try:
                    subprocess.run(
                        [str(Path(os.environ["SystemRoot"]) / "System32" / "taskkill.exe"),
                         "/PID", str(process.pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        timeout=5, check=False, shell=False,
                    )
                except (OSError, KeyError, subprocess.TimeoutExpired):
                    pass
            elif os.name != "nt":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)

        try:
            kwargs = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
            # Do not pass API keys, tokens, PYTHONPATH, or arbitrary parent env.
            env = {key: value for key, value in os.environ.items()
                   if key.upper() in {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LANG", "LC_ALL", "PATH", "LOCALAPPDATA", "APPDATA", "USERPROFILE", "PROGRAMDATA", "PROGRAMFILES"}}
            process = subprocess.Popen(
                list(recipe.argv), cwd=str(checkout.resolve()), shell=False,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, env=env, bufsize=0, **kwargs,
            )

            def pump() -> None:
                nonlocal count
                try:
                    while True:
                        chunk = process.stdout.read(min(4096, recipe.max_output_bytes + 1 - count))
                        if not chunk:
                            break
                        count += len(chunk)
                        if count > recipe.max_output_bytes:
                            exceeded.set()
                            break
                except OSError:
                    read_failed.set()
                finally:
                    process.stdout.close()
                    drained.set()

            reader = threading.Thread(target=pump, daemon=True)
            reader.start()
            if observer is not None:
                observer.on_spawn(
                    pid=process.pid, birth_token=process_control.process_birth_token(process.pid),
                    timeout_seconds=recipe.timeout_seconds,
                )
            while True:
                if exceeded.is_set():
                    error = "output_limit"
                    break
                if process.poll() is not None and drained.is_set():
                    break
                if time.monotonic() - started >= recipe.timeout_seconds:
                    error = "wall_timeout"
                    break
                time.sleep(0.01)
            if error:
                stop()
            exit_code = process.wait(timeout=5)
            if not error and read_failed.is_set():
                error = "process_error"
            if not error and exit_code != 0:
                error = "nonzero_exit"
        except (OSError, subprocess.SubprocessError):
            error = "process_error"
        except Exception:
            # Observer failures must not leak exception data or leave a child running.
            error = "observer_error"
        finally:
            try:
                stop()
            except (OSError, subprocess.SubprocessError):
                error = "cleanup_error"
            if reader is not None:
                reader.join(timeout=1)

        status = "INTERRUPTED" if error == "wall_timeout" else ("FAILED" if error else "SUCCESS")
        message = f"Recipe execution: {error}" if error else ""
        return ExecutionOutcome(
            status=status, exit_code=exit_code,
            duration_ms=int((time.monotonic() - started) * 1000),
            error_type=error, error=message, executor=self.name,
            result={
                "commands_run": [f"recipe:{self.recipe_name}"],
                "tests": [f"exit_code={exit_code}; output_bytes={count}; output_limit_exceeded={exceeded.is_set()}"],
                "first_error": message, "blocker": message,
                "next_recommended_action": "Review recipe outcome.",
            },
        )
