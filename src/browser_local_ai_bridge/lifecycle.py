from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import process_control
from .worker import WorkerConfig


class LifecycleError(RuntimeError):
    pass


@dataclass(frozen=True)
class WorkerIdentity:
    pid: int
    birth_token: str
    config_path: str


def lifecycle_state_path(config: WorkerConfig) -> Path:
    return config.db_path.parent / "worker-lifecycle.json"


def _load_identity(path: Path) -> WorkerIdentity | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError):
        return None
    try:
        pid = int(value.get("pid") or 0)
    except (TypeError, ValueError):
        return None
    token = str(value.get("birth_token") or "")
    config_path = str(value.get("config_path") or "")
    if pid <= 0 or not token or not config_path:
        return None
    return WorkerIdentity(pid=pid, birth_token=token, config_path=config_path)


def _write_identity(path: Path, identity: WorkerIdentity) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(identity.__dict__, sort_keys=True), encoding="utf-8")
    os.replace(temp, path)


def worker_health(config: WorkerConfig) -> dict[str, Any]:
    path = lifecycle_state_path(config)
    identity = _load_identity(path)
    if identity is None:
        return {"status": "STOPPED", "pid": None, "state_path": str(path)}
    alive = process_control.process_alive(identity.pid, identity.birth_token)
    if not alive:
        path.unlink(missing_ok=True)
        return {"status": "STOPPED", "pid": None, "stale_state": True, "state_path": str(path)}
    return {
        "status": "RUNNING",
        "pid": identity.pid,
        "config_path": identity.config_path,
        "state_path": str(path),
    }


def _hidden_popen_kwargs() -> dict[str, Any]:
    if os.name != "nt":
        return {"start_new_session": True}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    return {"creationflags": flags, "startupinfo": startupinfo}


def start_worker(
    config_path: Path,
    config: WorkerConfig,
    *,
    popen_factory: Callable[..., Any] = subprocess.Popen,
    birth_reader: Callable[[int], str] = process_control.process_birth_token,
) -> dict[str, Any]:
    current = worker_health(config)
    if current["status"] == "RUNNING":
        return {**current, "already_running": True}
    command = [sys.executable, "-m", "browser_local_ai_bridge.worker_cli", str(config_path), "run"]
    process = popen_factory(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        **_hidden_popen_kwargs(),
    )
    token = birth_reader(int(process.pid))
    if not token:
        try:
            process.terminate()
        except Exception:
            pass
        raise LifecycleError("worker process identity unavailable")
    identity = WorkerIdentity(int(process.pid), token, str(config_path.resolve()))
    _write_identity(lifecycle_state_path(config), identity)
    return {"status": "RUNNING", "pid": identity.pid, "already_running": False}


def stop_worker(config: WorkerConfig) -> dict[str, Any]:
    path = lifecycle_state_path(config)
    identity = _load_identity(path)
    if identity is None:
        path.unlink(missing_ok=True)
        return {"status": "STOPPED", "stopped": False}
    if not process_control.process_alive(identity.pid, identity.birth_token):
        path.unlink(missing_ok=True)
        return {"status": "STOPPED", "stopped": False, "stale_state": True}
    killed, reason = process_control.terminate_process_tree(identity.pid, identity.birth_token)
    if not killed:
        raise LifecycleError(f"worker stop failed: {reason}")
    path.unlink(missing_ok=True)
    return {"status": "STOPPED", "stopped": True, "reason": reason}


def restart_worker(config_path: Path, config: WorkerConfig) -> dict[str, Any]:
    stop_worker(config)
    return start_worker(config_path, config)


def default_startup_dir() -> Path:
    appdata = os.getenv("APPDATA")
    if not appdata:
        raise LifecycleError("APPDATA is unavailable")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def autostart_path(startup_dir: Path | None = None) -> Path:
    root = startup_dir or default_startup_dir()
    return root / "BrowserLocalAIBridgeWorker.vbs"


def _vbs_string(value: str) -> str:
    return '"' + str(value).replace('"', '""') + '"'


def enable_autostart(config_path: Path, *, startup_dir: Path | None = None) -> dict[str, Any]:
    if os.name != "nt" and startup_dir is None:
        raise LifecycleError("Windows login autostart is supported only on Windows")
    target = autostart_path(startup_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    python = Path(sys.executable)
    pythonw = python.with_name("pythonw.exe")
    executable = pythonw if pythonw.exists() else python
    command = f'"{executable}" -m browser_local_ai_bridge.worker_cli "{config_path.resolve()}" start'
    content = (
        'Set shell = CreateObject("WScript.Shell")\r\n'
        f'shell.Run {_vbs_string(command)}, 0, False\r\n'
    )
    target.write_text(content, encoding="utf-8")
    return {"enabled": True, "path": str(target)}


def disable_autostart(*, startup_dir: Path | None = None) -> dict[str, Any]:
    target = autostart_path(startup_dir)
    existed = target.exists()
    target.unlink(missing_ok=True)
    return {"enabled": False, "removed": existed, "path": str(target)}


def autostart_status(*, startup_dir: Path | None = None) -> dict[str, Any]:
    target = autostart_path(startup_dir)
    return {"enabled": target.exists(), "path": str(target)}
