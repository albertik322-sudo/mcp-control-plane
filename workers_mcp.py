from __future__ import annotations

from pathlib import Path
from typing import Any

import codex_workers
import agent_orchestrator
from mcp.server.apps import client_supports_apps
from mcp.server.mcpserver import Context
from mcp_contracts import AppToolResult, MCPFeatureSupportReport, validate_structured
from mcp_extensions import (
    AppPilot,
    CodexWorkersTaskAdapter,
    DurableTaskStore,
    app_tool_result,
    bind_app_tool,
    build_apps_extension,
    extension_support,
)
from mcp_runtime import configure_stdio, create_mcp_server


configure_stdio()
ROOT = Path(__file__).resolve().parent
CONTROL_PLANE_APP = AppPilot(
    resource_uri="ui://codex-workers/control-plane.html",
    title="MCP control plane capabilities",
    description="Read-only MCP extension and worker capability dashboard.",
    fallback_text="MCP control-plane extension status is available as structured data.",
    html="""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>MCP control plane</title>
  <style>
    body { margin: 0; font: 14px system-ui; color: #dce7f7; background: #101722; }
    main { padding: 18px; } h1 { font-size: 18px; } pre { white-space: pre-wrap; }
    .badge { display: inline-block; margin: 4px; padding: 5px 9px; border-radius: 999px;
      background: #20334a; color: #9dd7ff; }
  </style>
</head>
<body><main>
  <h1>MCP control plane</h1>
  <p>Read-only extension status. Hosts without MCP Apps receive the same data as text.</p>
  <div><span class="badge">MCP 2026-07-28</span><span class="badge">Stateless</span>
    <span class="badge">Safe fallback</span></div>
  <pre id="data">Waiting for tool result…</pre>
</main>
<script>
  addEventListener("message", event => {
    const payload = event.data?.structuredContent ?? event.data?.result?.structuredContent ?? event.data;
    if (payload) document.getElementById("data").textContent = JSON.stringify(payload, null, 2);
  });
</script></body></html>""",
)
_apps = build_apps_extension([CONTROL_PLANE_APP])


def _control_plane_dashboard(ctx: Context) -> AppToolResult:
    support = extension_support()
    result = app_tool_result(
        pilot=CONTROL_PLANE_APP,
        data=support,
        client_supports_ui=client_supports_apps(ctx),
    )
    return validate_structured(AppToolResult, result)


if _apps is not None:
    bind_app_tool(
        _apps,
        CONTROL_PLANE_APP,
        _control_plane_dashboard,
        name="control_plane_dashboard",
        description="Return MCP extension status and render a read-only dashboard when the host supports MCP Apps.",
    )

mcp = create_mcp_server("codex-workers", extensions=[_apps] if _apps is not None else [])


def _task_adapter() -> CodexWorkersTaskAdapter:
    return CodexWorkersTaskAdapter(
        DurableTaskStore(ROOT / ".runtime" / "mcp_tasks"),
        submit_tasks=codex_workers.submit_tasks,
        batch_status=codex_workers.batch_status,
        batch_results=codex_workers.batch_results,
    )


@mcp.tool()
def mcp_extension_support() -> MCPFeatureSupportReport:
    """Return typed MCP Tasks and Apps feature detection for this runtime."""
    return validate_structured(MCPFeatureSupportReport, extension_support())


@mcp.tool()
def submit_durable_tasks(
    tasks: list[dict[str, Any]],
    max_parallel: int | None = None,
    ttl_ms: int = 86_400_000,
) -> dict[str, Any]:
    """Submit a worker batch behind an internal durable task handle.

    This is a Tasks-extension pilot, not a wire-level io.modelcontextprotocol/tasks
    implementation. The handle survives an MCP restart; the process-local worker
    execution itself does not.
    """
    return _task_adapter().submit(tasks, max_parallel=max_parallel, ttl_ms=ttl_ms)


@mcp.tool()
def durable_task_status(task_id: str, refresh_backend: bool = True) -> dict[str, Any]:
    """Return or refresh one internal durable worker-task handle."""
    adapter = _task_adapter()
    return adapter.refresh(task_id) if refresh_backend else adapter.store.get(task_id)


@mcp.tool()
def list_durable_tasks(limit: int = 20, include_expired: bool = False) -> dict[str, Any]:
    """List internal durable worker-task handles without task input payloads."""
    records = _task_adapter().store.list(include_expired=include_expired)
    return {"tasks": records[: max(1, min(int(limit), 200))]}


@mcp.tool()
def request_durable_task_cancel(task_id: str) -> dict[str, Any]:
    """Record a cooperative cancellation request.

    The current codex_workers backend has no cancellation callback, so this tool
    never claims that execution stopped until a future backend confirms it.
    """
    return _task_adapter().request_cancel(task_id)


