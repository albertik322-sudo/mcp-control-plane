from __future__ import annotations

import base64
import json
import os
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

import psutil
from mcp_runtime import compact_command_result, configure_stdio, create_mcp_server, powershell_base_args, powershell_executable
import uuid
import windows_performance
import windows_admin_policy


configure_stdio()
mcp = create_mcp_server("windows-system")


def _powershell_executable() -> str:
    """Prefer PowerShell 7 when available, fall back to Windows PowerShell."""
    return powershell_executable()


def _powershell_base_args() -> list[str]:
    return powershell_base_args()


def _run_powershell(command: str, timeout: int = 30) -> dict[str, Any]:
    started = time.time()
    exe = _powershell_executable()
    completed = subprocess.run(
        [*_powershell_base_args(), "-Command", command],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    return {
        "runtime": exe,
        "command": command,
        "exit_code": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "duration_seconds": round(time.time() - started, 3),
    }


def _run_powershell_file(script: str, timeout: int = 60) -> dict[str, Any]:
    """Run a PowerShell script from a temporary UTF-8 .ps1 file.

    This avoids nested heredoc quoting and preserves Cyrillic, emoji, backslashes,
    and literal newlines as long as the MCP client sends valid UTF-8 JSON.
    """
    started = time.time()
    temp_path: Path | None = None
    try:
        fd, name = tempfile.mkstemp(prefix="codex-ps-", suffix=".ps1")
        os.close(fd)
        temp_path = Path(name)
        temp_path.write_text(script, encoding="utf-8-sig", newline="\n")
        exe = _powershell_executable()
        completed = subprocess.run(
            [*_powershell_base_args(), "-File", str(temp_path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        return {
            "runtime": exe,
            "command": f"{exe} -File {temp_path}",
            "script_path": str(temp_path),
            "exit_code": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "duration_seconds": round(time.time() - started, 3),
        }
    finally:
        if temp_path:
            try:
                temp_path.unlink(missing_ok=True)
            except Exception:
                pass


def _run_powershell_file_utf8_capture(script: str, timeout: int = 60) -> dict[str, Any]:
    """Run a UTF-8 .ps1 and capture combined output through a UTF-8 file.

    Windows PowerShell 5.1 can still replace emoji/non-codepage characters with
    `?` when stdout is captured through the process pipe. Capturing inside
    PowerShell with Out-File -Encoding utf8 preserves .NET strings better.
    """
    started = time.time()
    paths: list[Path] = []
    try:
        fd, script_name = tempfile.mkstemp(prefix="codex-ps-script-", suffix=".ps1")
        os.close(fd)
        script_path = Path(script_name)
        paths.append(script_path)
        script_path.write_text(script, encoding="utf-8-sig", newline="\n")

        fd, wrapper_name = tempfile.mkstemp(prefix="codex-ps-wrapper-", suffix=".ps1")
        os.close(fd)
        wrapper_path = Path(wrapper_name)
        paths.append(wrapper_path)

        fd, output_name = tempfile.mkstemp(prefix="codex-ps-output-", suffix=".txt")
        os.close(fd)
        output_path = Path(output_name)
        output_path.unlink(missing_ok=True)
        paths.append(output_path)

        fd, code_name = tempfile.mkstemp(prefix="codex-ps-exit-", suffix=".txt")
        os.close(fd)
        code_path = Path(code_name)
        code_path.unlink(missing_ok=True)
        paths.append(code_path)

        wrapper = f"""
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$scriptPath = {str(script_path)!r}
$outputPath = {str(output_path)!r}
$codePath = {str(code_path)!r}
try {{
    & $scriptPath *>&1 | Out-File -FilePath $outputPath -Encoding utf8
    $code = $LASTEXITCODE
    if ($null -eq $code) {{ $code = 0 }}
}} catch {{
    $_ | Out-File -FilePath $outputPath -Encoding utf8 -Append
    $code = 1
}}
[System.IO.File]::WriteAllText($codePath, [string]$code, [System.Text.UTF8Encoding]::new($false))
exit $code
"""
        wrapper_path.write_text(wrapper, encoding="utf-8-sig", newline="\n")
        exe = _powershell_executable()
        completed = subprocess.run(
            [*_powershell_base_args(), "-File", str(wrapper_path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        stdout = output_path.read_text(encoding="utf-8-sig", errors="replace") if output_path.exists() else completed.stdout
        try:
            exit_code = int(code_path.read_text(encoding="utf-8-sig", errors="replace").strip())
        except Exception:
            exit_code = completed.returncode
        return {
            "runtime": exe,
            "command": f"{exe} -File {wrapper_path}",
            "script_path": str(script_path),
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": completed.stderr,
            "duration_seconds": round(time.time() - started, 3),
        }
    finally:
        for path in paths:
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass


def _run_powershell_encoded(script: str, timeout: int = 60) -> dict[str, Any]:
    """Run PowerShell via -EncodedCommand using UTF-16LE, PowerShell's native format."""
    started = time.time()
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    exe = _powershell_executable()
    completed = subprocess.run(
        [*_powershell_base_args(), "-EncodedCommand", encoded],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    return {
        "runtime": exe,
        "command": f"{exe} -EncodedCommand <base64-utf16le>",
        "exit_code": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "duration_seconds": round(time.time() - started, 3),
    }


def _parse_json_stdout(result: dict[str, Any], key: str) -> dict[str, Any]:
    try:
        payload = json.loads(result["stdout"] or "null")
        result[key] = payload
    except json.JSONDecodeError as exc:
        result["parse_error"] = str(exc)
    return result


@mcp.tool()
def system_summary() -> dict[str, Any]:
    """Return CPU, memory, disk, boot time, and process count summary for this Windows laptop."""
    vm = psutil.virtual_memory()
    sm = psutil.swap_memory()
    return {
        "hostname": socket.gethostname(),
        "boot_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(psutil.boot_time())),
        "cpu_percent": psutil.cpu_percent(interval=1),
        "cpu_count_logical": psutil.cpu_count(logical=True),
        "memory": {
            "total_gb": round(vm.total / 1024**3, 2),
            "available_gb": round(vm.available / 1024**3, 2),
            "used_gb": round(vm.used / 1024**3, 2),
            "percent": vm.percent,
        },
        "swap": {
            "total_gb": round(sm.total / 1024**3, 2),
            "used_gb": round(sm.used / 1024**3, 2),
            "percent": sm.percent,
        },
        "process_count": len(psutil.pids()),
    }


@mcp.tool()
def top_processes(limit: int = 25, sort_by: str = "memory") -> dict[str, Any]:
    """Return top processes by memory, cpu, handles, or threads."""
    allowed = {"memory", "cpu", "threads"}
    if sort_by not in allowed:
        raise ValueError(f"sort_by must be one of {sorted(allowed)}")
    rows: list[dict[str, Any]] = []
    for proc in psutil.process_iter(["pid", "name", "username", "memory_info", "cpu_percent", "num_threads", "cmdline"]):
        try:
            info = proc.info
            mem = info.get("memory_info")
            rows.append(
                {
                    "pid": info["pid"],
                    "name": info.get("name"),
                    "username": info.get("username"),
                    "rss_mb": round((mem.rss if mem else 0) / 1024**2, 1),
                    "cpu_percent": info.get("cpu_percent") or 0,
                    "threads": info.get("num_threads") or 0,
                    "cmdline": " ".join(info.get("cmdline") or [])[:500],
                }
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    key = {"memory": "rss_mb", "cpu": "cpu_percent", "threads": "threads"}[sort_by]
    return {"processes": sorted(rows, key=lambda row: row[key], reverse=True)[: max(1, min(int(limit), 200))]}


@mcp.tool()
def process_groups(limit: int = 30) -> dict[str, Any]:
    """Group processes by executable name and sum memory usage."""
    groups: dict[str, dict[str, Any]] = {}
    for proc in psutil.process_iter(["name", "memory_info"]):
        try:
            name = proc.info.get("name") or "unknown"
            mem = proc.info.get("memory_info")
            row = groups.setdefault(name, {"name": name, "count": 0, "rss_mb": 0.0})
            row["count"] += 1
            row["rss_mb"] += (mem.rss if mem else 0) / 1024**2
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    rows = list(groups.values())
    for row in rows:
        row["rss_mb"] = round(row["rss_mb"], 1)
    return {"groups": sorted(rows, key=lambda row: row["rss_mb"], reverse=True)[: max(1, min(int(limit), 200))]}


@mcp.tool()
def startup_items() -> dict[str, Any]:
    """Return Windows startup commands."""
    command = (
        "Get-CimInstance Win32_StartupCommand | "
        "Select-Object Name,Command,Location,User | ConvertTo-Json -Depth 4"
    )
    result = _run_powershell(command, timeout=30)
    try:
        payload = json.loads(result["stdout"] or "[]")
        if isinstance(payload, dict):
            payload = [payload]
        result["items"] = payload
    except json.JSONDecodeError as exc:
        result["parse_error"] = str(exc)
    return result


@mcp.tool()
def services(query: str | None = None) -> dict[str, Any]:
    """List Windows services, optionally filtered by name/display name/status substring."""
    where = ""
    if query:
        q = query.replace("'", "''")
        where = f" | Where-Object {{ $_.Name -like '*{q}*' -or $_.DisplayName -like '*{q}*' -or $_.Status -like '*{q}*' }}"
    command = (
        "Get-Service"
        + where
        + " | Select-Object Name,DisplayName,Status,StartType | Sort-Object Name | ConvertTo-Json -Depth 4"
    )
    result = _run_powershell(command, timeout=30)
    try:
        payload = json.loads(result["stdout"] or "[]")
        result["services"] = [payload] if isinstance(payload, dict) else payload
    except json.JSONDecodeError as exc:
        result["parse_error"] = str(exc)
    return result


@mcp.tool()
def network_summary() -> dict[str, Any]:
    """Return network adapters, IP addresses, default routes, and DNS servers."""
    command = r"""
$adapters = Get-NetAdapter | Select-Object Name,InterfaceDescription,Status,LinkSpeed,MacAddress,ifIndex
$ip = Get-NetIPAddress | Where-Object {$_.AddressFamily -eq 'IPv4'} | Select-Object InterfaceAlias,IPAddress,PrefixLength
$dns = Get-DnsClientServerAddress -AddressFamily IPv4 | Select-Object InterfaceAlias,ServerAddresses
$routes = Get-NetRoute -AddressFamily IPv4 -DestinationPrefix '0.0.0.0/0' | Select-Object InterfaceAlias,NextHop,RouteMetric,InterfaceMetric
[pscustomobject]@{adapters=$adapters; ip=$ip; dns=$dns; default_routes=$routes} | ConvertTo-Json -Depth 5
"""
    result = _run_powershell(command, timeout=30)
    try:
        result["network"] = json.loads(result["stdout"])
    except json.JSONDecodeError as exc:
        result["parse_error"] = str(exc)
    return result


@mcp.tool()
def dns_test(hostname: str = "google.com", server: str | None = None, count: int = 5) -> dict[str, Any]:
    """Measure DNS resolution timing from Windows."""
    count = max(1, min(int(count), 50))
    rows: list[dict[str, Any]] = []
    for _ in range(count):
        started = time.time()
        try:
            if server:
                command = f"Resolve-DnsName {hostname!r} -Server {server!r} -ErrorAction Stop | Select-Object -First 5 | ConvertTo-Json -Depth 4"
                result = _run_powershell(command, timeout=15)
                ok = result["exit_code"] == 0
                error = result["stderr"] if not ok else None
            else:
                socket.getaddrinfo(hostname, None)
                ok = True
                error = None
            rows.append({"ok": ok, "duration_ms": round((time.time() - started) * 1000, 1), "error": error})
        except Exception as exc:
            rows.append({"ok": False, "duration_ms": round((time.time() - started) * 1000, 1), "error": str(exc)})
    return {
        "hostname": hostname,
        "server": server,
        "count": count,
        "results": rows,
        "avg_ms": round(sum(row["duration_ms"] for row in rows) / len(rows), 1),
    }


@mcp.tool()
def wifi_status() -> dict[str, Any]:
    """Return netsh Wi-Fi interface and visible network information."""
    return {
        "interfaces": _run_powershell("netsh wlan show interfaces", timeout=20),
        "networks": _run_powershell("netsh wlan show networks mode=bssid", timeout=30),
    }


@mcp.tool()
def battery_status() -> dict[str, Any]:
    """Return battery charge, health hints, and recent power state."""
    command = r"""
$battery = Get-CimInstance Win32_Battery | Select-Object Name,Status,BatteryStatus,EstimatedChargeRemaining,EstimatedRunTime,DesignVoltage
$power = Get-CimInstance Win32_PowerManagementEvent -ErrorAction SilentlyContinue | Select-Object -First 5
[pscustomobject]@{battery=$battery; power_events=$power} | ConvertTo-Json -Depth 5
"""
    return _parse_json_stdout(_run_powershell(command, timeout=30), "battery")


@mcp.tool()
def disk_health() -> dict[str, Any]:
    """Return physical disk health, logical volume space, and Storage reliability counters when available."""
    command = r"""
$physical = Get-PhysicalDisk -ErrorAction SilentlyContinue | Select-Object FriendlyName,SerialNumber,MediaType,HealthStatus,OperationalStatus,Size
$volumes = Get-Volume -ErrorAction SilentlyContinue | Select-Object DriveLetter,FileSystemLabel,FileSystem,HealthStatus,OperationalStatus,Size,SizeRemaining
$reliability = Get-StorageReliabilityCounter -PhysicalDisk (Get-PhysicalDisk -ErrorAction SilentlyContinue) -ErrorAction SilentlyContinue |
  Select-Object DeviceId,Temperature,TemperatureMax,ReadErrorsTotal,WriteErrorsTotal,Wear
[pscustomobject]@{physical_disks=$physical; volumes=$volumes; reliability=$reliability} | ConvertTo-Json -Depth 6
"""
    return _parse_json_stdout(_run_powershell(command, timeout=45), "storage")


@mcp.tool()
def defender_status() -> dict[str, Any]:
    """Return Microsoft Defender status and recent threat detections."""
    command = r"""
$status = Get-MpComputerStatus -ErrorAction SilentlyContinue
$threats = Get-MpThreatDetection -ErrorAction SilentlyContinue | Sort-Object InitialDetectionTime -Descending | Select-Object -First 20
[pscustomobject]@{status=$status; recent_threats=$threats} | ConvertTo-Json -Depth 8
"""
    return _parse_json_stdout(_run_powershell(command, timeout=45), "defender")


@mcp.tool()
def windows_update_summary() -> dict[str, Any]:
    """Return Windows Update service state and recent update history."""
    command = r"""
$services = Get-Service wuauserv,bits,cryptsvc -ErrorAction SilentlyContinue | Select-Object Name,DisplayName,Status,StartType
$hotfixes = Get-HotFix | Sort-Object InstalledOn -Descending | Select-Object -First 25 HotFixID,Description,InstalledBy,InstalledOn
$rebootPending = Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending'
[pscustomobject]@{services=$services; recent_hotfixes=$hotfixes; reboot_pending=$rebootPending} | ConvertTo-Json -Depth 5
"""
    return _parse_json_stdout(_run_powershell(command, timeout=45), "windows_update")


@mcp.tool()
def event_log(log_name: str = "System", level: str = "Error", hours: int = 24, limit: int = 80) -> dict[str, Any]:
    """Read recent Windows event log entries by log and level."""
    allowed_logs = {"System", "Application", "Security", "Microsoft-Windows-Windows Defender/Operational"}
    allowed_levels = {"Critical": 1, "Error": 2, "Warning": 3, "Information": 4}
    if log_name not in allowed_logs:
        raise ValueError(f"log_name must be one of {sorted(allowed_logs)}")
    if level not in allowed_levels:
        raise ValueError(f"level must be one of {sorted(allowed_levels)}")
    hours = max(1, min(int(hours), 24 * 30))
    limit = max(1, min(int(limit), 500))
    command = f"""
$start = (Get-Date).AddHours(-{hours})
Get-WinEvent -FilterHashtable @{{LogName={log_name!r}; Level={allowed_levels[level]}; StartTime=$start}} -ErrorAction SilentlyContinue |
  Select-Object -First {limit} TimeCreated,ProviderName,Id,LevelDisplayName,Message |
  ConvertTo-Json -Depth 5
"""
    result = _run_powershell(command, timeout=60)
    parsed = _parse_json_stdout(result, "events")
    if isinstance(parsed.get("events"), dict):
        parsed["events"] = [parsed["events"]]
    return parsed


@mcp.tool()
def laptop_health() -> dict[str, Any]:
    """Return a practical laptop health bundle for quick triage."""
    return {
        "system": system_summary(),
        "battery": battery_status(),
        "storage": disk_health(),
        "network": network_summary(),
        "defender": defender_status(),
        "windows_update": windows_update_summary(),
        "system_errors_24h": event_log(log_name="System", level="Error", hours=24, limit=50),
    }


@mcp.tool()
def performance_snapshot(
    sample_seconds: float = 1.0,
    process_limit: int = 80,
    latency_count: int = 2,
    latency_timeout: float = 2.0,
) -> dict[str, Any]:
    """Return a read-only Windows performance snapshot with safe recommendations."""
    return windows_performance.performance_snapshot(
        sample_seconds=sample_seconds,
        process_limit=process_limit,
        latency_count=latency_count,
        latency_timeout=latency_timeout,
    )


@mcp.tool()
def process_impact_snapshot(sample_seconds: float = 1.0, limit: int = 80) -> dict[str, Any]:
    """Sample processes and group likely performance impact without changing system state."""
    counters = windows_performance.collect_counters(sample_seconds)
    processes = windows_performance.collect_process_samples(sample_seconds=sample_seconds, limit=limit)
    total_memory_mb = float((counters.get("memory") or {}).get("total_gb") or 0) * 1024
    groups = windows_performance.process_group_impact(processes, total_memory_mb, limit=25)
    return {
        "read_only": True,
        "sample_seconds": sample_seconds,
        "counters": counters,
        "processes": processes,
        "process_groups": groups,
        "recommendations": windows_performance.safe_recommendations({"counters": counters, "process_groups": groups}),
    }


@mcp.tool()
def service_startup_impact_snapshot() -> dict[str, Any]:
    """Return read-only startup item and running service impact hints."""
    startup = startup_items()
    service_rows = services("Running")
    startup_rows = startup.get("items") or []
    running_services = service_rows.get("services") or []
    return {
        "read_only": True,
        "startup": windows_performance.startup_impact(startup_rows),
        "running_services": windows_performance.service_impact(running_services),
        "raw_exit_codes": {
            "startup_items": startup.get("exit_code"),
            "services": service_rows.get("exit_code"),
        },
        "parse_errors": {
            "startup_items": startup.get("parse_error"),
            "services": service_rows.get("parse_error"),
        },
    }


@mcp.tool()
def homelab_latency_snapshot(count: int = 3, timeout: float = 2.0) -> dict[str, Any]:
    """Measure read-only TCP connect latency from Windows to practical homelab targets."""
    return {"read_only": True, "latency": windows_performance.homelab_latency(count=count, timeout=timeout)}


@mcp.tool()
def powershell_runtime() -> dict[str, Any]:
    """Return the selected PowerShell runtime and version details."""
    exe = _powershell_executable()
    result = _run_powershell(
        "$PSVersionTable | Select-Object PSVersion,PSEdition,GitCommitId,OS,Platform | ConvertTo-Json -Depth 4",
        timeout=15,
    )
    try:
        details = json.loads(result["stdout"] or "{}")
    except json.JSONDecodeError as exc:
        details = {"parse_error": str(exc), "raw": result["stdout"]}
    return {
        "runtime": exe,
        "details": details,
        "exit_code": result["exit_code"],
        "stderr": result["stderr"],
    }


@mcp.tool()
def powershell(command: str, timeout: int = 30, compact_output: bool = True) -> dict[str, Any]:
    """Run a local PowerShell command. Keep commands focused and non-interactive."""
    if not command.strip():
        raise ValueError("command must not be empty")
    return compact_command_result(
        _run_powershell(command, timeout=max(1, min(int(timeout), 300))),
        enabled=compact_output,
    )


@mcp.tool()
def powershell_script(script: str, timeout: int = 60, compact_output: bool = True) -> dict[str, Any]:
    """Run a local multi-line PowerShell script from a temporary UTF-8 .ps1 file.

    Prefer this over textual heredocs for scripts containing Russian text, emoji,
    quotes, regexes, or literal newlines.
    """
    if not script.strip():
        raise ValueError("script must not be empty")
    return compact_command_result(
        _run_powershell_file(script, timeout=max(1, min(int(timeout), 900))),
        enabled=compact_output,
    )


@mcp.tool()
def powershell_script_utf8(script: str, timeout: int = 60, compact_output: bool = True) -> dict[str, Any]:
    """Run a local multi-line PowerShell script and capture output as UTF-8.

    Use this for scripts whose output may contain Russian text or emoji. It is
    slower than `powershell_script`, but avoids Windows PowerShell 5.1 pipe
    encoding replacing characters with `?`.
    """
    if not script.strip():
        raise ValueError("script must not be empty")
    return compact_command_result(
        _run_powershell_file_utf8_capture(script, timeout=max(1, min(int(timeout), 900))),
        enabled=compact_output,
    )


@mcp.tool()
def powershell_encoded(script: str, timeout: int = 60, compact_output: bool = True) -> dict[str, Any]:
    """Run local PowerShell using -EncodedCommand with UTF-16LE base64.

    This is useful when a script should not touch the filesystem, while still
    avoiding command-line quoting issues.
    """
    if not script.strip():
        raise ValueError("script must not be empty")
    return compact_command_result(
        _run_powershell_encoded(script, timeout=max(1, min(int(timeout), 900))),
        enabled=compact_output,
    )


@mcp.tool()
def write_text_file(path: str, content: str, encoding: str = "utf-8-sig", overwrite: bool = True) -> dict[str, Any]:
    """Write a local Windows text file with explicit encoding.

    Use this for UTF-8 JSON/localization files instead of PowerShell redirection.
    """
    target = Path(path).expanduser()
    if target.exists() and not overwrite:
        raise FileExistsError(str(target))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding=encoding, newline="\n")
    return {"path": str(target), "bytes": target.stat().st_size, "encoding": encoding}


@mcp.tool()
def read_text_file(path: str, encoding: str = "utf-8-sig", max_chars: int = 200000) -> dict[str, Any]:
    """Read a local Windows text file with explicit encoding and size limit."""
    target = Path(path).expanduser()
    text = target.read_text(encoding=encoding, errors="replace")
    return {"path": str(target), "content": text[: max(1, int(max_chars))], "truncated": len(text) > max_chars}


@mcp.tool()
def admin_capabilities() -> dict[str, Any]:
    """Return available allowlisted elevated Windows admin actions."""
    return windows_admin_policy.capabilities()


@mcp.tool()
def admin_plan(action: str, args: dict[str, Any] | None = None, timeout: int | None = None) -> dict[str, Any]:
    """Render an allowlisted elevated admin action without executing it."""
    rendered = windows_admin_policy.render_action(action, args or {}, timeout)
    return {**rendered.as_dict(), "execute_with": windows_admin_policy.TASK_NAME}


@mcp.tool()
def admin_result(request_id: str) -> dict[str, Any]:
    """Return a stored elevated admin action result by request id."""
    path = windows_admin_policy.result_path(request_id)
    if not path.exists():
        return {"found": False, "request_id": request_id}
    return {"found": True, "request_id": request_id, "result": json.loads(path.read_text(encoding="utf-8"))}


@mcp.tool()
def admin_run(action: str, args: dict[str, Any] | None = None, timeout: int | None = None, wait_seconds: int = 60) -> dict[str, Any]:
    """Run an allowlisted elevated Windows admin action through the scheduled-task broker.

    The broker must be installed once with install_windows_admin_broker_task.ps1.
    Existing raw PowerShell tools stay non-elevated by design.
    """
    rendered = windows_admin_policy.render_action(action, args or {}, timeout)
    windows_admin_policy.ensure_dirs()
    request_id = uuid.uuid4().hex
    request = {
        "request_id": request_id,
        "action": action,
        "args": args or {},
        "timeout": rendered.timeout,
        "created_at": time.time(),
    }
    windows_admin_policy.request_path(request_id).write_text(
        json.dumps(request, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    start = _run_powershell(f"schtasks /run /tn {windows_admin_policy.ps_quote(windows_admin_policy.TASK_NAME)}", timeout=20)
    if start["exit_code"] != 0:
        return {
            "ok": False,
            "request_id": request_id,
            "stage": "start_scheduled_task",
            "message": "Install the elevated broker first with install_windows_admin_broker_task.ps1 from an elevated PowerShell.",
            "start_result": start,
            "plan": rendered.as_dict(),
        }
    deadline = time.time() + max(1, min(int(wait_seconds), 300))
    while time.time() < deadline:
        path = windows_admin_policy.result_path(request_id)
        if path.exists():
            return {"ok": True, "request_id": request_id, "result": json.loads(path.read_text(encoding="utf-8"))}
        time.sleep(0.5)
    return {"ok": False, "request_id": request_id, "stage": "wait_result", "plan": rendered.as_dict()}


@mcp.tool()
def admin_audit_log(limit: int = 50) -> dict[str, Any]:
    """Read recent elevated admin broker audit events."""
    path = windows_admin_policy.AUDIT_LOG
    if not path.exists():
        return {"events": [], "path": str(path)}
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[-max(1, min(int(limit), 500)) :]:
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            rows.append({"raw": line})
    return {"events": rows, "path": str(path)}


if __name__ == "__main__":
    mcp.run()
