from __future__ import annotations

import importlib

import pytest


MCP_ENTRYPOINT_MODULES = [
    "memory_mcp",
    "workers_mcp",
    "server",
    "docker_mcp",
    "homelab_mcp",
    "mikrotik_mcp",
    "homeassistant_mcp",
    "tunnel_mcp",
    "windows_system_mcp",
    "windows_gui_mcp",
    "comfy_mcp",
    "steamdeck_mcp",
    "mailru_mcp",
]


@pytest.mark.parametrize("module_name", MCP_ENTRYPOINT_MODULES)
def test_mcp_entrypoint_imports_without_live_infra(module_name: str) -> None:
    module = importlib.import_module(module_name)

    assert hasattr(module, "mcp"), f"{module_name} should expose an MCPServer instance"


@pytest.mark.parametrize(
    "module_name",
    [
        "agent_orchestrator",
        "codex_memory",
        "codex_observability",
        "codex_workers",
        "doc_drift",
        "mcp_runtime",
    ],
)
def test_helper_module_imports(module_name: str) -> None:
    assert importlib.import_module(module_name)
