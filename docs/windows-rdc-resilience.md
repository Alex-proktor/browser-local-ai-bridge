# Windows RDC resilience

Remote Desktop Commander (RDC) is an optional direct connector. Browser-Local AI Bridge must remain usable through its durable transports when RDC is unavailable.

For the Windows workstation, install RDC at a stable global npm location instead of launching from an `npx` cache. The scripts in `tools/windows` provide:

- `rdc-bootstrap.ps1`: verifies the pinned RDC 0.2.51 entry file by SHA-256 before launch. If it is missing or corrupted, it performs at most two exact-package repairs in a 24-hour window and verifies the restored hash. This prevents an antivirus or other destructive actor from causing an infinite reinstall loop.
- `rdc-launcher.ps1`: runs the bootstrap, starts Node as a child process, and waits on its process handle. Stdout, stderr, and launcher status are persisted under `%LOCALAPPDATA%\RemoteDesktopCommander\logs`.
- `rdc-watchdog.ps1`: every five minutes checks for the stable global RDC process, repairs a disabled task, and restarts a missing agent with bounded exponential backoff.
- `install-rdc-resilience.ps1`: idempotently registers the user-logon agent and watchdog tasks. Both use hidden PowerShell directly; there is no separate VBScript wrapper that can become an orphaned Scheduled Task target.

The bootstrap intentionally does not change RDC authentication/configuration and never excludes files from endpoint protection. A repeated deletion or hash mismatch reaches the repair limit and remains visible in `bootstrap.log` instead of creating a repair storm.

Install RDC first with `npm.cmd install -g @wonderwhy-er/desktop-commander@0.2.51`, then run the installer script. The installer does not change RDC authentication.

Task Scheduler Operational logging is useful for attribution but may require administrator rights to enable. The launcher/watchdog/bootstrap logs remain the primary user-level diagnostic trail.

Rollback: unregister `Remote Desktop Commander Watchdog`, then restore/re-register the previous agent task if required. Do not delete RDC authentication/configuration as part of rollback.
