# Architecture

## Layers

| Layer | Main files | Responsibility |
| --- | --- | --- |
| Runtime | `mcp_runtime.py`, `mcp_observability.py` | Server creation, middleware, telemetry |
| Contracts | `mcp_contracts.py` | Closed Pydantic models and JSON Schema 2020-12 |
| Extensions | `mcp_extensions.py` | MCP Apps and durable internal Tasks |
| Context | `codex_memory.py`, `memory_mcp.py`, `context_collector.py` | Durable context, search, refresh, change memory |
| Workers | `codex_workers.py`, `workers_mcp.py`, `agent_orchestrator.py` | Bounded parallel and durable task execution |
| Infrastructure | `*_mcp.py` | Narrow subsystem adapters |
| Governance | `skills/`, `tests/`, `scripts/` | Audit, conformance, and supply-chain controls |

## Server factory

Every MCP entrypoint uses the shared server factory. This keeps request-state
middleware, W3C trace propagation, safe OpenTelemetry attributes, and extension
registration consistent.

## Tasks and Apps

The installed MCP SDK supports the Apps server extension. Tools must retain a
plain-text fallback because not every host negotiates embedded UI.

Protocol task models exist, but the SDK does not expose the server-side Tasks
wire extension. Durable worker handles are therefore internal-only and the
server deliberately does not advertise unsupported wire capability.

## Trust boundaries

Infrastructure credentials stay outside the repository. Each subsystem adapter
must validate inputs, enforce allowlists, and distinguish read-only inspection
from mutation. Agent instructions and UI approvals do not replace these checks.
