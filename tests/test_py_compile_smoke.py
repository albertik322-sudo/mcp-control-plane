from __future__ import annotations

import py_compile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]

SELECTED_MODULES = [
    "agent_orchestrator.py",
    "codex_memory.py",
    "codex_observability.py",
    "codex_workers.py",
    "doc_drift.py",
    "mcp_runtime.py",
    "windows_admin_policy.py",
    "windows_admin_broker.py",
    "memory_mcp.py",
    "workers_mcp.py",
    "server.py",
    "docker_mcp.py",
    "homelab_mcp.py",
    "mikrotik_mcp.py",
    "homeassistant_mcp.py",
    "tunnel_mcp.py",
    "windows_system_mcp.py",
    "windows_gui_mcp.py",
    "comfy_mcp.py",
    "steamdeck_mcp.py",
    "mailru_imap.py",
    "mailru_mcp.py",
]


@pytest.mark.parametrize("relative_path", SELECTED_MODULES)
def test_selected_module_py_compile(relative_path: str) -> None:
    path = ROOT / relative_path

    py_compile.compile(str(path), doraise=True)
