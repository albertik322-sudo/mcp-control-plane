from __future__ import annotations

import pytest

from tests.live.conftest import require_live_flag


pytestmark = [pytest.mark.live, pytest.mark.live_windows]


def test_windows_read_only_smoke() -> None:
    require_live_flag("MCP_LIVE_WINDOWS")
    import windows_system_mcp as windows

    runtime = windows.powershell_runtime()
    summary = windows.system_summary()
    processes = windows.top_processes(limit=5)
    network = windows.network_summary()

    assert runtime["exit_code"] == 0
    assert summary["cpu_count_logical"] >= 1
    assert len(processes["processes"]) <= 5
    assert network["exit_code"] == 0
    assert "stdout" in network
