# v1 roadmap

## Milestone 1: standalone core

Implement neutral package structure, protocol schemas, repository allowlist and persistent task state with focused tests.

## Milestone 2: Codex execution

Implement the Codex CLI executor and proven reliability behavior: model metadata/routing, bounded fallback, wall/idle/repeated-progress guards, process-tree termination, cancellation and process identity.

## Milestone 3: GitHub control plane

Implement GitHub Issues/comments ingest and publishing plus idempotent delivery semantics.

## Milestone 4: worker lifecycle and recovery

Implement the foreground worker, startup reconciliation, single-instance lifecycle, health/start/stop/restart and Windows autostart.

## Milestone 5: safe demo

Provide a synthetic sample repository and reproducible Browser ChatGPT -> GitHub -> local worker -> Codex -> GitHub flow.

## Milestone 6: parity and real migration

Run bounded parity validation against the existing private implementation, then switch the author's real workflow with rollback available.

## Milestone 7: public release

Pass security/privacy/history audit, improve installation/docs, publish the repository and tag the first release.

Post-v1 adapters for other controllers, transports or executors are welcome after the core contract is stable; they are not blockers for the ChatGPT + Codex v1.
