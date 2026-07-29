# Configuration

Copy `.env.example` to `.env` and fill only the integrations you use.

## Credential guidance

- Prefer SSH keys over passwords.
- Keep sudo credentials separate from normal SSH credentials.
- Store Windows credentials in Credential Manager where supported.
- Use a dedicated Home Assistant token with the narrowest practical access.
- Do not place credentials in Codex prompts, Git history, logs, or telemetry.

## OpenTelemetry

Telemetry export is disabled by default. Enable it only after configuring a
trusted collector:

```dotenv
MCP_OTEL_ENABLED=1
OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=http://127.0.0.1:4318/v1/traces
```

The custom middleware records server and coarse risk metadata, never tool
arguments.

## Output controls

`MCP_RTK_COMPACT`, `MCP_RTK_THRESHOLD_CHARS`, and `MCP_MAX_OUTPUT_CHARS`
prevent noisy tool responses from overwhelming the client. Individual tools can
request exact output only when necessary.
