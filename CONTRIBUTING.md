# Contributing

Thanks for helping improve BrowserLocal AI Bridge.

## Workflow

1. Fork the repository and create a focused branch.
2. Keep changes small and tied to an observed workflow problem.
3. Add or update tests for behavior changes.
4. Run `python -m pytest -q` locally.
5. Open a pull request against `main`; do not request write access just to propose a change.

## Design constraints

- Keep the core vendor-neutral; connectors and executors are adapters.
- Prefer direct-local execution for interactive work and durable mode only when durability is required.
- Remote tasks must never select arbitrary local paths or bypass the repository allowlist.
- Do not move large datasets through task/result envelopes; keep them local and return bounded metadata/artifact paths.
- Avoid features without a concrete observed benefit in real usage.

Security issues should follow `SECURITY.md`, not public Issues.
