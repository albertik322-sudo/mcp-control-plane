from __future__ import annotations

import pytest

from tests.live.conftest import require_live_flag


pytestmark = [pytest.mark.live, pytest.mark.live_mikrotik]


def test_mikrotik_probe_smoke() -> None:
    require_live_flag("MCP_LIVE_MIKROTIK")
    import mikrotik_mcp as mikrotik

    probe = mikrotik.probe()

    assert "host" in probe
    assert "api" in probe
