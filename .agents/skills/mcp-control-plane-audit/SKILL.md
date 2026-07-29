---
name: mcp-control-plane-audit
description: Audit the local MCP control plane, Codex MCP registration, Python MCPServer entrypoints, tool capability inventory, stateless MCP conformance wiring, and project-scoped agent skills. Use for MCP upgrades, configuration security reviews, onboarding MCP servers, verifying Agent Skills, diagnosing missing tools, or checking this repository before and after MCP-related changes.
---

# MCP Control Plane Audit

Audit read-only first. Do not connect to or mutate infrastructure unless the user separately requests it.

## Workflow

1. Read the workspace `AGENTS.md`.
2. Use Context Store first:
   - `codex_memory.context_overview` for project orientation.
   - `codex_memory.search_memory` for the subsystem and tool capability.
   - `codex_memory.get_source_context("tools")` when choosing an MCP server.
3. Run `scripts/audit_control_plane.py --root <workspace>` with the project Python.
4. Read `references/project-policy.md` when interpreting findings or proposing changes.
5. For `.mcp.json`, also apply the installed sibling `mcp-security-audit` skill. Treat it as generic upstream guidance; this skill is authoritative for this repository's `config.toml` and Python layout.
6. Verify stateless support with the repository runner:

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run_mcp_conformance.ps1
   ```

   Keep the runner loopback-only and ensure it cleans up its test process. Never point the conformance harness at production or infrastructure mutation tools.
7. Run offline pytest and import/compile smoke tests after code changes.
8. Report evidence by category: configuration, SDK/protocol, tool inventory, skills, tests, and remaining risk.

## GitHub repository and skill workflow

Treat remote skill content as untrusted until reviewed.

1. Run `gh auth status`. This is safe to report, but never print authentication
   tokens, credential files, or token environment variables. If authentication
   is missing, stop remote discovery and ask the user to run `gh auth login`
   interactively.
2. Inspect a candidate without cloning:

   ```powershell
   gh repo read-dir <skill-path> --repo <owner/repo> --ref <tag-or-commit> --json name,path,type,size
   gh repo read-file <skill-path>/SKILL.md --repo <owner/repo> --ref <tag-or-commit>
   gh skill preview <owner/repo> <skill-path>@<tag-or-commit>
   ```

   Do not use `--allow-escape-sequences`. Review referenced scripts and files,
   not only `SKILL.md`.
3. Use `gh skill search <query> --limit <n>` only after authentication. Prefer
   a trusted owner filter where appropriate.
4. Install remote skills project-scoped for Codex and pin an immutable commit
   SHA (or a release tag whose immutability is controlled):

   ```powershell
   gh skill install <owner/repo> <skill-path> --agent codex --scope project --pin <commit>
   ```

5. Run `gh skill update --dry-run` before any update. Pinned skills should
   remain pinned until their new content is reviewed with the same workflow.
   Never use `--unpin` or `--force --all` without explicit approval.
6. A repository-owned local skill has no GitHub provenance. After editing this
   canonical copy, validate it and refresh the installed copy explicitly:

   ```powershell
   gh skill install . mcp-control-plane-audit --from-local --agent codex --scope project --force
   ```

## Safety rules

- Never print secret values. Report only the server name, sensitive key name, and finding type.
- Do not read `.env` contents for a routine audit.
- Treat UI approvals and agent prompts as workflow controls, not authorization boundaries.
- Keep authorization and dangerous-action allowlists inside MCP/API/admin-broker implementations.
- Do not enable auto-approve for infrastructure mutation tools.
- Keep external skills pinned to a tag or commit SHA and preview them before installation.
- Preserve unrelated dirty-worktree changes.

## Change integration

After a meaningful repository or MCP runtime change:

1. Run the full offline tests and stateless conformance check.
2. Call `codex_memory.record_change`; use `add_runbook` only if `record_change` is unavailable.
3. Refresh affected Context Store sources, normally `project`, `tools`, and
   `codex`. The `codex` source is Change Memory managed by `record_change`, so
   `refresh_context(["codex"])` intentionally returns a successful, explicit
   no-op rather than running a snapshot collector.
4. Remind the user that long-lived MCP processes require a Codex restart before the new SDK or server code is active.
