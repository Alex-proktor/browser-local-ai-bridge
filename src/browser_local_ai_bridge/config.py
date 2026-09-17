from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class CommandRecipe:
    argv: tuple[str, ...]
    timeout_seconds: int = 30
    max_output_bytes: int = 65536


def _recipes(value: object) -> dict[str, CommandRecipe]:
    if not isinstance(value, dict):
        raise ConfigError("recipes must be an object")
    result = {}
    for name, recipe in value.items():
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", name):
            raise ConfigError("invalid recipe name")
        if not isinstance(recipe, dict) or set(recipe) - {"argv", "timeout_seconds", "max_output_bytes"}:
            raise ConfigError("invalid recipe fields")
        argv = recipe.get("argv")
        if (not isinstance(argv, list) or not argv or len(argv) > 128
                or any(not isinstance(arg, str) or "\x00" in arg or len(arg) > 8192 for arg in argv)):
            raise ConfigError("recipe argv must be a bounded array of strings")
        executable = Path(argv[0])
        # Windows batch files can invoke a shell even with shell=False.
        if (not executable.is_absolute()
                or executable.suffix.lower() in {".cmd", ".bat"}
                or executable.resolve().suffix.lower() in {".cmd", ".bat"}):
            raise ConfigError("recipe executable must be an absolute non-batch path")
        timeout = recipe.get("timeout_seconds", 30)
        output = recipe.get("max_output_bytes", 65536)
        if type(timeout) is not int or not 1 <= timeout <= 300:
            raise ConfigError("recipe timeout_seconds must be an integer from 1 to 300")
        if type(output) is not int or not 1 <= output <= 1048576:
            raise ConfigError("recipe max_output_bytes must be an integer from 1 to 1048576")
        result[name] = CommandRecipe(tuple(argv), timeout, output)
    return result


@dataclass(frozen=True)
class RepoTarget:
    logical_name: str
    checkout_path: Path
    recipes: dict[str, CommandRecipe] = field(default_factory=dict)


RepoAllowlist = dict[str, RepoTarget | tuple[RepoTarget, ...]]


def default_home() -> Path:
    explicit = os.getenv("BROWSER_LOCAL_AI_BRIDGE_HOME")
    if explicit:
        return Path(explicit).expanduser().resolve()
    if os.name == "nt" and os.getenv("LOCALAPPDATA"):
        return (Path(os.environ["LOCALAPPDATA"]) / "BrowserLocalAIBridge").resolve()
    return (Path.home() / ".browser-local-ai-bridge").resolve()


def load_repo_allowlist(path: Path) -> RepoAllowlist:
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

    result: RepoAllowlist = {}
    for logical_name, checkout in repos.items():
        if not isinstance(logical_name, str) or not logical_name.strip():
            raise ConfigError("repo allowlist contains invalid logical name")
        entries = checkout if isinstance(checkout, list) else [checkout]
        if not entries:
            raise ConfigError("repo checkout list must not be empty")
        targets: list[RepoTarget] = []
        for entry in entries:
            recipes = {}
            if isinstance(entry, dict):
                if set(entry) - {"checkout_path", "recipes"}:
                    raise ConfigError("invalid repo configuration fields")
                recipes = _recipes(entry.get("recipes", {}))
                entry = entry.get("checkout_path")
            if not isinstance(entry, str) or not entry.strip():
                raise ConfigError(f"repo allowlist path is invalid for {logical_name!r}")
            target = RepoTarget(logical_name, Path(entry).expanduser().resolve(), recipes)
            if any(existing.checkout_path == target.checkout_path for existing in targets):
                raise ConfigError("duplicate checkout path for repository")
            targets.append(target)
        result[logical_name] = tuple(targets) if isinstance(checkout, list) else targets[0]
    return result


def resolve_repo(logical_name: str, allowlist: RepoAllowlist) -> RepoTarget | tuple[RepoTarget, ...]:
    try:
        return allowlist[logical_name]
    except KeyError as exc:
        raise ConfigError(f"repository is not allowed: {logical_name}") from exc
