# Durable GitHub worker

The durable worker is the asynchronous path for queued work that must survive loss of the browser session.

Each bounded tick:

1. reconciles stale or recoverable runtime state;
2. polls only explicitly configured GitHub mailbox sources;
3. ingests at most one new task per source;
4. executes at most one READY task;
5. publishes a bounded number of pending terminal results.

A source or delivery failure is recorded in the tick result and does not stop the loop. Exact replay and “no new task” are normal conditions.
## Configuration

Worker JSON version 1 requires explicit `sources`, runtime paths (`repos`, `db`, `tasks`, `locks`) and an executor block.

```text
browser-local-ai-bridge-worker worker.json once
browser-local-ai-bridge-worker worker.json run
```

`once` performs one bounded tick and prints structured JSON. `run` repeats ticks with an interruptible configured poll interval.

The worker composes the existing GitHub transport, orchestration state/recovery and executor boundary. It does not perform global GitHub search and does not add unbounded retry.
