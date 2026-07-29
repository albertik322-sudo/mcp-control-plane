from __future__ import annotations

import json
import os
import shlex
import time
from dataclasses import dataclass
from typing import Any

import httpx
import paramiko
from dotenv import load_dotenv
from mcp_runtime import compact_command_result, configure_stdio, create_mcp_server
from ssh_auth import ssh_connect_kwargs


configure_stdio()
load_dotenv(encoding="utf-8-sig")
mcp = create_mcp_server("homelab-observability")


@dataclass(frozen=True)
class RemoteConfig:
    host: str
    port: int
    user: str
    password: str | None
    sudo_password: str | None
    timeout: float


def _remote_config() -> RemoteConfig:
    host = os.getenv("REMOTE_HOST", "").strip()
    user = os.getenv("REMOTE_USER", "").strip()
    if not host or not user:
        raise RuntimeError("REMOTE_HOST and REMOTE_USER must be set in .env")
    return RemoteConfig(
        host=host,
        port=int(os.getenv("REMOTE_PORT", "22")),
        user=user,
        password=os.getenv("REMOTE_PASSWORD") or None,
        sudo_password=os.getenv("REMOTE_SUDO_PASSWORD") or os.getenv("REMOTE_PASSWORD") or None,
        timeout=float(os.getenv("REMOTE_CONNECT_TIMEOUT", "15")),
    )


def _ssh_exec(command: str, timeout: int = 60, sudo: bool = False) -> dict[str, Any]:
    cfg = _remote_config()
    command = f"export LANG=C.UTF-8 LC_ALL=C.UTF-8 PYTHONIOENCODING=utf-8 PYTHONUTF8=1; {command}"
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    started = time.time()
    try:
        client.connect(
            hostname=cfg.host,
            port=cfg.port,
            username=cfg.user,
            **ssh_connect_kwargs(cfg.password),
            timeout=cfg.timeout,
            banner_timeout=cfg.timeout,
            auth_timeout=cfg.timeout,
        )
        if sudo:
            if not cfg.sudo_password:
                raise RuntimeError("REMOTE_SUDO_PASSWORD or REMOTE_PASSWORD is required for sudo")
            remote_command = f"sudo -S -p '' bash -lc {shlex.quote(command)}"
        else:
            remote_command = f"bash -lc {shlex.quote(command)}"
        stdin, stdout, stderr = client.exec_command(remote_command, get_pty=False, timeout=timeout)
        if sudo:
            stdin.write(cfg.sudo_password + "\n")
            stdin.flush()
        exit_code = stdout.channel.recv_exit_status()
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
    finally:
        client.close()
    if cfg.sudo_password:
        out = out.replace(cfg.sudo_password, "[redacted]")
        err = err.replace(cfg.sudo_password, "[redacted]")
    return {
        "command": command,
        "exit_code": exit_code,
        "stdout": out,
        "stderr": err,
        "duration_seconds": round(time.time() - started, 3),
    }


def _url(name: str, default: str) -> str:
    return os.getenv(name, default).rstrip("/")


def _prom_url() -> str:
    return _url("PROMETHEUS_URL", "http://prometheus.example.lan")


def _grafana_url() -> str:
    return _url("GRAFANA_URL", "http://grafana.example.lan")


def _kuma_url() -> str:
    return _url("UPTIME_KUMA_URL", "http://status.example.lan")


def _homepage_url() -> str:
    return _url("HOMEPAGE_URL", "http://home.example.lan")


def _http_get_json(url: str, params: dict[str, Any] | None = None, timeout: float = 15.0) -> Any:
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        response = client.get(url, params=params)
    response.raise_for_status()
    return response.json()


@mcp.tool()
def prometheus_query(query: str, time_: str | None = None) -> dict[str, Any]:
    """Run an instant Prometheus query."""
    params: dict[str, Any] = {"query": query}
    if time_:
        params["time"] = time_
    return _http_get_json(f"{_prom_url()}/api/v1/query", params=params)


@mcp.tool()
def prometheus_range_query(query: str, start: str, end: str, step: str = "60s") -> dict[str, Any]:
    """Run a Prometheus range query."""
    return _http_get_json(
        f"{_prom_url()}/api/v1/query_range",
        params={"query": query, "start": start, "end": end, "step": step},
    )


@mcp.tool()
def prometheus_alerts() -> dict[str, Any]:
    """Return active Prometheus alerts."""
    return _http_get_json(f"{_prom_url()}/api/v1/alerts")


@mcp.tool()
def prometheus_targets() -> dict[str, Any]:
    """Return Prometheus scrape targets."""
    return _http_get_json(f"{_prom_url()}/api/v1/targets")


@mcp.tool()
def prometheus_rules() -> dict[str, Any]:
    """Return Prometheus alerting and recording rules."""
    return _http_get_json(f"{_prom_url()}/api/v1/rules")


