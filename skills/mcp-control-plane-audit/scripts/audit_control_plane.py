from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from pathlib import Path
from typing import Any


SENSITIVE_KEY = re.compile(
    r"(api[_-]?key|token|secret|password|credential|private[_-]?key)",
    re.IGNORECASE,
)
SHELL_COMMANDS = {
    "bash",
    "bash.exe",
    "cmd",
    "cmd.exe",
    "powershell",
    "powershell.exe",
    "pwsh",
    "pwsh.exe",
    "sh",
}
SHELL_FLAGS = {"-c", "-command", "/c"}
CONFORMANCE_VERSION = "0.2.0-alpha.10"
OTEL_VERSION = "1.44.0"


def find_project_root(start: Path) -> Path:
    for candidate in (start, *start.parents):
        if (candidate / "AGENTS.md").exists() and (candidate / "tool_capability_memory.py").exists():
            return candidate
    raise FileNotFoundError("Could not find the MCP project root.")


def finding(severity: str, check: str, message: str, path: Path) -> dict[str, str]:
    return {
        "severity": severity,
        "check": check,
        "message": message,
        "path": str(path),
    }


def audit_codex_config(path: Path) -> tuple[dict[str, Any], list[dict[str, str]]]:
    if not path.exists():
        return {"path": str(path), "server_count": 0}, [
            finding("MEDIUM", "config-missing", "Codex config.toml was not found.", path)
        ]

    with path.open("rb") as handle:
        servers = tomllib.load(handle).get("mcp_servers", {})

    results: list[dict[str, str]] = []
    summaries: list[dict[str, Any]] = []
    for name, server in sorted(servers.items()):
        if not isinstance(server, dict):
            results.append(finding("HIGH", "invalid-server-config", f"{name}: configuration is not a table.", path))
            continue

        command = str(server.get("command", ""))
        command_name = Path(command).name.lower()
        args = [str(value) for value in server.get("args", [])]
        env = server.get("env", {})
        env_keys = sorted(str(key) for key in env) if isinstance(env, dict) else []
        summaries.append(
            {
                "name": name,
                "command": Path(command).name,
                "arg_count": len(args),
                "env_keys": env_keys,
            }
        )

        if not command:
            results.append(finding("HIGH", "missing-command", f"{name}: command is empty.", path))
        if command_name in SHELL_COMMANDS and any(arg.lower() in SHELL_FLAGS for arg in args):
            results.append(finding("HIGH", "shell-wrapper", f"{name}: server starts through a command shell.", path))
        if any("@latest" in arg.lower() for arg in args):
            results.append(finding("MEDIUM", "unpinned-package", f"{name}: an argument uses @latest.", path))

        if isinstance(env, dict):
            for key, value in env.items():
                if not SENSITIVE_KEY.search(str(key)):
                    continue
                if isinstance(value, str) and value and not re.fullmatch(r"\$\{[A-Z0-9_]+\}", value):
                    results.append(
                        finding(
                            "HIGH",
                            "inline-sensitive-env",
                            f"{name}: sensitive key {key} has an inline value; value suppressed.",
                            path,
                        )
                    )

    return {
        "path": str(path),
        "server_count": len(servers),
        "servers": summaries,
    }, results


