# GitHub transport v1

GitHub Issues/comments are the first durable transport for BrowserLocal AI Bridge.

GitHub is a control plane and mailbox, not the execution environment. Local execution remains on the user's machine.

## Task comment

A task candidate must have `BRIDGE_TASK_V1` as its first non-empty logical line. Occurrences of the marker inside prose, JSON, result comments or checkpoints do not activate the parser.

Example:

````text
BRIDGE_TASK_V1

```json
{
  "schema": "BRIDGE_TASK_V1",
  "version": 1,
  "event_id": "demo-task-001",
  "task": {
    "schema": "BRIDGE_TASK_V1",
    "schema_version": 1,
    "task_id": "demo-001",
    "goal": "Fix the failing sample test",
    "repo": "demo/sample",
    "branch": "main",
    "why_local_required": "The sample checkout and test environment are local",
    "allowed_actions": ["read files", "edit the sample", "run pytest"],
    "forbidden_actions": ["deploy", "access unrelated repositories"],
    "known_facts": [],
    "do_not_repeat": [],
    "expected_output": "Return changed files and pytest result"
  },
  "transport": {
    "result_repo": "demo/control-plane",
    "result_issue": 1
  }
}
```
````

The task contains only a logical repository name. A local checkout path is resolved from the local allowlist and is never accepted from the untrusted GitHub payload.

## Result comment

A successful delivery is one comment beginning with `BRIDGE_RESULT_V1` and containing a versioned wrapper with:

- transport `event_id`;
- `task_id`;
- canonical `BRIDGE_RESULT_V1` result envelope;
- optional canonical `BRIDGE_CHECKPOINT_V1` checkpoint envelope.

Before publishing, strings are sanitized again and recognized local absolute paths are replaced with `[LOCAL_PATH_REDACTED]`.

## Idempotency

Inbound task events are keyed by transport `event_id` plus a canonical payload hash. Exact replay is safe; conflicting reuse of the same event ID fails closed.

Outbound delivery has one stable result event ID per task. Once a remote comment ID is recorded as successful, replay does not post a second comment.

## Local adapter

v1 uses the authenticated GitHub CLI (`gh`) for local API access. Authentication remains outside model/task payloads. Command execution is isolated behind the GitHub transport adapter so future transports do not change the core task/state model.

## Failure rules

The transport fails closed for:

- malformed top-level task JSON;
- unsupported transport or envelope versions;
- unknown logical repositories;
- conflicting event replay;
- conflicting delivery target;
- task/result/checkpoint ID mismatch;
- oversized transport comments;
- invalid GitHub API responses.

Raw issue bodies are not persisted in the task state database.
