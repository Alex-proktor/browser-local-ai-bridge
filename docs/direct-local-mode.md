# Direct-local mode

Direct-local mode is the preferred interactive path when a browser AI already has authorized access to the workstation.

```text
Browser AI
  -> authorized remote-computer connector
  -> BrowserLocal CLI/library
  -> repository policy + BRIDGE_TASK_V1 validation
  -> executor
  -> BRIDGE_RESULT_V1 / checkpoint
  -> Browser AI
```

The connector is transport only. BrowserLocal remains responsible for repository authorization, branch validation, persistent state, execution and structured results. Remote Desktop Commander is one connector used by the author; it is not a product dependency.

## Runtime layout

By default the local surface uses `config.default_home()`:

- `repos.json` — logical repository allowlist;
- `task-state.db` — persistent task state plus bounded `execution_runs` metadata;
- `tasks/` — canonical task/result/checkpoint envelopes;
- `locks/` — checkout execution locks.

`BROWSER_LOCAL_AI_BRIDGE_HOME` or `--home` can select another runtime home.

## CLI

The console entrypoint is `browser-local-ai-bridge`.

```text
browser-local-ai-bridge health
browser-local-ai-bridge repos
browser-local-ai-bridge validate task.json
browser-local-ai-bridge execute task.json
browser-local-ai-bridge status <task-id>
browser-local-ai-bridge inspect <task-id>
browser-local-ai-bridge cancel <task-id>
```

`execute` is synchronous and processes exactly one canonical `BRIDGE_TASK_V1`. Codex can be selected explicitly with `--codex <path>` or `BROWSER_LOCAL_AI_BRIDGE_CODEX`; otherwise the executor uses `codex` from PATH.

During execution, the runtime persists the executor PID, process-birth identity, timeout and bounded structured progress. `status` exposes only bounded runtime metadata. On Windows, `cancel` terminates a verified executor process tree and records `CANCELLED`; missing or stale identity fails closed instead of killing an ambiguous process.

Codex execution has bounded wall timeout, idle/no-progress timeout and repeated-progress guards. Startup reconciliation keeps a still-live verified run as `RUNNING`, recovers an already-written terminal result without re-execution, and marks a stale `RUNNING` task `INTERRUPTED` rather than retrying side effects automatically.

The same `BRIDGE_TASK_V1` task model is shared with durable GitHub transport, so direct-local mode does not introduce another task protocol.
