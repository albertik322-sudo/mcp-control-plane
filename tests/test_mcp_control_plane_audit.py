from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "mcp-control-plane-audit"
    / "scripts"
    / "audit_control_plane.py"
)


def load_audit_module():
    spec = importlib.util.spec_from_file_location("mcp_control_plane_audit", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_config_audit_detects_and_redacts_inline_sensitive_value(tmp_path: Path) -> None:
    audit = load_audit_module()
    config = tmp_path / "config.toml"
    sentinel = "example-inline-sensitive-value"
    config.write_text(
        f"""
[mcp_servers.example]
command = "python"
args = ["server.py"]

[mcp_servers.example.env]
API_TOKEN = "{sentinel}"
""",
        encoding="utf-8",
    )

    summary, findings = audit.audit_codex_config(config)

    assert summary["server_count"] == 1
    assert any(row["check"] == "inline-sensitive-env" for row in findings)
    assert sentinel not in repr(findings)


def test_source_audit_accepts_mcp_v2_and_pinned_conformance() -> None:
    audit = load_audit_module()
    root = Path(__file__).resolve().parents[1]

    summary, findings = audit.audit_sources(root)

    assert summary["entrypoint_count"] >= 10
    assert summary["shared_factory_count"] == summary["entrypoint_count"]
    assert all(summary["features"].values())
    assert not [
        row
        for row in findings
        if row["check"]
        in {
            "legacy-mcp-v1",
            "mcp-version",
            "conformance-pin",
            "runtime-factory-missing",
            "tasks-wire-gate",
            "apps-pilot",
            "typed-tool",
        }
    ]