@mcp.tool()
def homelab_health() -> dict[str, Any]:
    """Return a compact health summary for Homepage, Grafana, Uptime Kuma, and Prometheus."""
    urls = {
        "homepage": _homepage_url(),
        "grafana": _grafana_url(),
        "uptime_kuma": _kuma_url(),
        "prometheus": _prom_url(),
    }
    results: dict[str, Any] = {}
    with httpx.Client(timeout=10, follow_redirects=False) as client:
        for name, url in urls.items():
            started = time.time()
            try:
                response = client.get(url)
                results[name] = {
                    "url": url,
                    "status_code": response.status_code,
                    "ok": response.status_code < 500,
                    "duration_seconds": round(time.time() - started, 3),
                }
            except Exception as exc:
                results[name] = {"url": url, "ok": False, "error": str(exc)}
    alerts = prometheus_alerts()
    targets = prometheus_targets()
    active_targets = targets.get("data", {}).get("activeTargets", [])
    return {
        "services": results,
        "prometheus_alert_count": len(alerts.get("data", {}).get("alerts", [])),
        "prometheus_targets": {
            "total": len(active_targets),
            "down": sum(1 for row in active_targets if row.get("health") != "up"),
        },
    }


@mcp.tool()
def uptime_kuma_snapshot() -> dict[str, Any]:
    """Read Uptime Kuma monitors and latest heartbeat directly from the server SQLite DB."""
    script = r"""
python3 - <<'PY'
import json, sqlite3
p='/opt/homelab-observability/uptime-kuma/kuma.db'
conn=sqlite3.connect(p)
conn.row_factory=sqlite3.Row
query = '''
select m.id,m.name,m.type,m.url,m.hostname,m.port,m.active,
       h.status,h.msg,h.ping,h.time
from monitor m
left join heartbeat h on h.id=(
    select id from heartbeat where monitor_id=m.id order by id desc limit 1
)
order by m.id
'''
rows=[dict(r) for r in conn.execute(query)]
print(json.dumps(rows, ensure_ascii=False))
PY
"""
    result = _ssh_exec(script, timeout=30, sudo=True)
    try:
        result["monitors"] = json.loads(result["stdout"])
    except json.JSONDecodeError as exc:
        result["parse_error"] = str(exc)
    return result


@mcp.tool()
def grafana_dashboards() -> dict[str, Any]:
    """List Grafana dashboards through the local Grafana API on the server."""
    script = r"""
PASS=$(awk -F': ' '/^Password:/ {print $2}' /root/homelab-grafana-admin.txt 2>/dev/null)
AUTH_FILE=$(mktemp)
trap 'rm -f "$AUTH_FILE"' EXIT
chmod 600 "$AUTH_FILE"
printf 'machine 127.0.0.1 login admin password %s\n' "$PASS" > "$AUTH_FILE"
curl -sS --netrc-file "$AUTH_FILE" 'http://127.0.0.1:3001/api/search?type=dash-db'
"""
    result = _ssh_exec(script, timeout=30, sudo=True)
    try:
        result["dashboards"] = json.loads(result["stdout"])
    except json.JSONDecodeError as exc:
        result["parse_error"] = str(exc)
    return result


@mcp.tool()
def grafana_dashboard(uid: str) -> dict[str, Any]:
    """Fetch one Grafana dashboard JSON by UID."""
    if not uid or any(ch in uid for ch in "\r\n/"):
        raise ValueError("invalid dashboard uid")
    script = f"""
PASS=$(awk -F': ' '/^Password:/ {{print $2}}' /root/homelab-grafana-admin.txt 2>/dev/null)
AUTH_FILE=$(mktemp)
trap 'rm -f "$AUTH_FILE"' EXIT
chmod 600 "$AUTH_FILE"
printf 'machine 127.0.0.1 login admin password %s\\n' "$PASS" > "$AUTH_FILE"
curl -sS --netrc-file "$AUTH_FILE" {shlex.quote('http://127.0.0.1:3001/api/dashboards/uid/' + uid)}
"""
    result = _ssh_exec(script, timeout=30, sudo=True)
    try:
        result["dashboard"] = json.loads(result["stdout"])
    except json.JSONDecodeError as exc:
        result["parse_error"] = str(exc)
    return result


@mcp.tool()
def homepage_config() -> dict[str, Any]:
    """Find and return Homepage config files from the Ubuntu server."""
    script = r"""
set -e
base=/opt/homelab-observability/homepage
if [ ! -d "$base" ]; then
  find /opt -maxdepth 4 -iname '*homepage*' -print
  exit 0
fi
find "$base" -maxdepth 3 -type f | sort | while read -r f; do
  echo "===== $f"
  sed -n '1,220p' "$f"
done
"""
    return _ssh_exec(script, timeout=30, sudo=True)


@mcp.tool()
def homepage_logs(tail: int = 100, compact_output: bool = True) -> dict[str, Any]:
    """Tail Homepage container logs."""
    tail = max(1, min(int(tail), 1000))
    return compact_command_result(
        _ssh_exec(f"docker logs --tail {tail} homepage 2>&1", timeout=30, sudo=True),
        filter_name="log",
        enabled=compact_output,
    )


@mcp.tool()
def doc_drift_snapshot() -> dict[str, Any]:
    """Return normalized documentation and live homelab inventories for drift checks."""
    import doc_drift

    return doc_drift.snapshot()


@mcp.tool()
def doc_drift_check() -> dict[str, Any]:
    """Compare canonical homelab docs with live Docker/systemd/Apache/monitoring inventory."""
    import doc_drift

    result = doc_drift.check()
    result.pop("snapshot", None)
    return result


@mcp.tool()
def doc_drift_report() -> dict[str, Any]:
    """Return a markdown homelab documentation drift report."""
    import doc_drift

    result = doc_drift.check()
    return {"report": doc_drift.render_markdown_report(result), "finding_count": result.get("finding_count", 0), "severities": result.get("severities", {})}


if __name__ == "__main__":
    mcp.run()
