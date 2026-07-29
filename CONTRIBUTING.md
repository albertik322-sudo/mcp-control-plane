# Contributing

Changes should be small, reviewable, and safe to roll back.

## Workflow

1. Create a focused branch from `main`.
2. Do not include local configuration, runtime data, generated artifacts, or
   unrelated workspace changes.
3. Preserve MCP/API authorization boundaries and read-only defaults.
4. Add or update tests for behavior changes.
5. Run:

   ```powershell
   python -m pytest
   python skills\mcp-control-plane-audit\scripts\audit_control_plane.py --root . --fail-on medium
   powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run_mcp_conformance.ps1
   ```

6. Open a draft pull request describing impact, risk, verification, and
   rollback.

## Style

- Target Python 3.12.
- Prefer typed, closed output contracts for new tools.
- Keep MCP entrypoints thin and shared behavior in reusable modules.
- Bound tool output and never log secret-bearing arguments.
- Use environment variables or OS credential storage for secrets.
