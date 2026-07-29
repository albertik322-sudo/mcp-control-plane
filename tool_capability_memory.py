from __future__ import annotations

import ast
import time
from pathlib import Path
from typing import Any

import codex_memory as mem


ROOT = Path(__file__).resolve().parent
ENTRYPOINT_NAMES = {
    "memory_mcp.py",
    "workers_mcp.py",
    "server.py",
    "docker_mcp.py",
    "homelab_mcp.py",
    "mikrotik_mcp.py",
    "homeassistant_mcp.py",
    "tunnel_mcp.py",
    "windows_system_mcp.py",
    "windows_gui_mcp.py",
    "comfy_mcp.py",
    "steamdeck_mcp.py",
    "mailru_mcp.py",
}

DOMAIN_TAGS = {
    "memory_mcp": ["memory", "context-store"],
    "workers_mcp": ["workers", "agents"],
    "server": ["server", "ubuntu", "ssh"],
    "docker_mcp": ["docker", "server", "ssh"],
    "homelab_mcp": ["homelab", "observability"],
    "mikrotik_mcp": ["mikrotik", "router", "network"],
    "homeassistant_mcp": ["homeassistant", "smart-home"],
    "tunnel_mcp": ["tunnel", "ssh", "homeassistant"],
    "windows_system_mcp": ["windows", "system"],
    "windows_gui_mcp": ["windows", "gui"],
    "comfy_mcp": ["comfy", "ai", "server"],
    "steamdeck_mcp": ["steamdeck", "ssh"],
    "mailru_mcp": ["mailru", "yandex", "email", "imap", "read-only"],
}

