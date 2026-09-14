# Security Policy

## Supported release

Security fixes are accepted for the latest tagged release and current `main`.

## Security boundary

BrowserLocal AI Bridge executes coding-agent tasks only against explicitly configured local repositories. The local repository allowlist is a hard trust boundary: remote task payloads must never choose arbitrary local filesystem paths.

The project must not persist or publish:

- access tokens, API keys, cookies or credentials;
- `.env` contents;
- raw prompts or chat history;
- raw stdout/stderr or unrestricted tool logs;
- personal or employer/client data;
- local runtime databases or task artifacts.

Unknown repositories, unsupported schema versions, ambiguous process identity and malformed transport input fail closed.

## Reporting

Do not open a public issue for a suspected vulnerability that could expose secrets or enable unsafe local execution. Contact the repository owner privately through GitHub with enough detail to reproduce the issue safely.
