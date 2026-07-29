from __future__ import annotations

import pytest

from tests.live.conftest import require_live_flag


pytestmark = [pytest.mark.live, pytest.mark.live_homeassistant]


def test_homeassistant_read_only_smoke() -> None:
    require_live_flag("MCP_LIVE_HOMEASSISTANT")
    import homeassistant_mcp as ha

    health = ha.health()
    config = ha.config()
    sun = ha.search_states("sun", limit=3)

    assert health["ok"] is True
    assert "location_name" in config
    assert sun["count"] <= 3
