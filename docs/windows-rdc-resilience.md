# Windows RDC resilience

Remote Desktop Commander (RDC) is an optional direct connector. Browser-Local AI Bridge must remain usable through its durable transports when RDC is unavailable.

For the Windows workstation, install RDC at a stable global npm location instead of launching from an `npx` cache. The scripts in `tools/windows` provide:
- `rdc-launcher.ps1`: validates fixed Node/RDC paths, starts Node as a child process, and waits on its process handle. Stdout, stderr, and launcher status are written to separate persistent files under `%LOCALAPPDATA%\RemoteDesktopCommander\logs`;
- `rdc-watchdog.ps1`: every five minutes checks for the global npm `dist/index.js` process, repairs a disabled task, and restarts a missing agent with bounded exponential backoff (up to 30 minutes);
- `install-rdc-resilience.ps1`: idempotently registers the user-logon agent and watchdog tasks.

Install RDC first with `npm.cmd install -g @wonderwhy-er/desktop-commander@<pinned-version>`, then run the installer script. The installer does not change RDC authentication.

Task Scheduler Operational logging is useful for attribution but may require administrator rights to enable. The launcher/watchdog logs therefore remain the primary user-level diagnostic trail.

Rollback: unregister `Remote Desktop Commander Watchdog`, then restore/re-register the previous agent task if required. Do not delete RDC authentication/configuration as part of rollback.
