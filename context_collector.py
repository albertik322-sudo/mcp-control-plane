from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any, Callable

import codex_memory as mem


DEFAULT_TTL = {
    "tools": 24 * 60 * 60,
    "windows": 15 * 60,
    "server": 15 * 60,
    "docker": 10 * 60,
    "mikrotik": 15 * 60,
    "homeassistant": 15 * 60,
    "homelab": 20 * 60,
    "project": 24 * 60 * 60,
}

# Sources that exist in Context Store but are maintained by a write API rather
# than a snapshot collector. Accepting them explicitly keeps Change Memory
# refresh lists honest without pretending that a collector rebuilt the data.
MANAGED_SOURCES = {
    "codex": "Change Memory is updated by record_change; no snapshot collector is required",
}


def store(conn, source: str, subject: str, title: str, payload: Any, tags: list[str] | None = None, ttl: int | None = None) -> None:
    mem.upsert_document(
        conn,
        kind="snapshot",
        source=source,
        subject=subject,
        title=title,
        payload=payload,
        tags=tags or [source],
        ttl_seconds=ttl if ttl is not None else DEFAULT_TTL.get(source),
    )


def store_error(conn, source: str, subject: str, title: str, exc: Exception) -> None:
    mem.upsert_document(
        conn,
        kind="snapshot",
        source=source,
        subject=subject,
        title=title,
        payload={"error": str(exc), "type": type(exc).__name__},
        tags=[source, "error"],
        ttl_seconds=10 * 60,
        status="error",
        error=str(exc),
    )


def call(
    conn,
    source: str,
    subject: str,
    title: str,
    fn: Callable[[], Any],
    tags: list[str] | None = None,
    ttl: int | None = None,
) -> Any | None:
    try:
        payload = fn()
        store(conn, source, subject, title, payload, tags, ttl)
        conn.commit()
        return payload
    except Exception as exc:
        store_error(conn, source, subject, title, exc)
        conn.commit()
        return None


def collect_project(conn) -> None:
    root = Path(__file__).resolve().parent
    facts = {
        ("server", "ubuntu_host"): os.getenv("REMOTE_HOST", "192.168.1.10"),
        ("server", "ssh_user"): os.getenv("REMOTE_USER", "automation"),
        ("router", "mikrotik_host"): os.getenv("MIKROTIK_HOST", "192.168.1.1"),
        ("homeassistant", "url"): os.getenv("HOMEASSISTANT_URL", "http://192.168.1.20:8123"),
        ("homelab", "homepage_url"): os.getenv("HOMEPAGE_URL", "http://home.example.lan"),
        ("homelab", "grafana_url"): os.getenv("GRAFANA_URL", "http://grafana.example.lan"),
        ("homelab", "uptime_kuma_url"): os.getenv("UPTIME_KUMA_URL", "http://status.example.lan"),
        ("homelab", "prometheus_url"): os.getenv("PROMETHEUS_URL", "http://prometheus.example.lan"),
        ("network", "lan_cidr"): os.getenv("LAN_CIDR", "192.168.1.0/24"),
        ("project", "root"): str(root),
    }
    for (namespace, key), value in facts.items():
        mem.upsert_fact(conn, namespace=namespace, key=key, value=value, source="project", tags=[namespace, "inventory"])
    files = ["AGENTS.md", "README.md", ".env.example", "requirements.txt", ".gitignore"]
    for name in files:
        path = root / name
        if path.exists():
            store(conn, "project", name, f"Project file {name}", path.read_text(encoding="utf-8", errors="replace"), ["project", "file"], DEFAULT_TTL["project"])
    py_files = sorted(p.name for p in root.glob("*.py"))
    store(conn, "project", "python-files", "Project Python files", py_files, ["project", "inventory"], DEFAULT_TTL["project"])
    try:
        import subprocess

        status = subprocess.run(
            ["git", "status", "--short", "--branch"],
            cwd=root,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=10,
        )
        log = subprocess.run(
            ["git", "log", "--oneline", "-10"],
            cwd=root,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=10,
        )
        store(
            conn,
            "project",
            "git",
            "Git status and recent commits",
            {"status": status.stdout, "status_stderr": status.stderr, "recent_commits": log.stdout},
            ["project", "git"],
            60 * 60,
        )
    except Exception as exc:
        store_error(conn, "project", "git", "Git status", exc)


