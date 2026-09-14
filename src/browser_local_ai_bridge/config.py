from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class RepoTarget:
    logical_name: str
    checkout_path: Path


def default_home() -> Path:
    explicit = os.getenv("BROWSER_LOCAL_AI_BRIDGE_HOME")
    if explicit:
        return Path(explicit).expanduser().resolve()
    if os.name == "nt" and os.getenv("LOCALAPPDATA"):
        return (Path(os.environ["LOCALAPPDATA"]) / "BrowserLocalAIBridge").resolve()
    return (Path.home() / ".browser-local-ai-bridge").resolve()


def load_repo_allowlist(path: Path) -> dict[str, RepoTarget]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"repo allowlist not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError("repo allowlist is not valid JSON") from exc

    if not isinstance(raw, dict) or raw.get("version") != 1:
        raise ConfigError("unsupported repo allowlist format")
    repos = raw.get("repos")
    if not isinstance(repos, dict):
        raise ConfigError("repo allowlist must contain object field 'repos'")

    result: dict[str, RepoTarget] = {}
    for logical_name, checkout in repos.items():
        if not isinstance(logical_name, str) or not logical_name.strip():
            raise ConfigError("repo allowlist contains invalid logical name")
        if not isinstance(checkout, str) or not checkout.strip():
            raise ConfigError(f"repo allowlist path is invalid for {logical_name!r}")
        resolved = Path(checkout).expanduser().resolve()
        result[logical_name] = RepoTarget(logical_name=logical_name, checkout_path=resolved)
    return result


def resolve_repo(logical_name: str, allowlist: dict[str, RepoTarget]) -> RepoTarget:
    try:
        return allowlist[logical_name]
    except KeyError as exc:
        raise ConfigError(f"repository is not allowed: {logical_name}") from exc