WRITE_WORDS = {
    "add",
    "apply",
    "backup",
    "call",
    "clean",
    "create",
    "delete",
    "disable",
    "edit",
    "flush",
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

ADMIN_WORDS = {"admin", "sudo", "elevated", "defender", "route", "service_control"}
EXEC_WORDS = {"command", "exec", "powershell", "script", "shell", "ssh"}
DESTRUCTIVE_WORDS = {"delete", "remove", "kill", "stop", "disable", "prune", "revoke"}


def discover_mcp_modules(root: Path | None = None) -> list[Path]:
    base = root or ROOT
    return [base / name for name in sorted(ENTRYPOINT_NAMES) if (base / name).exists()]


def _constant_string(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _decorator_name(node: ast.AST) -> str:
    if isinstance(node, ast.Call):
        return _decorator_name(node.func)
    if isinstance(node, ast.Attribute):
        parent = _decorator_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _signature(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    args: list[str] = []
    positional = [*fn.args.posonlyargs, *fn.args.args]
    defaults = [None] * (len(positional) - len(fn.args.defaults)) + list(fn.args.defaults)
    for arg, default in zip(positional, defaults):
        item = arg.arg
        if arg.annotation is not None:
            item += f": {ast.unparse(arg.annotation)}"
        if default is not None:
            item += f" = {ast.unparse(default)}"
        args.append(item)
    if fn.args.vararg:
        args.append("*" + fn.args.vararg.arg)
    elif fn.args.kwonlyargs:
        args.append("*")
    for arg, default in zip(fn.args.kwonlyargs, fn.args.kw_defaults):
        item = arg.arg
        if arg.annotation is not None:
            item += f": {ast.unparse(arg.annotation)}"
        if default is not None:
            item += f" = {ast.unparse(default)}"
        args.append(item)
    if fn.args.kwarg:
        args.append("**" + fn.args.kwarg.arg)
    suffix = f" -> {ast.unparse(fn.returns)}" if fn.returns is not None else ""
    return f"{fn.name}({', '.join(args)}){suffix}"


def _is_mcp_tool(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return any(_decorator_name(decorator).endswith("mcp.tool") for decorator in fn.decorator_list)


def _find_server_name(tree: ast.Module) -> str | None:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        constructor = _decorator_name(node.func)
        if constructor.endswith(("FastMCP", "MCPServer", "create_mcp_server")) and node.args:
            name = _constant_string(node.args[0])
            if name:
                return name
    return None


def infer_tool_tags(module_name: str, server_name: str, tool_name: str, signature: str, docstring: str) -> list[str]:
    text = " ".join([module_name, server_name, tool_name, signature, docstring]).lower()
    tags = {"tools", "mcp", "capability", *DOMAIN_TAGS.get(module_name, [])}
    words = set(tool_name.lower().replace("-", "_").split("_"))
    if words & WRITE_WORDS or any(word in text for word in ["write", "start ", "stop ", "restart ", "delete", "apply"]):
        tags.add("write")
    else:
        tags.add("read")
    if words & ADMIN_WORDS or any(word in text for word in ["sudo", "elevated", "administrator"]):
        tags.add("admin")
    if words & EXEC_WORDS or any(word in text for word in ["command", "powershell", "shell", "ssh"]):
        tags.add("exec")
    if any(word in text for word in ["log", "logs", "journal"]):
        tags.add("logs")
    if any(word in text for word in ["health", "status", "summary", "probe"]):
        tags.add("health")
    if any(word in text for word in ["security", "defender", "audit", "firewall"]):
        tags.add("security")
    if any(word in text for word in ["file", "path", "upload", "download"]):
        tags.add("file")
    if words & DESTRUCTIVE_WORDS:
        tags.add("destructive")
    return sorted(tags)


def parse_mcp_module(path: Path) -> dict[str, Any]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    module_name = path.stem
    server_name = _find_server_name(tree) or module_name
    tools: list[dict[str, Any]] = []
    functions = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    registrations: list[tuple[ast.FunctionDef | ast.AsyncFunctionDef, str]] = [
        (node, node.name) for node in functions.values() if _is_mcp_tool(node)
    ]
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not _decorator_name(node.func).endswith("bind_app_tool"):
            continue
        if len(node.args) < 3 or not isinstance(node.args[2], ast.Name):
            continue
        fn = functions.get(node.args[2].id)
        if fn is None:
            continue
        exposed_name = fn.name
        for keyword in node.keywords:
            if keyword.arg == "name":
                exposed_name = _constant_string(keyword.value) or exposed_name
        registrations.append((fn, exposed_name))

    seen: set[str] = set()
    for node, exposed_name in registrations:
        if exposed_name in seen:
            continue
        seen.add(exposed_name)
        docstring = ast.get_docstring(node) or ""
        signature = _signature(node)
        tags = infer_tool_tags(module_name, server_name, exposed_name, signature, docstring)
        tools.append(
            {
                "name": exposed_name,
                "function": node.name,
                "signature": signature,
                "docstring": docstring,
                "line": node.lineno,
                "tags": tags,
                "risk": {
                    "write": "write" in tags,
                    "admin": "admin" in tags,
                    "exec": "exec" in tags,
                    "destructive": "destructive" in tags,
                },
            }
        )
    tools.sort(key=lambda item: item["name"])
    module_tags = sorted({tag for tool in tools for tag in tool["tags"]} | set(DOMAIN_TAGS.get(module_name, [])) | {"tools", "mcp"})
    return {
        "module": module_name,
        "path": str(path),
        "server_name": server_name,
        "tool_count": len(tools),
        "tags": module_tags,
        "tools": tools,
    }


def build_tool_inventory(root: Path | None = None) -> dict[str, Any]:
    modules = [parse_mcp_module(path) for path in discover_mcp_modules(root)]
    tool_count = sum(module["tool_count"] for module in modules)
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "module_count": len(modules),
        "tool_count": tool_count,
        "modules": modules,
    }


def collect_tool_capabilities(conn, root: Path | None = None) -> dict[str, Any]:
    inventory = build_tool_inventory(root)
    ttl = 24 * 60 * 60
    mem.upsert_document(
        conn,
        kind="snapshot",
        source="tools",
        subject="mcp-modules",
        title="MCP tool capability inventory",
        payload={
            "generated_at": inventory["generated_at"],
            "module_count": inventory["module_count"],
            "tool_count": inventory["tool_count"],
            "modules": [
                {
                    "module": module["module"],
                    "server_name": module["server_name"],
                    "path": module["path"],
                    "tool_count": module["tool_count"],
                    "tags": module["tags"],
                }
                for module in inventory["modules"]
            ],
        },
        tags=["tools", "mcp", "capability", "inventory"],
        ttl_seconds=ttl,
    )
    mem.upsert_fact(conn, namespace="tools", key="module_count", value=inventory["module_count"], source="tools", tags=["tools", "inventory"], ttl_seconds=ttl)
    mem.upsert_fact(conn, namespace="tools", key="tool_count", value=inventory["tool_count"], source="tools", tags=["tools", "inventory"], ttl_seconds=ttl)
    for module in inventory["modules"]:
        mem.upsert_document(
            conn,
            kind="snapshot",
            source="tools",
            subject=f"module:{module['module']}",
            title=f"MCP module {module['module']} capabilities",
            payload=module,
            tags=module["tags"],
            ttl_seconds=ttl,
        )
        mem.upsert_fact(
            conn,
            namespace="tool_module",
            key=module["module"],
            value={"server_name": module["server_name"], "tool_count": module["tool_count"], "tags": module["tags"]},
            source="tools",
            tags=["tools", "mcp", "module", *DOMAIN_TAGS.get(module["module"], [])],
            ttl_seconds=ttl,
        )
        for tool in module["tools"]:
            mem.upsert_document(
                conn,
                kind="snapshot",
                source="tools",
                subject=f"tool:{module['server_name']}:{tool['name']}",
                title=f"MCP tool {module['server_name']}.{tool['name']}",
                payload={**tool, "module": module["module"], "server_name": module["server_name"], "path": module["path"]},
                tags=tool["tags"],
                ttl_seconds=ttl,
            )
    return inventory