def collect_tools(conn) -> None:
    import tool_capability_memory as tools

    tools.collect_tool_capabilities(conn, Path(__file__).resolve().parent)


def collect_windows(conn) -> None:
    import windows_system_mcp as w

    call(conn, "windows", "system-summary", "Windows system summary", w.system_summary, ["windows", "system"])
    call(conn, "windows", "battery-status", "Windows battery status", w.battery_status, ["windows", "battery"])
    call(conn, "windows", "disk-health", "Windows disk and volume health", w.disk_health, ["windows", "storage"], 60 * 60)
    call(conn, "windows", "process-groups", "Windows process groups by memory", lambda: w.process_groups(80), ["windows", "processes"])
    call(conn, "windows", "top-processes-memory", "Windows top memory processes", lambda: w.top_processes(80, "memory"), ["windows", "processes", "memory"])
    call(conn, "windows", "startup-items", "Windows startup items", w.startup_items, ["windows", "startup"], 60 * 60)
    call(conn, "windows", "services-running", "Windows running services", lambda: w.services("Running"), ["windows", "services"])
    call(conn, "windows", "network-summary", "Windows network summary", w.network_summary, ["windows", "network"])
    call(conn, "windows", "wifi-status", "Windows Wi-Fi status", w.wifi_status, ["windows", "wifi"])
    router_host = os.getenv("MIKROTIK_HOST", "192.168.1.1")
    call(conn, "windows", "dns-router-test", "Windows DNS test through MikroTik", lambda: w.dns_test("github.com", router_host, 2), ["windows", "dns"])
    call(conn, "windows", "defender-status", "Windows Defender status", w.defender_status, ["windows", "defender", "security"], 60 * 60)
    call(conn, "windows", "windows-update-summary", "Windows Update summary", w.windows_update_summary, ["windows", "updates"], 60 * 60)
    call(conn, "windows", "system-errors-24h", "Windows System error events in 24h", lambda: w.event_log(log_name="System", level="Error", hours=24, limit=50), ["windows", "events"])
    call(conn, "windows", "admin-broker-capabilities", "Windows elevated admin broker capabilities", w.admin_capabilities, ["windows", "admin-broker", "mcp"], 60 * 60)


def collect_server(conn) -> None:
    import server

    call(conn, "server", "server-health", "Ubuntu server health bundle", server.server_health, ["server", "health"])
    call(conn, "server", "system-overview", "Ubuntu server system overview", server.system_overview, ["server", "system"])
    call(conn, "server", "hardware-overview", "Ubuntu server hardware overview", server.hardware_overview, ["server", "hardware"], 60 * 60)
    call(conn, "server", "services", "Ubuntu important service status", server.service_status, ["server", "systemd"])
    call(conn, "server", "security-audit", "Ubuntu security audit", server.security_audit, ["server", "security"], 60 * 60)
    call(conn, "server", "apt-summary", "Ubuntu apt summary", server.apt_summary, ["server", "apt"], 60 * 60)
    call(conn, "server", "apt-security-summary", "Ubuntu apt security summary", server.apt_security_summary, ["server", "apt", "security"], 60 * 60)
    call(conn, "server", "ports", "Ubuntu listening ports", server.ports_list, ["server", "network"])
    call(conn, "server", "ufw", "Ubuntu UFW status", server.ufw_status, ["server", "firewall"])
    call(conn, "server", "processes", "Ubuntu top processes", lambda: server.process_list(120), ["server", "processes"])
    call(conn, "server", "nextcloud-status", "Nextcloud status", server.nextcloud_status, ["server", "nextcloud"])
    call(conn, "server", "nextcloud-log", "Nextcloud recent log", lambda: server.nextcloud_log(120), ["server", "nextcloud", "logs"])
    call(
        conn,
        "server",
        "config-inventory",
        "Ubuntu config inventory",
        lambda: server._exec(
            "find /etc/apache2/sites-available /etc/systemd/system /opt -maxdepth 3 -type f 2>/dev/null | sort | head -500",
            timeout=30,
            sudo=True,
        ),
        ["server", "inventory", "config"],
        60 * 60,
    )
    call(
        conn,
        "server",
        "timers",
        "Ubuntu systemd timers",
        lambda: server._exec("systemctl list-timers --all --no-pager", timeout=30, sudo=True),
        ["server", "systemd", "timers"],
        60 * 60,
    )


