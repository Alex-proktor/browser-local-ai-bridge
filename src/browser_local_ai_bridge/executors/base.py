from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True)
class ExecutionOutcome:
    status: str
    exit_code: int | None = None
    duration_ms: int = 0
    result: dict[str, Any] = field(default_factory=dict)
    error_type: str = ""
    error: str = ""
    executor: str = ""


class Executor(Protocol):
    name: str

    def execute(self, *, task: dict[str, Any], checkout: Path, observer: Any | None = None) -> ExecutionOutcome:
        ...
