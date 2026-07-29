from __future__ import annotations

import os

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.subscriptions import PromptsListChanged, ToolsListChanged
from mcp.shared.exceptions import MCPError
from mcp_types import (
    MISSING_REQUIRED_CLIENT_CAPABILITY,
    ClientCapabilities,
    MissingRequiredClientCapabilityErrorData,
    SamplingCapability,
)


mcp = MCPServer(
    "mcp-control-plane-conformance",
    version="1.0.0",
    instructions="Safe loopback-only fixture for MCP protocol conformance checks.",
)


@mcp.tool()
def echo(text: str) -> str:
    """Return text unchanged without external side effects."""
    return text


@mcp.tool()
def add(a: int, b: int) -> int:
    """Add two integers without external side effects."""
    return a + b


@mcp.tool()
def test_missing_capability(ctx: Context) -> str:
    """Reject calls when the client did not declare the sampling capability."""
    capabilities = ctx.client_capabilities
    if capabilities is None or capabilities.sampling is None:
        data = MissingRequiredClientCapabilityErrorData(
            required_capabilities=ClientCapabilities(sampling=SamplingCapability())
        )
        raise MCPError(
            code=MISSING_REQUIRED_CLIENT_CAPABILITY,
            message="Client did not declare the sampling capability.",
            data=data.model_dump(by_alias=True, mode="json", exclude_none=True),
        )
    return "sampling capability declared"


@mcp.tool()
async def test_trigger_tool_change() -> str:
    """Add a harmless diagnostic tool and publish its list-change event."""

    def dynamic_conformance_tool() -> str:
        return "dynamic tool ready"

    mcp.add_tool(dynamic_conformance_tool, name="dynamic_conformance_tool")
    await mcp._subscriptions.publish(ToolsListChanged())
    return "tool list changed"


@mcp.tool()
async def test_trigger_prompt_change() -> str:
    """Add a harmless diagnostic prompt and publish its list-change event."""

    def dynamic_conformance_prompt() -> str:
        return "Inspect the safe conformance fixture."

    mcp.prompt(name="dynamic_conformance_prompt")(dynamic_conformance_prompt)
    await mcp._subscriptions.publish(PromptsListChanged())
    return "prompt list changed"


@mcp.resource("control-plane://status")
def status() -> str:
    """Return the fixed status of the conformance fixture."""
    return "ready"


@mcp.prompt()
def inspect_control_plane(area: str = "mcp") -> str:
    """Return a harmless inspection prompt."""
    return f"Inspect the {area} control plane without making changes."


if __name__ == "__main__":
    mcp.run(
        transport="streamable-http",
        host="127.0.0.1",
        port=int(os.getenv("MCP_CONFORMANCE_PORT", "3017")),
        stateless_http=True,
        json_response=True,
    )