def collect_docker(conn) -> None:
    import docker_mcp as d

    call(conn, "docker", "health-overview", "Docker health overview", d.docker_health_overview, ["docker", "health"])
    call(conn, "docker", "containers", "Docker containers", d.docker_ps, ["docker", "containers"])
    call(conn, "docker", "unhealthy", "Docker unhealthy containers", d.docker_unhealthy, ["docker", "health"])
    call(conn, "docker", "security-audit", "Docker security audit", d.docker_security_audit, ["docker", "security"], 60 * 60)
    call(conn, "docker", "compose-projects", "Docker Compose projects", d.docker_compose_projects, ["docker", "compose", "inventory"], 60 * 60)
    call(conn, "docker", "stats", "Docker one-shot stats", d.docker_stats, ["docker", "metrics"])
    call(conn, "docker", "disk-usage", "Docker disk usage", d.docker_disk_usage, ["docker", "storage"])


def collect_mikrotik(conn) -> None:
    import mikrotik_mcp as r

    call(conn, "mikrotik", "router-health", "MikroTik router health", r.router_health, ["mikrotik", "health"])
    call(conn, "mikrotik", "security-audit", "MikroTik security audit", r.security_audit, ["mikrotik", "security"], 60 * 60)
    call(conn, "mikrotik", "safe-snapshot", "MikroTik safe snapshot", r.safe_snapshot, ["mikrotik", "router"])
    call(conn, "mikrotik", "neighbors", "MikroTik neighbors", r.neighbors, ["mikrotik", "neighbors", "inventory"])
    call(conn, "mikrotik", "arp-table", "MikroTik ARP table", r.arp_table, ["mikrotik", "arp", "inventory"])
    call(conn, "mikrotik", "wifi-registration", "MikroTik Wi-Fi registrations", r.wifi_registration, ["mikrotik", "wifi"])
    call(conn, "mikrotik", "dhcp-leases", "MikroTik DHCP leases", r.dhcp_leases, ["mikrotik", "dhcp", "inventory"])
    call(conn, "mikrotik", "dhcp-servers", "MikroTik DHCP servers", r.dhcp_servers, ["mikrotik", "dhcp"])
    call(conn, "mikrotik", "dns-settings", "MikroTik DNS settings", r.dns_settings, ["mikrotik", "dns"])
    call(conn, "mikrotik", "interfaces", "MikroTik interfaces", r.interfaces, ["mikrotik", "interfaces"])
    call(conn, "mikrotik", "wireguard-peers", "MikroTik WireGuard peers", r.wireguard_peers, ["mikrotik", "wireguard"])
    call(conn, "mikrotik", "recent-logs", "MikroTik recent logs", lambda: r.router_logs(), ["mikrotik", "logs"])
    call(conn, "mikrotik", "export-hide-sensitive", "MikroTik export hide-sensitive", lambda: r.export_config(True, True, 40), ["mikrotik", "config"], 60 * 60)


def collect_homeassistant(conn) -> None:
    import homeassistant_mcp as h

    call(conn, "homeassistant", "health", "Home Assistant health", h.health, ["homeassistant", "health"])
    call(conn, "homeassistant", "config", "Home Assistant config", h.config, ["homeassistant", "config"], 60 * 60)
    call(conn, "homeassistant", "areas", "Home Assistant areas", h.areas, ["homeassistant", "inventory"])
    call(conn, "homeassistant", "devices", "Home Assistant devices", h.devices, ["homeassistant", "inventory"])
    call(conn, "homeassistant", "entities", "Home Assistant entity registry", h.entities, ["homeassistant", "inventory"])
    call(conn, "homeassistant", "states", "Home Assistant states", h.states, ["homeassistant", "states"])
    call(conn, "homeassistant", "yandex-entities", "Home Assistant Yandex entities", h.yandex_entities, ["homeassistant", "yandex"])


