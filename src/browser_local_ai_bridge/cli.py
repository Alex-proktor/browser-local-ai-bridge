from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

from . import envelopes
from .executors import CodexExecutor
from .executors.command import CommandExecutor
from .config import load_repo_allowlist, resolve_repo
from .local_control import DirectLocalControl, LocalControlError, RuntimePaths
from .execution import _git_branch, select_repo_target


def _json_out(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def _load_task(path: str) -> dict[str, Any]:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LocalControlError("task file is not valid JSON") from exc
    return envelopes.validate_envelope(raw, envelopes.TASK_SCHEMA)


def _control(args: argparse.Namespace, *, executor: bool = False) -> DirectLocalControl:
    home = Path(args.home).expanduser().resolve() if args.home else None
    paths = RuntimePaths.from_home(home)
    if not executor:
        return DirectLocalControl(paths=paths)
    executable = args.codex or os.getenv("BROWSER_LOCAL_AI_BRIDGE_CODEX", "codex")
    return DirectLocalControl(
        paths=paths,
        executor=CodexExecutor(executable=executable, timeout_seconds=args.timeout),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="browser-local-ai-bridge")
    parser.add_argument("--home", help="runtime home; defaults to BROWSER_LOCAL_AI_BRIDGE_HOME/default")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("health")
    sub.add_parser("repos")

    validate = sub.add_parser("validate")
    validate.add_argument("task")

    execute = sub.add_parser("execute")
    execute.add_argument("task")
    execute.add_argument("--codex")
    execute.add_argument("--timeout", type=int, default=900)

    recipe = sub.add_parser("execute-recipe")
    recipe.add_argument("repo")
    recipe.add_argument("recipe")
    recipe.add_argument("--task-id", default=None)
    recipe.add_argument("--branch", default="")

    status = sub.add_parser("status")
    status.add_argument("task_id")

    inspect = sub.add_parser("inspect")
    inspect.add_argument("task_id")

    cancel = sub.add_parser("cancel")
    cancel.add_argument("task_id")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "health":
            value = _control(args).health()
        elif args.command == "repos":
            value = _control(args).list_repos()
        elif args.command == "validate":
            value = _control(args).validate_task(_load_task(args.task))
        elif args.command == "execute":
            value = _control(args, executor=True).submit(_load_task(args.task))
        elif args.command == "execute-recipe":
            paths = _control(args).paths
            target = select_repo_target(
                resolve_repo(args.repo, load_repo_allowlist(paths.repos)), args.branch, _git_branch
            )
            executor = CommandExecutor(target=target, recipe_name=args.recipe)
            task = envelopes.task_envelope(
                task_id=args.task_id or f"recipe-{uuid.uuid4().hex}",
                repo=args.repo, branch=args.branch,
                goal=f"Run local recipe {args.recipe}",
                why_local_required="Locally configured deterministic check",
                allowed_actions=[f"recipe:{args.recipe}"],
                forbidden_actions=["Execute commands supplied by task text"],
                expected_output="Recipe exit status and bounded output accounting",
            )
            value = DirectLocalControl(paths=paths, executor=executor).submit(task)
        elif args.command == "status":
            value = _control(args).task_status(args.task_id)
        elif args.command == "inspect":
            value = _control(args).inspect(args.task_id)
        elif args.command == "cancel":
            value = _control(args).cancel(args.task_id)
        else:
            parser.error("unknown command")
            return 2
    except (LocalControlError, ValueError) as exc:
        _json_out({"status": "ERROR", "error": str(exc)})
        return 2
    _json_out(value)
    if args.command == "execute-recipe" and value["execution"]["status"] != "SUCCESS":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
