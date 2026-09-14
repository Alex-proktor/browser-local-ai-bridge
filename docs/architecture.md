# Architecture

## Product model

BrowserLocal AI Bridge has two execution modes that share the same local core.

```text
                    +----------------------+
                    |  Browser AI / LLM    |
                    +----------+-----------+
                               |
                 +-------------+-------------+
                 |                           |
                 v                           v
        Direct-local mode             Durable mode
  Remote computer connector      GitHub task transport
  (RDC/MCP/SSH/etc.)                    |
                 |                      v
                 |                 Local worker
                 |                      |
                 +----------+-----------+
                            v
                    Local control surface
                    CLI / library API
                            |
                            v
                    Orchestration core
                    - repo allowlist
                    - task protocol
                    - persistent state
                    - idempotency/recovery
                    - guards/cancellation
                            |
                            v
                    Executor boundary
                            |
                            v
                    Codex CLI executor
```

## Preferred interactive path

For an active browser session with an authorized remote-computer connector, use direct-local mode.

The browser controller calls a narrow local CLI/API instead of using a mailbox daemon. This removes polling, mailbox latency and unnecessary GitHub traffic while keeping repository authorization and execution policy inside the local package.

Remote Desktop Commander is the author's current direct-local connector, but it is not a dependency of the project. Any connector capable of invoking the local control surface can play the same role.

## Durable path

GitHub Issues/comments remains the durable asynchronous transport for tasks that must survive browser disconnects, controller restarts or long-running work.

Durable mode uses the same task protocol, state machine, repository policy and executor implementation as direct-local mode. GitHub is an adapter, not the core architecture.

## Core ownership

The core owns semantics that remain stable across modes and integrations:

- canonical task identity and lifecycle;
- task/result/checkpoint schemas;
- validation and size bounds;
- logical repository identity and local checkout authorization;
- persistent state transitions;
- idempotency and duplicate protection;
- execution ownership and process identity;
- bounded failure/recovery semantics;
- cancellation and delivery state.

The core must not know how Browser ChatGPT, Remote Desktop Commander, GitHub or Codex expose their external interfaces beyond narrow adapters.

## Local control surface

The local control surface is the main product boundary for direct-local use.

Target commands/API operations include:

- `status` / health;
- list authorized repositories;
- validate a task without executing it;
- execute one task synchronously;
- inspect task state/result;
- cancel an active task;
- worker start/stop/status for durable mode.

Direct-local connectors should call this surface rather than arbitrary project-specific scripts.

## Producer/controller boundary

A producer creates a canonical task envelope and consumes canonical result/checkpoint envelopes.

Browser ChatGPT is the first real controller. No ChatGPT-specific logic belongs in the core.

## Transport boundary

A transport is optional in direct-local mode. In durable mode it delivers canonical envelopes plus transport metadata.

GitHub Issues/comments is the first durable transport adapter.

## Executor boundary

An executor receives a validated, authorized local task plus execution policy and returns structured execution evidence.

Codex CLI is the first executor. The minimal contract covers start, structured progress when available, cancellation, terminal result and process identity/runtime metadata.

Model selection/fallback remains executor-specific policy exposed through neutral requested/attempted/effective model metadata.

## Extension rule

Do not introduce a large plugin framework in v1.

Use small Python protocols/interfaces only where required to isolate real connectors, transports and executors. Add discovery/packaging only after a second real implementation needs it.

## Migration rule

For the author's workstation, migrate first from the private Boss worker to direct-local control through Remote Desktop Commander. Keep durable GitHub mode available for asynchronous tasks and as the open-source reference transport.

Cutover occurs only after standalone E2E, parity validation and rollback preparation.