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

## Safe named recipes (direct local CLI)

`CommandExecutor` runs a fixed argv selected by repository and recipe name from
local `repos.json`. Existing string-valued repository entries still work. To opt
in, use an object entry (replace the example paths with trusted local paths):

```json
{
  "version": 1,
  "repos": {
    "example/demo": {
      "checkout_path": "C:/Projects/demo",
      "recipes": {
        "tests": {
          "argv": ["C:/Python313/python.exe", "-I", "-m", "pytest", "-q"],
          "timeout_seconds": 30,
          "max_output_bytes": 65536
        }
      }
    }
  }
}
```

```text
browser-local-ai-bridge --home <local-home> execute-recipe example/demo tests --branch main
```

Optional `--task-id` supplies the durable task ID; otherwise one is generated.
The existing coordinator validates the checkout and optional branch, takes the
checkout lock, and writes canonical task/result/checkpoint records. Success waits
for the controller; nonzero exits and output overflow fail; timeout interrupts.
CLI exit codes are 0 for success, 1 for execution failure/interruption and 2 for
invalid requests/configuration. The worker and ordinary `execute` retain their
existing Codex routing; task prose cannot select or modify a recipe.

Recipe names contain 1-80 ASCII letters, digits, underscores, dots or hyphens,
starting with a letter or digit. Argv has 1-128 strings of at most 8192 characters
without NULs. The executable must be an absolute non-batch path (`.cmd` and `.bat`
are rejected). No shell, interpolation, task-supplied arguments or stdin is used.
Timeout is an integer from 1-300 seconds (default 30); combined stdout/stderr
limit is 1-1048576 bytes (default 65536). Booleans and unknown recipe fields are
rejected. Output is counted and discarded; results expose only the recipe name,
exit status, byte count (capped at limit + 1), and generic failure category.
Raw output, argv and process exception details are never published.

The child receives only a fixed non-secret runtime set: SystemRoot, WINDIR, TEMP, TMP, LANG, LC_ALL, PATH, LOCALAPPDATA, APPDATA, USERPROFILE, PROGRAMDATA and PROGRAMFILES
environment variables. PATH, API credentials and Python environment overrides
are not inherited; configure absolute executable paths and explicit arguments.
Timeout/overflow cleanup attempts to terminate the process tree (Windows
`taskkill /T /F`, POSIX process group), with a bounded wait. This is not an OS
sandbox: locally configured programs and repository code must be trusted, can
read files with the user's privileges, and may launch detached descendants that
escape cleanup. Windows tree cleanup cannot reliably recover descendants after
their parent has already exited. Do not configure shell/interpreter recipes that
interpret untrusted task text, and protect the local allowlist from untrusted edits.
