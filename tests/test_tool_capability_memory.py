from __future__ import annotations

import sqlite3
from pathlib import Path

import codex_memory
import context_collector
import tool_capability_memory as tools


def test_parse_mcp_module_extracts_tools_without_importing(tmp_path: Path) -> None:
    module = tmp_path / "sample_mcp.py"
    module.write_text(
        """
from mcp.server import MCPServer
mcp = MCPServer("sample-server")

@mcp.tool()
def status(limit: int = 5) -> dict:
    \"\"\"Return safe status.\"\"\"
    return {}

@mcp.tool()
def admin_run(action: str) -> dict:
    \"\"\"Run an elevated allowlisted action.\"\"\"
    return {}
""",
        encoding="utf-8",
    )

    parsed = tools.parse_mcp_module(module)

    assert parsed["server_name"] == "sample-server"
    assert [tool["name"] for tool in parsed["tools"]] == ["admin_run", "status"]
    admin = parsed["tools"][0]
    assert "admin" in admin["tags"]
    assert "write" in admin["tags"]
    assert admin["risk"]["admin"] is True


def test_infer_tool_tags_marks_domains_and_risks() -> None:
    docker_tags = tools.infer_tool_tags("docker_mcp", "ubuntu-docker", "docker_logs", "docker_logs(container: str)", "Tail logs")
    route_tags = tools.infer_tool_tags("windows_system_mcp", "windows-system", "persistent_route_add", "persistent_route_add()", "Add route")

    assert {"docker", "logs", "read"} <= set(docker_tags)
    assert {"windows", "write"} <= set(route_tags)


def test_parse_mcp_module_includes_apps_extension_tool(tmp_path: Path) -> None:
    module = tmp_path / "apps_mcp.py"
    module.write_text(
        """
from mcp_runtime import create_mcp_server

def _dashboard() -> dict:
    return {}

bind_app_tool(apps, pilot, _dashboard, name="control_plane_dashboard")
mcp = create_mcp_server("apps-server", extensions=[apps])
""",
        encoding="utf-8",
    )

    parsed = tools.parse_mcp_module(module)

    assert parsed["server_name"] == "apps-server"
    assert parsed["tools"][0]["name"] == "control_plane_dashboard"
    assert parsed["tools"][0]["function"] == "_dashboard"


def test_collect_tool_capabilities_writes_searchable_documents(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    codex_memory.init_db(conn)
    tools.collect_tool_capabilities(conn, Path.cwd())
    conn.commit()

    modules = conn.execute("select * from documents where source='tools' and subject='mcp-modules'").fetchone()
    windows = conn.execute("select * from documents where source='tools' and subject='module:windows_system_mcp'").fetchone()
    search = codex_memory.search(conn, "windows admin broker", limit=5)

    assert modules is not None
    assert windows is not None
    assert any("admin" in row["snippet"].lower() for row in search)


def test_context_collector_exposes_tools_source() -> None:
    assert "tools" in context_collector.COLLECTORS
