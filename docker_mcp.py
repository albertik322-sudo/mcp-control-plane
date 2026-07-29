from __future__ import annotations

import json
import os
import shlex
import time
from dataclasses import dataclass
from typing import Any

import paramiko
from dotenv import load_dotenv
from mcp_runtime import compact_command_result, configure_stdio, create_mcp_server
from ssh_auth import ssh_connect_kwargs


configure_stdio()
load_dotenv(encoding="utf-8-sig")
mcp = create_mcp_server("ubuntu-docker")


@dataclass(frozen=True)
class RemoteConfig:
    host: str
    port: int
    user: str
    password: str | None
    sudo_password: str | None
    timeout: float


def _config() -> RemoteConfig:
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


def _client() -> paramiko.SSHClient:
    cfg = _config()
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=cfg.host,
        port=cfg.port,
        username=cfg.user,
        **ssh_connect_kwargs(cfg.password),
        timeout=cfg.timeout,
        banner_timeout=cfg.timeout,
        auth_timeout=cfg.timeout,
    )
    return client


def _exec(command: str, timeout: int = 60, sudo: bool = True) -> dict[str, Any]:
    cfg = _config()
    started = time.time()
    command = f"export LANG=C.UTF-8 LC_ALL=C.UTF-8 PYTHONIOENCODING=utf-8 PYTHONUTF8=1; {command}"
    if sudo:
        if not cfg.sudo_password:
            raise RuntimeError("REMOTE_SUDO_PASSWORD or REMOTE_PASSWORD is required for sudo")
        remote_command = f"sudo -S -p '' bash -lc {shlex.quote(command)}"
    else:
        remote_command = f"bash -lc {shlex.quote(command)}"

    with _client() as client:
        stdin, stdout, stderr = client.exec_command(remote_command, get_pty=False, timeout=timeout)
        if sudo:
            stdin.write(cfg.sudo_password + "\n")
            stdin.flush()
        exit_code = stdout.channel.recv_exit_status()
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")

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


def _json_lines(command: str, timeout: int = 60) -> dict[str, Any]:
    result = _exec(command, timeout=timeout)
    rows: list[Any] = []
    errors: list[str] = []
    for line in result["stdout"].splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            errors.append(f"{exc}: {line[:200]}")
    result["rows"] = rows
    if errors:
        result["parse_errors"] = errors
    return result


def _safe_name(value: str) -> str:
    if not value or any(ch in value for ch in "\r\n"):
        raise ValueError("invalid name")
    return value


def _json_result(result: dict[str, Any], key: str = "json") -> dict[str, Any]:
    try:
        result[key] = json.loads(result["stdout"] or "null")
    except json.JSONDecodeError as exc:
        result["parse_error"] = str(exc)
    return result


@mcp.tool()
def docker_ps(all_containers: bool = True) -> dict[str, Any]:
    """List Docker containers on the Ubuntu server."""
    flag = "-a" if all_containers else ""
    fmt = "{{json .}}"
    return _json_lines(f"docker ps {flag} --no-trunc --format {shlex.quote(fmt)}")


@mcp.tool()
def docker_stats() -> dict[str, Any]:
    """Return one-shot Docker container CPU, memory, network, and block IO stats."""
    fmt = "{{json .}}"
    return _json_lines(f"docker stats --no-stream --format {shlex.quote(fmt)}", timeout=30)


@mcp.tool()
def docker_disk_usage(verbose: bool = False) -> dict[str, Any]:
    """Return Docker disk usage."""
    return _exec("docker system df " + ("-v" if verbose else ""), timeout=60)


@mcp.tool()
def docker_info() -> dict[str, Any]:
    """Return Docker daemon info as parsed JSON."""
    return _json_result(_exec("docker info --format '{{json .}}'", timeout=30))


@mcp.tool()
def docker_networks() -> dict[str, Any]:
    """List Docker networks as parsed JSON lines."""
    fmt = "{{json .}}"
    return _json_lines(f"docker network ls --no-trunc --format {shlex.quote(fmt)}", timeout=30)


@mcp.tool()
def docker_volumes() -> dict[str, Any]:
    """List Docker volumes as parsed JSON lines."""
    fmt = "{{json .}}"
    return _json_lines(f"docker volume ls --format {shlex.quote(fmt)}", timeout=30)


@mcp.tool()
def docker_images() -> dict[str, Any]:
    """List Docker images as parsed JSON lines."""
    fmt = "{{json .}}"
    return _json_lines(f"docker image ls --digests --no-trunc --format {shlex.quote(fmt)}", timeout=30)


@mcp.tool()
def docker_logs(
    container: str,
    tail: int = 200,
    since: str | None = None,
    compact_output: bool = True,
) -> dict[str, Any]:
    """Tail logs from a Docker container."""
    container = _safe_name(container)
    cmd = f"docker logs --tail {max(1, min(int(tail), 5000))}"
    if since:
        cmd += f" --since {shlex.quote(since)}"
    cmd += f" {shlex.quote(container)} 2>&1"
    return compact_command_result(
        _exec(cmd, timeout=60),
        filter_name="log",
        enabled=compact_output,
    )