@mcp.tool()
def submit_tasks(tasks: list[dict[str, Any]], max_parallel: int | None = None) -> dict[str, Any]:
    """Submit independent worker tasks and return immediately with a batch id."""
    return codex_workers.submit_tasks(tasks, max_parallel=max_parallel)


@mcp.tool()
def run_tasks(tasks: list[dict[str, Any]], timeout: float | None = 120, max_parallel: int | None = None) -> dict[str, Any]:
    """Submit independent worker tasks and wait for their results."""
    return codex_workers.run_tasks(tasks, timeout=timeout, max_parallel=max_parallel)


@mcp.tool()
def batch_status(batch_id: str) -> dict[str, Any]:
    """Return current status for a worker batch."""
    return codex_workers.batch_status(batch_id)


@mcp.tool()
def batch_results(batch_id: str) -> dict[str, Any]:
    """Return current status and available results for a worker batch."""
    return codex_workers.batch_results(batch_id)


@mcp.tool()
def wait_batch(batch_id: str, timeout: float | None = 120) -> dict[str, Any]:
    """Wait for a worker batch to finish or until timeout."""
    return codex_workers.wait_batch(batch_id, timeout=timeout)


@mcp.tool()
def list_batches(limit: int = 20) -> dict[str, Any]:
    """List recent worker batches in this MCP process."""
    return codex_workers.list_batches(limit=limit)


@mcp.tool()
def list_recipes() -> dict[str, Any]:
    """List built-in worker recipes."""
    return codex_workers.recipes()


@mcp.tool()
def run_recipe(name: str, timeout: float | None = 120) -> dict[str, Any]:
    """Run a built-in worker recipe such as server_quick or network_quick."""
    return codex_workers.run_recipe(name, wait_timeout=timeout)


@mcp.tool()
def start_implementation_branch(
    name: str,
    command: str,
    write_scopes: list[str],
    timeout: int = 1200,
    base_ref: str = "HEAD",
    branch_name: str | None = None,
) -> dict[str, Any]:
    """Start a bounded implementation branch in an isolated git worktree."""
    return codex_workers.start_implementation_branch(
        name=name,
        command=command,
        write_scopes=write_scopes,
        timeout=timeout,
        base_ref=base_ref,
        branch_name=branch_name,
    )


@mcp.tool()
def implementation_branch_status(branch_id: str) -> dict[str, Any]:
    """Return status for a bounded implementation branch."""
    return codex_workers.implementation_branch_status(branch_id)


@mcp.tool()
def implementation_branch_results(branch_id: str) -> dict[str, Any]:
    """Return manifest, artifacts, changed files, and command result for an implementation branch."""
    return codex_workers.implementation_branch_results(branch_id)


@mcp.tool()
def list_implementation_branches(limit: int = 20) -> dict[str, Any]:
    """List recent bounded implementation branches."""
    return codex_workers.list_implementation_branches(limit=limit)


@mcp.tool()
def plan_agent_branches(goal: str, branches: list[dict[str, Any]], include_prompts: bool = False) -> dict[str, Any]:
    """Create branch manifests for parallel Codex agents.

    Prompt/context generation is optional because it can touch Context Store and
    git. Keep planning fast by default, then call agent_branch_prompt per branch.
    """
    return agent_orchestrator.plan_branches(goal, branches, include_prompts=include_prompts)


@mcp.tool()
def agent_branch_prompt(branch_id: str) -> dict[str, Any]:
    """Return a spawn_agent-ready prompt for one planned agent branch."""
    return agent_orchestrator.build_agent_prompt(branch_id)


@mcp.tool()
def agent_context_pack(branch_id: str, max_search_results: int = 8) -> dict[str, Any]:
    """Build and return a compact full-context substitute for a planned agent branch."""
    return agent_orchestrator.build_context_pack(branch_id, max_search_results=max_search_results)


@mcp.tool()
def agent_branch_status(branch_id: str) -> dict[str, Any]:
    """Return manifest and recorded result for one planned agent branch."""
    return agent_orchestrator.branch_status(branch_id)


@mcp.tool()
def record_agent_branch_result(
    branch_id: str,
    status: str,
    summary: str,
    changed_files: list[str] | None = None,
    verification: list[str] | None = None,
    artifacts: list[str] | None = None,
    notes: list[str] | None = None,
) -> dict[str, Any]:
    """Record a completed free-form agent branch result for integration."""
    return agent_orchestrator.record_result(
        branch_id=branch_id,
        status=status,
        summary=summary,
        changed_files=changed_files,
        verification=verification,
        artifacts=artifacts,
        notes=notes,
    )


@mcp.tool()
def list_agent_branches(limit: int = 50) -> dict[str, Any]:
    """List planned free-form agent branches."""
    return agent_orchestrator.list_branches(limit=limit)


@mcp.tool()
def agent_integration_summary() -> dict[str, Any]:
    """Return open planned agent branches and recorded branch results."""
    return agent_orchestrator.integration_summary()


if __name__ == "__main__":
    mcp.run()