def collect_homelab(conn) -> None:
    import homelab_mcp as h
    import server as srv

    call(conn, "homelab", "health", "Homelab health", h.homelab_health, ["homelab", "health"])
    call(conn, "homelab", "prometheus-alerts", "Prometheus alerts", h.prometheus_alerts, ["prometheus", "alerts"])
    call(conn, "homelab", "prometheus-targets", "Prometheus targets", h.prometheus_targets, ["prometheus", "targets"])
    call(conn, "homelab", "prometheus-rules", "Prometheus rules", h.prometheus_rules, ["prometheus", "rules"], 60 * 60)
    call(conn, "homelab", "uptime-kuma", "Uptime Kuma monitors", h.uptime_kuma_snapshot, ["uptime-kuma", "monitors"])
    call(conn, "homelab", "grafana-dashboards", "Grafana dashboards", h.grafana_dashboards, ["grafana", "dashboards"])
    call(conn, "homelab", "homepage-config", "Homepage config", h.homepage_config, ["homepage", "config"], 60 * 60)
    call(
        conn,
        "homelab",
        "infrastructure-md",
        "Homelab infrastructure context pack",
        lambda: srv.read_file("/opt/homelab-observability/docs/INFRASTRUCTURE.md", max_bytes=512_000),
        ["homelab", "documentation", "infrastructure", "topology", "desired-state"],
        24 * 60 * 60,
    )
    call(
        conn,
        "homelab",
        "runbook-md",
        "Homelab operations runbook",
        lambda: srv.read_file("/opt/homelab-observability/docs/RUNBOOK.md", max_bytes=512_000),
        ["homelab", "documentation", "runbook", "operations", "recovery"],
        24 * 60 * 60,
    )
    call(
        conn,
        "homelab",
        "doc-drift",
        "Homelab documentation drift check",
        h.doc_drift_check,
        ["homelab", "documentation", "drift", "desired-state"],
        60 * 60,
    )


COLLECTORS: dict[str, Callable[[Any], None]] = {
    "project": collect_project,
    "tools": collect_tools,
    "windows": collect_windows,
    "server": collect_server,
    "docker": collect_docker,
    "mikrotik": collect_mikrotik,
    "homeassistant": collect_homeassistant,
    "homelab": collect_homelab,
}


def publish_observability() -> dict[str, Any]:
    import codex_observability

    status = codex_observability.write_status_files()
    publish = codex_observability.publish_status_to_server(status)
    return {"ok": True, "status": status.get("status"), "publish": publish}


def collect_selected(sources: list[str] | None = None, publish: bool = True) -> dict[str, Any]:
    selected = sources or list(COLLECTORS)
    results: dict[str, Any] = {}
    with mem.connect() as conn:
        for source in selected:
            started = mem.now_ts()
            fn = COLLECTORS.get(source)
            if not fn:
                managed_reason = MANAGED_SOURCES.get(source)
                if managed_reason:
                    results[source] = {
                        "ok": True,
                        "skipped": True,
                        "managed": True,
                        "reason": managed_reason,
                    }
                else:
                    results[source] = {"ok": False, "error": "unknown source"}
                continue
            try:
                fn(conn)
                conn.commit()
                mem.record_run(conn, source, "ok", started)
                conn.commit()
                results[source] = {"ok": True, "duration_seconds": round(mem.now_ts() - started, 3)}
            except Exception as exc:
                conn.rollback()
                with mem.connect() as err_conn:
                    store_error(err_conn, source, "__collector__", f"{source} collector failure", exc)
                    mem.record_run(err_conn, source, "error", started, str(exc))
                    err_conn.commit()
                results[source] = {"ok": False, "error": str(exc), "type": type(exc).__name__}
    if publish:
        try:
            results["codex_observability"] = publish_observability()
        except Exception as exc:
            results["codex_observability"] = {"ok": False, "error": str(exc), "type": type(exc).__name__}
    return results


def loop(interval: int, sources: list[str] | None = None) -> None:
    while True:
        print(json.dumps({"time": mem.iso(), "result": collect_selected(sources)}, ensure_ascii=False), flush=True)
        time.sleep(max(30, int(interval)))


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect Codex context into SQLite")
    parser.add_argument(
        "--source",
        action="append",
        choices=sorted(set(COLLECTORS) | set(MANAGED_SOURCES)),
        help="Refresh only this source; managed sources may return an explicit no-op",
    )
    parser.add_argument("--no-observability", action="store_true", help="Skip MCP observability status publishing")
    parser.add_argument("--observability-only", action="store_true", help="Only publish MCP observability status")
    parser.add_argument("--loop", action="store_true", help="Run forever")
    parser.add_argument("--interval", type=int, default=900, help="Loop interval in seconds")
    args = parser.parse_args()
    if args.observability_only:
        print(json.dumps({"codex_observability": publish_observability()}, ensure_ascii=False, indent=2))
        return
    if args.loop:
        loop(args.interval, args.source)
    else:
        print(json.dumps(collect_selected(args.source, publish=not args.no_observability), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
