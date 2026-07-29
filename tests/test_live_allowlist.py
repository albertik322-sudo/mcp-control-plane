from __future__ import annotations


LIVE_FAST_ALLOWLIST = {
    "windows_system_mcp": {"powershell_runtime", "system_summary", "top_processes", "network_summary"},
    "server": {"system_overview", "service_status"},
    "docker_mcp": {"docker_info", "docker_ps", "docker_unhealthy"},
    "mikrotik_mcp": {"probe"},
    "homeassistant_mcp": {"health", "config", "search_states"},
    "homelab_mcp": {"prometheus_query", "prometheus_alerts", "homelab_health"},
}

FORBIDDEN_NAME_PARTS = {
    "action",
    "apply",
    "call_service",
    "compose",
    "delete",
    "disable",
    "edit",
    "exec",
    "install",
    "kill",
    "patch",
    "powershell",
    "prune",
    "restart",
    "run_command",
    "run_sudo",
    "script",
    "set_",
    "start_",
    "stop",
    "toggle",
    "write",
}

ALLOWED_EXCEPTIONS = {"powershell_runtime"}


def test_live_fast_allowlist_has_no_mutation_tools() -> None:
    for module, names in LIVE_FAST_ALLOWLIST.items():
        for name in names:
            if name in ALLOWED_EXCEPTIONS:
                continue
            lowered = name.lower()
            assert not any(part in lowered for part in FORBIDDEN_NAME_PARTS), f"{module}.{name} should not be in fast live smoke"
