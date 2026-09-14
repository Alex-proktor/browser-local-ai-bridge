from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any


class ProcessControlError(RuntimeError):
    pass


def _powershell_json(script: str) -> dict[str, Any] | None:
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0 or not (completed.stdout or "").strip():
        return None
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def process_birth_token(pid: int) -> str:
    pid = int(pid)
    if pid <= 0:
        return ""
    if os.name == "nt":
        info = _powershell_json(
            f"$p=Get-CimInstance Win32_Process -Filter 'ProcessId={pid}' -ErrorAction SilentlyContinue; "
            "if($p){$p|Select-Object ProcessId,CreationDate|ConvertTo-Json -Compress}"
        )
        return str(info.get("CreationDate") or "") if info else ""
    stat = Path(f"/proc/{pid}/stat")
    try:
        parts = stat.read_text(encoding="utf-8").split()
    except OSError:
        return ""
    return parts[21] if len(parts) > 21 else ""


def process_alive(pid: int | None, birth_token: str) -> bool:
    if not pid or not birth_token:
        return False
    return process_birth_token(int(pid)) == str(birth_token)


def terminate_process_tree(pid: int, birth_token: str) -> tuple[bool, str]:
    pid = int(pid)
    if not process_alive(pid, birth_token):
        return False, "stale_or_reused_pid"
    if os.name != "nt":
        return False, "active_cancel_not_supported_on_this_platform"
    try:
        completed = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False, "terminate_failed"
    if completed.returncode != 0:
        return False, "taskkill_failed"
    return True, "terminated"
