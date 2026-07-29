from __future__ import annotations

import os
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

import psutil
from dotenv import load_dotenv
from mcp_runtime import configure_stdio, create_mcp_server, powershell_base_args


ROOT = Path(__file__).resolve().parent
configure_stdio()
load_dotenv(ROOT / ".env", encoding="utf-8-sig")
mcp = create_mcp_server("local-tunnels")

PID_DIR = ROOT / ".runtime"
PID_DIR.mkdir(exist_ok=True)


def _pid_file(name: str) -> Path:
    return PID_DIR / f"{name}.pid"


def _python() -> str:
    return str(ROOT / ".venv" / "Scripts" / "python.exe")


def _is_port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _read_pid(name: str) -> int | None:
    path = _pid_file(name)
    if not path.exists():
        return None
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
        proc = psutil.Process(pid)
        if proc.is_running():
            return pid
    except Exception:
        pass
    path.unlink(missing_ok=True)
    return None


def _start_hidden(command: list[str]) -> int:
    stdout_log = PID_DIR / "ha_tunnel.out.log"
    stderr_log = PID_DIR / "ha_tunnel.err.log"
    argument_list = " ".join(f'"{arg}"' for arg in command[1:])
    completed = subprocess.run(
        [
            *powershell_base_args(),
            "-Command",
            (
                "$p = Start-Process "
                f"-FilePath '{command[0]}' "
                f"-ArgumentList '{argument_list}' "
                f"-WorkingDirectory '{ROOT}' "
                "-WindowStyle Hidden "
                f"-RedirectStandardOutput '{stdout_log}' "
                f"-RedirectStandardError '{stderr_log}' "
                "-PassThru; "
                "Write-Output $p.Id"
            ),
        ],
        cwd=str(ROOT),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
    )
    if completed.returncode != 0 or not completed.stdout.strip():
        raise RuntimeError({"stdout": completed.stdout, "stderr": completed.stderr})
    return int(completed.stdout.strip().splitlines()[-1])


@mcp.tool()
def tunnel_status(name: str = "homeassistant") -> dict[str, Any]:
    """Return status for a local tunnel."""
    if name != "homeassistant":
        raise ValueError("only homeassistant tunnel is configured")
    local_host = os.getenv("HA_TUNNEL_LOCAL_HOST", "127.0.0.1")
    local_port = int(os.getenv("HA_TUNNEL_LOCAL_PORT", "18123"))
    pid = _read_pid(name)
    return {
        "name": name,
        "pid": pid,
        "local_host": local_host,
        "local_port": local_port,
        "port_open": _is_port_open(local_host, local_port),
    }


@mcp.tool()
def start_homeassistant_tunnel(wait_seconds: float = 3.0) -> dict[str, Any]:
    """Start a persistent SSH tunnel for Home Assistant: localhost -> Ubuntu localhost:8123."""
    name = "homeassistant"
    status = tunnel_status(name)
    if status["pid"] and status["port_open"]:
        return {"started": False, "reason": "already running", **status}

    pid = _start_hidden([_python(), str(ROOT / "ha_tunnel.py")])
    _pid_file(name).write_text(str(pid), encoding="utf-8")

    deadline = time.time() + max(0.0, wait_seconds)
    while time.time() <= deadline:
        status = tunnel_status(name)
        if status["port_open"]:
            return {"started": True, **status}
        time.sleep(0.2)
    return {"started": True, "warning": "process started, port not open yet", **tunnel_status(name)}


@mcp.tool()
def stop_tunnel(name: str = "homeassistant") -> dict[str, Any]:
    """Stop a managed local tunnel."""
    pid = _read_pid(name)
    if not pid:
        return {"stopped": False, "reason": "not running", "name": name}
    proc = psutil.Process(pid)
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except psutil.TimeoutExpired:
        proc.kill()
    _pid_file(name).unlink(missing_ok=True)
    return {"stopped": True, "name": name, "pid": pid}


if __name__ == "__main__":
    mcp.run()
