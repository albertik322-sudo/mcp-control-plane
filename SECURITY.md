# Security policy

## Supported version

The default branch is the supported development line. The project does not
currently publish a compatibility or long-term support guarantee.

## Reporting a vulnerability

Use a private GitHub Security Advisory for vulnerabilities, exposed
credentials, authorization bypasses, unsafe tool behavior, or supply-chain
issues. Do not open a public issue containing exploit details or secrets.

If a credential may have been exposed:

1. Revoke or rotate it immediately.
2. Preserve relevant audit evidence without copying the secret.
3. Open a private advisory describing the affected component and revision.

## Control-plane rules

- Never commit `.env`, private keys, tokens, passwords, captures, or runtime
  databases.
- Keep infrastructure mutation behind server-side authorization and narrow
  allowlists.
- Treat agent prompts, UI confirmations, and approval text as workflow controls,
  not authorization boundaries.
- Pin external Agent Skills and critical dependencies to reviewed revisions.
- Keep OpenTelemetry argument capture disabled; emit metadata-only attributes.
- Run `gitleaks`, offline tests, the MCP audit, and stateless conformance before
  publishing a release.
