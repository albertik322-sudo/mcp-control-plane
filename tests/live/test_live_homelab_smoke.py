from __future__ import annotations

import pytest

from tests.live.conftest import require_live_flag


pytestmark = [pytest.mark.live, pytest.mark.live_homelab]


def test_homelab_read_only_smoke() -> None:
    require_live_flag("MCP_LIVE_HOMELAB")
    import homelab_mcp as homelab

    query = homelab.prometheus_query("up")
    alerts = homelab.prometheus_alerts()
    health = homelab.homelab_health()

    assert query["status"] == "success"
    assert alerts["status"] == "success"
    assert "services" in health
