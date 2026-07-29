from __future__ import annotations

import os
import sys
import threading
from dataclasses import dataclass
from typing import Any

from opentelemetry.trace import get_current_span

from mcp.server.context import CallNext, HandlerResult, ServerMiddleware, ServerRequestContext


_TRUE_VALUES = {"1", "true", "yes", "on"}
_WRITE_WORDS = {
    "add",
    "apply",
    "create",
    "delete",
    "disable",
    "edit",
    "install",
    "move",
    "patch",
    "record",
    "refresh",
    "reload",
    "remove",
    "restart",
    "run",
    "set",
    "start",
    "stop",
    "toggle",
    "update",
    "upload",
    "write",
}
_ADMIN_WORDS = {"admin", "defender", "elevated", "route", "sudo"}
_EXEC_WORDS = {"command", "exec", "powershell", "script", "shell", "ssh"}
_DESTRUCTIVE_WORDS = {"delete", "disable", "kill", "prune", "remove", "revoke", "stop"}

_CONFIGURE_LOCK = threading.Lock()
_CONFIGURED = False
_CONFIGURATION: dict[str, Any] = {"enabled": False, "exporter": "none"}


@dataclass(frozen=True)
class ObservabilitySettings:
    enabled: bool
    exporter: str
    endpoint: str | None


def _enabled(value: str | None) -> bool:
    return str(value or "").strip().lower() in _TRUE_VALUES


def load_observability_settings() -> ObservabilitySettings:
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "").strip() or os.getenv(
        "OTEL_EXPORTER_OTLP_ENDPOINT", ""
    ).strip()
    enabled = _enabled(os.getenv("MCP_OTEL_ENABLED")) or bool(endpoint)
    exporter = "otlp-http" if endpoint else ("console" if enabled else "none")
    return ObservabilitySettings(enabled=enabled, exporter=exporter, endpoint=endpoint or None)


def configure_otel(service_name: str) -> dict[str, Any]:
    """Configure an opt-in trace exporter without exposing endpoint credentials.

    MCP 2.0 already creates spans and propagates W3C trace context. This function
    only installs an SDK provider/exporter when explicitly enabled. With no
    environment opt-in the OpenTelemetry API remains a zero-cost no-op.
    """

    global _CONFIGURED, _CONFIGURATION
    settings = load_observability_settings()
    if not settings.enabled:
        return {"enabled": False, "exporter": "none"}

    with _CONFIGURE_LOCK:
        if _CONFIGURED:
            return dict(_CONFIGURATION)

        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter, SimpleSpanProcessor

        provider = TracerProvider(
            resource=Resource.create(
                {
                    "service.name": service_name,
                    "service.namespace": "codex-mcp",
                }
            )
        )
        if settings.exporter == "otlp-http":
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.endpoint)))
        else:
            provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter(out=sys.stderr)))
        trace.set_tracer_provider(provider)
        _CONFIGURED = True
        _CONFIGURATION = {"enabled": True, "exporter": settings.exporter}
        return dict(_CONFIGURATION)


def classify_tool_risk(tool_name: str) -> dict[str, bool]:
    words = {part for part in tool_name.lower().replace("-", "_").split("_") if part}
    return {
        "write": bool(words & _WRITE_WORDS),
        "admin": bool(words & _ADMIN_WORDS),
        "exec": bool(words & _EXEC_WORDS),
        "destructive": bool(words & _DESTRUCTIVE_WORDS),
    }


class SafeMCPAttributesMiddleware(ServerMiddleware[Any]):
    """Add bounded server/tool risk attributes to the SDK's current span."""

    def __init__(self, server_name: str):
        self.server_name = server_name

    async def __call__(self, ctx: ServerRequestContext[Any, Any], call_next: CallNext) -> HandlerResult:
        span = get_current_span()
        span.set_attribute("mcp.server.name", self.server_name)
        if ctx.method == "tools/call":
            raw_name = ctx.params.get("name") if ctx.params else None
            if isinstance(raw_name, str):
                risk = classify_tool_risk(raw_name)
                span.set_attribute("mcp.tool.risk.write", risk["write"])
                span.set_attribute("mcp.tool.risk.admin", risk["admin"])
                span.set_attribute("mcp.tool.risk.exec", risk["exec"])
                span.set_attribute("mcp.tool.risk.destructive", risk["destructive"])
        return await call_next(ctx)
