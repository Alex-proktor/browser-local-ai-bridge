# Protocol v1 draft

This document defines the neutral public protocol direction. Exact JSON schemas will be implemented and tested before v1 release.

## Markers

- `BRIDGE_TASK_V1`
- `BRIDGE_RESULT_V1`
- `BRIDGE_CHECKPOINT_V1`

No Hermes-specific marker is part of the public protocol.

## Task envelope

Required semantic fields:

- `schema_version`
- `task_id`
- `goal`
- `repo`
- `branch`
- `why_local_required`
- `allowed_actions`
- `forbidden_actions`
- `known_facts`
- `do_not_repeat`
- `expected_output`

Optional integration policy may include neutral execution preferences such as executor/model tier, but the canonical task meaning must not depend on Codex-specific flags.

## Result envelope

Required semantic fields:

- `schema_version`
- `task_id`
- `status`
- `head`
- `changed_files`
- `commands_run`
- `tests`
- `first_error`
- `git_status`
- `blocker`
- `artifacts`
- `next_recommended_action`

Runtime metadata may include executor name, run id and requested/attempted/effective model identifiers when available.

## Checkpoint envelope

Required semantic fields:

- `schema_version`
- `task_id`
- `goal`
- `decisions`
- `verified`
- `changed`
- `failed_attempts`
- `do_not_repeat`
- `remaining`
- `next_action`
- `evidence_refs`

## Safety

- unsupported schema versions fail closed;
- task ids must be safe for local persistence mapping;
- envelope sizes are bounded;
- raw chat history, credentials, raw stdout/stderr and tool logs are not protocol payloads;
- transport metadata is separate from task semantics;
- local checkout paths never come from an untrusted task payload.
