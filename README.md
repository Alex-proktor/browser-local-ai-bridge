# BrowserLocal AI Bridge

Local-first orchestration between browser AI controllers and local coding agents.

BrowserLocal AI Bridge removes the manual copy/paste loop between a browser-based AI controller and coding tools working against local repositories.

It supports two execution modes:

```text
Direct local
Browser AI -> authorized computer connector -> BrowserLocal CLI -> executor -> structured result

Durable
Browser AI -> GitHub mailbox -> local worker -> executor -> structured GitHub result
```

The core does not depend on a specific browser controller, remote-computer connector, or coding agent. The first supported executor is Codex CLI.

## v0.1.0

The first release provides:

- `BRIDGE_TASK_V1`, `BRIDGE_RESULT_V1` and `BRIDGE_CHECKPOINT_V1` envelopes;
- explicit logical-repository to local-checkout allowlist;
- SQLite task and runtime state;
- direct-local validate / execute / status / inspect / cancel commands;
- GitHub issue/comment durable transport;
- bounded local worker with replay-safe ingest/execution/publishing;
- wall, idle/no-progress and repeated-progress guards;
- PID + process-birth identity and crash/restart reconciliation;
- Windows worker start / stop / restart / health and optional login autostart;
- Linux and Windows tests on Python 3.11 and 3.12.

## Install

Requirements:

- Python 3.11 or 3.12;
- Git;
- Codex CLI for Codex execution;
- GitHub CLI (`gh`) only for durable GitHub mode.

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate
python -m pip install -U pip
python -m pip install -e .
```

For development/tests:

```bash
python -m pip install -e .[test]
python -m pytest -q
```

## Quick start: direct local

Create a runtime home and copy `examples/repo-allowlist.example.json` to `repos.json`, replacing the sample checkout path with a repository you explicitly authorize.

Then inspect the surface:

```bash
browser-local-ai-bridge --home ./runtime health
browser-local-ai-bridge --home ./runtime repos
```
Validate or execute a canonical task:

```bash
browser-local-ai-bridge --home ./runtime validate examples/task.example.json
browser-local-ai-bridge --home ./runtime execute examples/task.example.json
```

Use `--codex <path>` or `BROWSER_LOCAL_AI_BRIDGE_CODEX` when `codex` is not available on PATH.

See `docs/direct-local-mode.md` for status, inspect, cancel and recovery semantics.

## Durable GitHub worker

Copy `examples/worker.example.json` and set explicit mailbox sources plus runtime paths.

```bash
browser-local-ai-bridge-worker worker.json once
browser-local-ai-bridge-worker worker.json run
```

On Windows the worker CLI also exposes `start`, `stop`, `restart`, `health`, `autostart-enable`, `autostart-disable` and `autostart-status`.

Durable mode requires an authenticated `gh` CLI. It polls only configured issue mailboxes; there is no global GitHub search.

## Safety model

Remote task payloads never choose arbitrary checkout paths. Logical repository names must resolve through the local allowlist. Unknown repositories, malformed envelopes, unsupported schema versions and ambiguous process identities fail closed.

Runtime databases, task artifacts, credentials, raw prompts, chat history and unrestricted tool logs are local-only and must not be committed.

## Documentation

- `docs/architecture.md`
- `docs/direct-local-mode.md`
- `docs/durable-worker.md`
- `docs/executors.md`
- `docs/github-transport.md`
- `docs/protocol-v1.md`

Licensed under the MIT License.

Locally configured deterministic checks can use `execute-recipe <repo> <recipe>`; see [safe named recipes](docs/executors.md#safe-named-recipes-direct-local-cli) for configuration, limits and trust boundaries.
