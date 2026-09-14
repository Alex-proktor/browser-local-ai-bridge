# Executors

BrowserLocal AI Bridge keeps orchestration independent from a particular local coding agent.

## Executor boundary

The core executor contract is intentionally small:

```python
class Executor(Protocol):
    name: str

    def execute(self, *, task: dict, checkout: Path) -> ExecutionOutcome:
        ...
```

Transport, persistent task state, repository policy and result publishing do not depend on Codex-specific types.

## Codex executor v1

`CodexExecutor` is the first concrete executor because Browser ChatGPT + local Codex is the primary v1 workflow.

For one canonical task it:

1. receives a local checkout already resolved from the allowlist;
2. builds a compact prompt only from canonical task-envelope fields;
3. launches `codex exec` with an explicit checkout and temporary last-message file;
4. passes the task prompt through stdin using Codex's trailing `-` input mode so prompt contents are not placed in the command line;
5. discards raw stdout/stderr instead of persisting them;
6. enforces a bounded wall timeout and terminates the process tree on timeout;
7. accepts success only when the last message is bounded, valid JSON and satisfies the structured result contract.

Current command shape:

```text
codex exec -C <checkout> --json --output-last-message <temporary-file> -
```

The scoped prompt is written to the child process stdin. The temporary result file is removed with its temporary directory after execution.

## Execution coordinator

`execution.execute_task()` owns orchestration around an executor:

- task must already be `READY`;
- canonical task envelope must match durable task state;
- logical repository is resolved through the local allowlist;
- checkout must exist, be a Git checkout and be on the requested branch;
- an atomic checkout lock prevents two bridge executions from writing the same checkout concurrently;
- task moves `READY -> RUNNING` before executor invocation;
- valid success writes canonical result/checkpoint and moves to `WAITING_CONTROLLER`;
- executor failure writes a bounded publishable failure result/checkpoint and moves to `FAILED`;
- wall timeout/interruption moves to `INTERRUPTED`;
- no automatic retry occurs in this layer.

Crash-time stale lock reconciliation, live status/cancel, idle/no-progress guards and model routing are separate later stages.

## Future executors

A future executor (for example another local coding-agent CLI) should implement the same small contract and reuse the same task state, repository policy, transport and result envelopes. No plugin discovery framework is required for v1.
