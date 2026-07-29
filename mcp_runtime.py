from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


DEFAULT_COMPACT_THRESHOLD = 8_000
DEFAULT_MAX_OUTPUT_CHARS = 24_000


def configure_stdio() -> None:
    """Make local MCP stdio/subprocess text handling prefer UTF-8 on Windows."""
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    os.environ.setdefault("PYTHONUTF8", "1")
    for stream_name in ("stdin", "stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def create_mcp_server(name: str, **kwargs: Any):
    """Create an MCPServer with safe, opt-in OpenTelemetry enrichment."""
    from mcp.server import MCPServer

    from mcp_observability import SafeMCPAttributesMiddleware, configure_otel

    configure_otel(name)
    middleware = [SafeMCPAttributesMiddleware(name), *list(kwargs.pop("middleware", ()))]
    return MCPServer(name, middleware=middleware, **kwargs)


def powershell_executable() -> str:
    """Return PowerShell 7 when available, otherwise Windows PowerShell."""
    env_override = os.getenv("POWERSHELL_EXE", "").strip()
    candidates = [
        env_override,
        shutil.which("pwsh"),
        r"C:\Program Files\PowerShell\7\pwsh.exe",
        shutil.which("powershell"),
        "powershell",
    ]
    for candidate in candidates:
        if not candidate:
            continue
        if "\\" in candidate or "/" in candidate:
            if Path(candidate).exists():
                return candidate
        else:
            return candidate
    return "powershell"


def powershell_base_args() -> list[str]:
    return [powershell_executable(), "-NoProfile", "-ExecutionPolicy", "Bypass"]


def run_powershell(command: str, timeout: int = 60, cwd: str | None = None) -> dict[str, Any]:
    """Run PowerShell with UTF-8 capture and the selected PS runtime."""
    completed = subprocess.run(
        [*powershell_base_args(), "-Command", command],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=max(1, int(timeout)),
        env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
    )
    return {
        "runtime": powershell_executable(),
        "command": command,
        "exit_code": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def rtk_executable() -> str | None:
    """Return the local RTK executable when available."""
    override = os.getenv("RTK_EXE", "").strip()
    candidates = [
        override,
        shutil.which("rtk"),
        str(Path.home() / ".local" / "bin" / "rtk.exe"),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        if "\\" in candidate or "/" in candidate:
            if Path(candidate).exists():
                return candidate
        else:
            return candidate
    return None


def _positive_env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def _head_tail(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    marker = f"\n...[MCP output truncated: {len(text) - max_chars} chars omitted]...\n"
    available = max(2, max_chars - len(marker))
    head_chars = max(1, int(available * 0.7))
    tail_chars = max(1, available - head_chars)
    return text[:head_chars] + marker + text[-tail_chars:]


def _rtk_pipe(text: str, filter_name: str, timeout: int = 15) -> str | None:
    executable = rtk_executable()
    if not executable:
        return None
    try:
        completed = subprocess.run(
            [executable, "pipe", "--filter", filter_name],
            input=text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(1, int(timeout)),
            env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    candidate = completed.stdout.strip()
    if completed.returncode != 0 or not candidate:
        return None
    return candidate + "\n"


def compact_text_output(
    text: str,
    *,
    filter_name: str | None = None,
    enabled: bool = True,
    threshold: int | None = None,
    max_chars: int | None = None,
) -> tuple[str, dict[str, Any] | None]:
    """Compact a large text field with RTK, then enforce a bounded fallback."""
    if not enabled or not text:
        return text, None

    threshold = threshold or _positive_env_int("MCP_RTK_THRESHOLD_CHARS", DEFAULT_COMPACT_THRESHOLD)
    max_chars = max_chars or _positive_env_int("MCP_MAX_OUTPUT_CHARS", DEFAULT_MAX_OUTPUT_CHARS)
    if len(text) <= threshold and len(text) <= max_chars:
        return text, None

    original_chars = len(text)
    result = text
    methods: list[str] = []
    rtk_enabled = os.getenv("MCP_RTK_COMPACT", "1").strip().lower() not in {"0", "false", "no", "off"}
    if rtk_enabled and filter_name:
        candidate = _rtk_pipe(text, filter_name)
        if candidate is not None and len(candidate) < len(result):
            result = candidate
            methods.append(f"rtk:{filter_name}")

    if len(result) > max_chars:
        result = _head_tail(result, max_chars)
        methods.append("head-tail-limit")

    if not methods:
        return text, None
    returned_chars = len(result)
    return result, {
        "methods": methods,
        "original_chars": original_chars,
        "returned_chars": returned_chars,
        "saved_percent": round((1 - returned_chars / original_chars) * 100, 1),
    }


def compact_command_result(
    result: dict[str, Any],
    *,
    filter_name: str | None = None,
    enabled: bool = True,
    threshold: int | None = None,
    max_chars: int | None = None,
) -> dict[str, Any]:
    """Compact stdout while preserving the command result schema and exact stderr."""
    output = dict(result)
    compacted, metadata = compact_text_output(
        str(output.get("stdout") or ""),
        filter_name=filter_name,
        enabled=enabled,
        threshold=threshold,
        max_chars=max_chars,
    )
    output["stdout"] = compacted
    if metadata:
        output["output_compaction"] = {
            **metadata,
            "full_output_hint": "Rerun this MCP tool with compact_output=false for exact stdout.",
        }
    return output


configure_stdio()
