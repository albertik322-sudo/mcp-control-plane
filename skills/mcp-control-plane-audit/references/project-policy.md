# Project policy and tool map

## Mandatory routing

- Start with `codex_memory` for orientation and capability discovery.
- Use `windows_system`, `ubuntu_server`, `ubuntu_docker`, `homelab`, `mikrotik`, `homeassistant`, and `local_tunnels` for their named live subsystems.
- Use `codex_workers` for typed parallel checks.
- Use direct shell for repository edits, local tests, or when the relevant MCP surface is unavailable or ambiguous.

## Repository evidence

- `tool_capability_memory.py` builds the MCP tool inventory without importing live servers.
- `tests/test_live_allowlist.py` ensures the fast live layer exposes no mutation tools.
- `tests/test_import_smoke.py` imports every MCP entrypoint without live infrastructure.
- `tests/conformance_http_server.py` is the safe loopback-only protocol fixture.
- `scripts/run_mcp_conformance.ps1` runs the pinned MCP `2026-07-28` stateless scenario and cleans up the process.
- `.agents/skills/` contains project-scoped skills managed by `gh skill`.

## Authorization boundary

Agent instructions, prompts, hooks, and approvals can reduce mistakes but cannot replace server-side authorization. Preserve narrow MCP tools, allowlisted admin actions, credential separation, dry-run defaults, and explicit user approval for dangerous infrastructure changes.

## Skill supply chain

- Check `gh auth status` before remote discovery; authenticate interactively
  with `gh auth login` when needed, and never expose tokens in output.
- Inspect remote trees and every referenced file with `gh repo read-dir` and
  `gh repo read-file` at an explicit tag or commit before installation.
- Use `gh skill preview` before `gh skill install`.
- Install external skills at project scope, pin reviewed immutable revisions,
  and run `gh skill update --dry-run` before changing them.
- Local repository skills are refreshed with `--from-local`; they do not gain
  GitHub provenance until published and reinstalled from a GitHub repository.

## Reporting

Do not report secret values or raw sensitive configuration. Prefer counts, key names, file paths, line numbers, tool names, protocol versions, and test results.