def audit_sources(root: Path) -> tuple[dict[str, Any], list[dict[str, str]]]:
    results: list[dict[str, str]] = []
    entrypoints: list[str] = []
    factory_entrypoints: list[str] = []
    paths = sorted(root.glob("*_mcp.py")) + [root / "server.py"]
    for path in paths:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        if "@mcp.tool" not in text:
            continue
        entrypoints.append(path.name)
        if "mcp.server.fastmcp" in text or "FastMCP(" in text:
            results.append(finding("HIGH", "legacy-mcp-v1", f"{path.name}: legacy FastMCP usage.", path))
        if "create_mcp_server(" in text:
            factory_entrypoints.append(path.name)
        else:
            results.append(
                finding(
                    "MEDIUM",
                    "runtime-factory-missing",
                    f"{path.name}: MCP server bypasses the shared observability factory.",
                    path,
                )
            )

    requirements = root / "requirements.txt"
    requirements_text = requirements.read_text(encoding="utf-8") if requirements.exists() else ""
    if "mcp[cli]==2.0.0" not in requirements_text:
        results.append(finding("HIGH", "mcp-version", "mcp[cli]==2.0.0 is not pinned.", requirements))
    for package in (
        f"opentelemetry-sdk=={OTEL_VERSION}",
        f"opentelemetry-exporter-otlp-proto-http=={OTEL_VERSION}",
    ):
        if package not in requirements_text:
            results.append(finding("MEDIUM", "otel-version", f"{package} is not pinned.", requirements))

    workflow = root / ".github" / "workflows" / "mcp-ci.yml"
    workflow_text = workflow.read_text(encoding="utf-8") if workflow.exists() else ""
    if "run_mcp_conformance.ps1" not in workflow_text:
        results.append(finding("MEDIUM", "conformance-ci", "Stateless conformance is not in CI.", workflow))

    runner = root / "scripts" / "run_mcp_conformance.ps1"
    runner_text = runner.read_text(encoding="utf-8") if runner.exists() else ""
    if CONFORMANCE_VERSION not in runner_text:
        results.append(
            finding(
                "MEDIUM",
                "conformance-pin",
                f"Conformance is not pinned to {CONFORMANCE_VERSION}.",
                runner,
            )
        )

    fixture = root / "tests" / "conformance_http_server.py"
    fixture_text = fixture.read_text(encoding="utf-8") if fixture.exists() else ""
    for hook in ("test_trigger_tool_change", "test_trigger_prompt_change"):
        if hook not in fixture_text:
            results.append(
                finding("MEDIUM", "conformance-dynamic-hook", f"Missing safe conformance hook {hook}.", fixture)
            )

    feature_files = {
        "contracts": root / "mcp_contracts.py",
        "extensions": root / "mcp_extensions.py",
        "observability": root / "mcp_observability.py",
    }
    for feature, path in feature_files.items():
        if not path.exists():
            results.append(finding("MEDIUM", f"{feature}-missing", f"{path.name} was not found.", path))

    extensions_text = feature_files["extensions"].read_text(encoding="utf-8") if feature_files["extensions"].exists() else ""
    if "tasks_wire_extension" not in extensions_text or "UnsupportedWireExtension" not in extensions_text:
        results.append(
            finding(
                "HIGH",
                "tasks-wire-gate",
                "Unsupported Tasks wire capability is not guarded fail-closed.",
                feature_files["extensions"],
            )
        )

    workers = root / "workers_mcp.py"
    workers_text = workers.read_text(encoding="utf-8") if workers.exists() else ""
    if "build_apps_extension" not in workers_text or "control_plane_dashboard" not in workers_text:
        results.append(finding("MEDIUM", "apps-pilot", "The read-only MCP Apps pilot is not wired.", workers))
    if "mcp_extension_support() -> MCPFeatureSupportReport" not in workers_text:
        results.append(
            finding("MEDIUM", "typed-tool", "No integrated typed MCP extension status tool was found.", workers)
        )

    return {
        "entrypoint_count": len(entrypoints),
        "entrypoints": entrypoints,
        "shared_factory_count": len(factory_entrypoints),
        "shared_factory_entrypoints": factory_entrypoints,
        "features": {name: path.exists() for name, path in feature_files.items()},
    }, results


def audit_skills(root: Path) -> tuple[dict[str, Any], list[dict[str, str]]]:
    results: list[dict[str, str]] = []
    skills_root = root / ".agents" / "skills"
    installed = sorted(path.name for path in skills_root.iterdir() if path.is_dir()) if skills_root.exists() else []
    security_skill = skills_root / "mcp-security-audit" / "SKILL.md"
    if not security_skill.exists():
        results.append(
            finding("MEDIUM", "security-skill-missing", "mcp-security-audit is not installed.", security_skill)
        )
    elif "github-pinned:" not in security_skill.read_text(encoding="utf-8"):
        results.append(finding("HIGH", "security-skill-unpinned", "mcp-security-audit is not pinned.", security_skill))
    return {"installed": installed, "count": len(installed)}, results


def build_inventory(root: Path) -> dict[str, Any]:
    sys.path.insert(0, str(root))
    try:
        import tool_capability_memory

        inventory = tool_capability_memory.build_tool_inventory(root)
        return {
            "module_count": inventory["module_count"],
            "tool_count": inventory["tool_count"],
            "modules": [
                {
                    "server_name": module["server_name"],
                    "tool_count": module["tool_count"],
                    "tags": module["tags"],
                }
                for module in inventory["modules"]
            ],
        }
    finally:
        sys.path.remove(str(root))


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit the MCP control plane without exposing secrets.")
    parser.add_argument("--root", type=Path, help="MCP project root.")
    parser.add_argument("--config", type=Path, default=Path.home() / ".codex" / "config.toml")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--fail-on", choices=("none", "high", "medium"), default="none")
    args = parser.parse_args()

    root = args.root.resolve() if args.root else find_project_root(Path.cwd().resolve())
    config_summary, config_findings = audit_codex_config(args.config.resolve())
    source_summary, source_findings = audit_sources(root)
    skill_summary, skill_findings = audit_skills(root)
    findings = config_findings + source_findings + skill_findings
    report = {
        "project_root": str(root),
        "config": config_summary,
        "sources": source_summary,
        "skills": skill_summary,
        "tool_inventory": build_inventory(root),
        "summary": {
            "finding_count": len(findings),
            "by_severity": {
                severity: sum(row["severity"] == severity for row in findings)
                for severity in ("HIGH", "MEDIUM", "LOW")
            },
        },
        "findings": findings,
    }

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        inventory = report["tool_inventory"]
        print(f"MCP control plane: {root}")
        print(
            f"Servers: {config_summary['server_count']} | "
            f"Modules: {inventory['module_count']} | "
            f"Tools: {inventory['tool_count']} | "
            f"Skills: {skill_summary['count']}"
        )
        for row in findings:
            print(f"[{row['severity']}] {row['check']}: {row['message']}")
        if not findings:
            print("No findings.")

    severities = {row["severity"] for row in findings}
    if args.fail_on == "high" and "HIGH" in severities:
        return 1
    if args.fail_on == "medium" and severities.intersection({"HIGH", "MEDIUM"}):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
