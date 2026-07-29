from __future__ import annotations

from mcp_observability import classify_tool_risk, load_observability_settings


def test_observability_is_disabled_without_opt_in(monkeypatch):
    monkeypatch.delenv("MCP_OTEL_ENABLED", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)

    settings = load_observability_settings()

    assert settings.enabled is False
    assert settings.exporter == "none"
    assert settings.endpoint is None


def test_otlp_endpoint_enables_exporter_without_returning_it(monkeypatch):
    monkeypatch.delenv("MCP_OTEL_ENABLED", raising=False)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "https://collector.invalid/v1/traces")

    settings = load_observability_settings()

    assert settings.enabled is True
    assert settings.exporter == "otlp-http"


def test_tool_risk_uses_only_the_tool_name():
    assert classify_tool_risk("admin_run") == {
        "write": True,
        "admin": True,
        "exec": False,
        "destructive": False,
    }
    assert classify_tool_risk("delete_backup")["destructive"] is True
    assert classify_tool_risk("laptop_health") == {
        "write": False,
        "admin": False,
        "exec": False,
        "destructive": False,
    }