@mcp.tool()
def docker_inspect(container: str) -> dict[str, Any]:
    """Inspect a Docker container or image and parse JSON output."""
    container = _safe_name(container)
    result = _exec(f"docker inspect {shlex.quote(container)}", timeout=60)
    try:
        result["json"] = json.loads(result["stdout"])
    except json.JSONDecodeError as exc:
        result["parse_error"] = str(exc)
    return result


@mcp.tool()
def docker_container_action(container: str, action: str, timeout: int = 60) -> dict[str, Any]:
    """Run a safe Docker container action: start, stop, restart, pause, unpause, or kill."""
    container = _safe_name(container)
    allowed = {"start", "stop", "restart", "pause", "unpause", "kill"}
    if action not in allowed:
        raise ValueError(f"action must be one of {sorted(allowed)}")
    return _exec(f"docker {action} {shlex.quote(container)}", timeout=max(10, int(timeout)))


@mcp.tool()
def docker_exec(
    container: str,
    command: str,
    timeout: int = 60,
    compact_output: bool = True,
) -> dict[str, Any]:
    """Execute a shell command inside a Docker container."""
    container = _safe_name(container)
    if not command.strip():
        raise ValueError("command must not be empty")
    return compact_command_result(
        _exec(f"docker exec {shlex.quote(container)} sh -lc {shlex.quote(command)}", timeout=int(timeout)),
        enabled=compact_output,
    )


@mcp.tool()
def docker_compose_projects(search_root: str = "/opt", max_depth: int = 4) -> dict[str, Any]:
    """Discover docker-compose.yml / compose.yaml projects on the Ubuntu server."""
    if not search_root.startswith("/"):
        raise ValueError("search_root must be absolute")
    depth = max(1, min(int(max_depth), 8))
    cmd = (
        f"find {shlex.quote(search_root)} -maxdepth {depth} -type f "
        "\\( -name 'docker-compose.yml' -o -name 'docker-compose.yaml' -o -name 'compose.yml' -o -name 'compose.yaml' \\) "
        "-print | sort"
    )
    result = _exec(cmd, timeout=60, sudo=True)
    result["projects"] = [
        {"file": line, "directory": os.path.dirname(line)}
        for line in result["stdout"].splitlines()
        if line.strip()
    ]
    return result


@mcp.tool()
def docker_compose(
    path: str,
    args: str = "ps",
    timeout: int = 120,
    compact_output: bool = True,
) -> dict[str, Any]:
    """Run docker compose/docker-compose in a directory. Use for ps, logs, pull, up -d, restart, etc."""
    if not path.startswith("/"):
        raise ValueError("path must be absolute on the Ubuntu server")
    if any(ch in args for ch in "\r\n"):
        raise ValueError("args must be a single command line")
    command = (
        f"cd {shlex.quote(path)} && "
        f"if docker compose version >/dev/null 2>&1; then docker compose {args}; "
        f"else docker-compose {args}; fi"
    )
    filter_name = "log" if args.strip().startswith("logs") else None
    return compact_command_result(
        _exec(command, timeout=int(timeout)),
        filter_name=filter_name,
        enabled=compact_output,
    )


@mcp.tool()
def docker_health_overview() -> dict[str, Any]:
    """Return a compact overview of Docker runtime, containers, stats, and disk usage."""
    return {
        "version": _json_result(_exec("docker version --format '{{json .}}'", timeout=30)),
        "info": docker_info(),
        "containers": docker_ps(all_containers=True),
        "stats": docker_stats(),
        "disk_usage": docker_disk_usage(verbose=False),
    }


@mcp.tool()
def docker_unhealthy() -> dict[str, Any]:
    """Return containers that are exited, restarting, unhealthy, or missing health checks."""
    rows = docker_ps(all_containers=True).get("rows", [])
    problem_rows = []
    for row in rows:
        state = str(row.get("State", "")).lower()
        status = str(row.get("Status", "")).lower()
        if state != "running" or "unhealthy" in status or "restarting" in status:
            problem_rows.append(row)
    return {"containers": problem_rows, "total": len(problem_rows)}


@mcp.tool()
def docker_security_audit() -> dict[str, Any]:
    """Inspect containers for risky runtime settings without changing anything."""
    containers = docker_ps(all_containers=True).get("rows", [])
    findings: list[dict[str, Any]] = []
    for row in containers:
        name = row.get("Names") or row.get("Names", "")
        if not name:
            continue
        inspected = docker_inspect(str(name)).get("json") or []
        if not inspected:
            continue
        cfg = inspected[0]
        host_config = cfg.get("HostConfig", {})
        network_mode = str(host_config.get("NetworkMode", ""))
        binds = host_config.get("Binds") or []
        caps = host_config.get("CapAdd") or []
        issues = []
        if host_config.get("Privileged"):
            issues.append("privileged container")
        if network_mode == "host":
            issues.append("host network")
        if any(str(bind).startswith("/:") or str(bind).startswith("/:/") for bind in binds):
            issues.append("root filesystem bind mount")
        if caps:
            issues.append("additional capabilities: " + ", ".join(map(str, caps)))
        if issues:
            findings.append({"container": name, "issues": issues, "image": cfg.get("Config", {}).get("Image")})
    return {"findings": findings, "total": len(findings)}


@mcp.tool()
def docker_prune_preview() -> dict[str, Any]:
    """Show reclaimable Docker disk usage without pruning."""
    return docker_disk_usage(verbose=True)


if __name__ == "__main__":
    mcp.run()
