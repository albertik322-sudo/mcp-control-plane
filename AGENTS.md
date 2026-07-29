# Agent instructions

This repository is an MCP control plane. Begin with Context Store orientation
and prefer the relevant MCP server over ad-hoc infrastructure shell commands.

## Safe workflow

1. Use `codex_memory` for project and tool capability context.
2. Use read-only health, audit, and snapshot tools before mutations.
3. Keep remote changes narrow, reversible, and explicitly authorized.
4. Never print or store secret values.
5. Preserve authorization checks inside MCP/API implementations.
6. Run offline tests, the control-plane audit, and stateless conformance after
   MCP runtime changes.
7. Restart long-lived MCP processes after code or dependency changes.

Do not commit `.env`, runtime state, infrastructure snapshots, captures,
firmware, generated artifacts, vendored tools, or private operational data.
