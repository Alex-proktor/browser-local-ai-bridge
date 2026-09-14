from __future__ import annotations

import argparse
import json
import threading
from dataclasses import asdict
from pathlib import Path

from .config import load_repo_allowlist
from .lifecycle import (
    autostart_status,
    disable_autostart,
    enable_autostart,
    restart_worker,
    start_worker,
    stop_worker,
    worker_health,
)
from .worker import build_executor, load_worker_config, run_worker, worker_tick


def _print_json(value) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def _startup_dir(args: argparse.Namespace) -> Path | None:
    return Path(args.startup_dir).expanduser().resolve() if getattr(args, "startup_dir", None) else None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="browser-local-ai-bridge-worker")
    parser.add_argument("config", help="path to local worker JSON config")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("once", "run", "start", "stop", "restart", "health"):
        sub.add_parser(name)
    for name in ("autostart-enable", "autostart-disable", "autostart-status"):
        item = sub.add_parser(name)
        item.add_argument("--startup-dir")
    return parser


def _run_once(config):
    allowlist = load_repo_allowlist(config.repos_path)
    return worker_tick(
        db_path=config.db_path,
        tasks_root=config.tasks_root,
        lock_root=config.lock_root,
        allowlist=allowlist,
        config=config,
        executor=build_executor(config),
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config_path = Path(args.config).expanduser().resolve()
    config = load_worker_config(config_path)
    if args.command == "once":
        _print_json(asdict(_run_once(config)))
        return 0
    if args.command == "run":
        stop_event = threading.Event()
        try:
            run_worker(config=config, stop_event=stop_event)
        except KeyboardInterrupt:
            stop_event.set()
        return 0
    if args.command == "start":
        value = start_worker(config_path, config)
    elif args.command == "stop":
        value = stop_worker(config)
    elif args.command == "restart":
        value = restart_worker(config_path, config)
    elif args.command == "health":
        value = worker_health(config)
    elif args.command == "autostart-enable":
        value = enable_autostart(config_path, startup_dir=_startup_dir(args))
    elif args.command == "autostart-disable":
        value = disable_autostart(startup_dir=_startup_dir(args))
    elif args.command == "autostart-status":
        value = autostart_status(startup_dir=_startup_dir(args))
    else:
        raise SystemExit(2)
    _print_json(value)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
