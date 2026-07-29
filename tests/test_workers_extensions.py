from __future__ import annotations

import workers_mcp
from mcp_contracts import JSON_SCHEMA_2020_12


def _tools():
    return {tool.name: tool for tool in workers_mcp.mcp._tool_manager.list_tools()}


def test_workers_advertises_apps_but_not_unsupported_tasks_wire_extension():
    extensions = workers_mcp.mcp._lowlevel_server.extensions

    assert "io.modelcontextprotocol/ui" in extensions
    assert "io.modelcontextprotocol/tasks" not in extensions


def test_feature_status_has_typed_output_schema():
    tool = _tools()["mcp_extension_support"]

    assert tool.output_schema["$schema"] == JSON_SCHEMA_2020_12
    assert tool.output_schema["additionalProperties"] is False
    assert workers_mcp.mcp_extension_support().tasks.wire_supported is False
    assert workers_mcp.mcp_extension_support().apps.wire_supported is True


def test_apps_tool_and_internal_task_pilot_are_registered():
    tools = _tools()

    assert tools["control_plane_dashboard"].meta["ui"]["resourceUri"] == workers_mcp.CONTROL_PLANE_APP.resource_uri
    assert {
        "submit_durable_tasks",
        "durable_task_status",
        "list_durable_tasks",
        "request_durable_task_cancel",
    } <= set(tools)
